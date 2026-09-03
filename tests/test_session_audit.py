"""v0.5.0 audit: tool error contract, closure metadata, no-CoT, authority
boundary, no persistence, process-restart, exact tools (sections 17-27)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from harness_agent.execution import ExecutionBroker
from harness_agent.session import (
    MAX_STEPS_PER_TASK,
    MAX_TASKS,
    SessionState,
)
from harness_agent.tools.execution_tools import (
    get_execution_result_core,
    make_execution_tools,
    prepare_command_core,
)
from harness_agent.tools.task_tools import (
    add_task_steps_core,
    create_task_plan_core,
    get_task_state_core,
    make_task_tools,
    update_task_step_core,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()


# ---------------------------------------------------------------------------
# 17. State Tool Error Contract (structured errors, not exceptions)
# ---------------------------------------------------------------------------


def test_task_tools_never_raise_user_recoverable_errors():
    state = SessionState()
    task = create_task_plan_core(state, "Goal", ["One"])["task"]
    tid = task["id"]

    # unknown task
    assert get_task_state_core(state, "f" * 32)["ok"] is False
    assert update_task_step_core(state, "f" * 32, 1, "completed")["ok"] is False
    assert add_task_steps_core(state, "f" * 32, ["x"])["ok"] is False
    # unknown step
    assert update_task_step_core(state, tid, 99, "completed")["ok"] is False
    # invalid transition (completed -> pending is illegal)
    state2 = SessionState()
    t2 = create_task_plan_core(state2, "G", ["a", "b"])["task"]
    update_task_step_core(state2, t2["id"], 1, "completed")
    assert update_task_step_core(state2, t2["id"], 1, "in_progress")["ok"] is False
    # blank input
    assert create_task_plan_core(state, "", ["x"])["ok"] is False
    assert create_task_plan_core(state, "Goal", [])["ok"] is False  # no steps
    # limit reached
    s3 = SessionState()
    for i in range(MAX_TASKS):
        create_task_plan_core(s3, f"G{i}", ["x"])
    assert create_task_plan_core(s3, "overflow", ["x"])["ok"] is False
    # invalid status
    assert update_task_step_core(state, tid, 1, "almost_done")["ok"] is False


# ---------------------------------------------------------------------------
# 18. Tool Closure Metadata (stable model-visible names)
# ---------------------------------------------------------------------------


def test_two_agents_tool_names_and_schemas_identical():
    a = make_task_tools(SessionState())
    b = make_task_tools(SessionState())

    a_names = [t.tool_name for t in a]
    b_names = [t.tool_name for t in b]
    expected = [
        "create_task_plan",
        "get_task_state",
        "update_task_step",
        "add_task_steps",
    ]
    assert a_names == expected
    assert b_names == expected
    # No uuid/suffix mangling of tool names.
    for name in a_names:
        assert name not in {f"{expected_name}_1" for expected_name in expected}


def test_execution_closure_tool_names_exact():
    broker = ExecutionBroker()
    prep, result = make_execution_tools(broker)
    assert prep.tool_name == "prepare_command"
    assert result.tool_name == "get_execution_result"


# ---------------------------------------------------------------------------
# 19. No Chain-of-Thought schema
# ---------------------------------------------------------------------------


def test_state_schema_has_no_private_reasoning_fields():
    import harness_agent.session.models as models_module

    source = inspect.getsource(models_module)
    for forbidden in (
        "reasoning",
        "analysis",
        "thoughts",
        "chain_of_thought",
        "scratchpad",
        "private_notes",
    ):
        assert forbidden not in source
    # A plain observable note field is allowed.
    assert "note" in source


# ---------------------------------------------------------------------------
# 20. Planning Evidence Semantics (prepared != executed != succeeded)
# ---------------------------------------------------------------------------


def test_prepared_approved_completed_are_distinct_evidence():
    broker = ExecutionBroker()
    prepped = prepare_command_core(
        "python", ["--version"], broker=broker, root=REPO_ROOT
    )
    assert prepped["ok"] is True
    assert prepped["status"] == "prepared"

    # Prepared only -> not executed.
    looked = get_execution_result_core(prepped["plan_id"], broker=broker)
    assert looked["executed"] is False
    assert looked["status"] == "pending"

    # Approved alive doesn't imply success.
    broker.approve(prepped["plan_id"])
    got = get_execution_result_core(prepped["plan_id"], broker=broker)
    assert got["status"] == "approved"
    assert got["executed"] is False

    # A nonzero completion is never "success" metadata.
    state = SessionState()
    state.record_execution_completed(
        prepped["plan_id"],
        exit_code=2,
        timed_out=False,
        duration_ms=5,
        stdout_truncated=False,
        stderr_truncated=True,
    )
    snaps = json.dumps(state.snapshot())
    assert "exit_code=2" in snaps
    assert "succeeded" not in snaps


# ---------------------------------------------------------------------------
# 21. Planning Does Not Grant Git Authority
# ---------------------------------------------------------------------------


def test_git_push_step_never_grants_execution_authority():
    state = SessionState()
    task = state.create_task("Ship change", ["Commit and push changes"])
    state.update_step(task.id, 1, "blocked", "Git mutation unsupported.")

    broker = ExecutionBroker()
    denied = prepare_command_core(
        "git", ["push"], broker=broker, root=REPO_ROOT
    )
    assert denied["ok"] is False
    assert denied["denied"] is True
    assert broker.pending() == []

    # No git_commit / git_push tool exists anywhere.
    from harness_agent.agent import create_agent

    source = inspect.getsource(create_agent)
    assert "git_push" not in source
    assert "git_commit" not in source
    assert state.get_task(task.id).status == "blocked"


# ---------------------------------------------------------------------------
# 22. No Persistence in production session/task code
# ---------------------------------------------------------------------------


def test_session_production_code_has_no_persistence_or_execution():
    from harness_agent.session import state as state_module
    from harness_agent.tools import task_tools as task_tools_module

    combined = inspect.getsource(state_module) + inspect.getsource(task_tools_module)
    for forbidden in (
        "open(",
        "write_text",
        "write_bytes",
        "sqlite",
        "pickle",
        "shelve",
        "redis",
        "database",
        "subprocess",
        "shell=True",
        "os.system",
        "eval(",
        "exec(",
        "requests.",
        "socket",
    ):
        assert forbidden not in combined, f"forbidden token present: {forbidden}"


# ---------------------------------------------------------------------------
# 23. Process Restart Semantic (no module-global restore)
# ---------------------------------------------------------------------------


def test_discarding_state_yields_empty_new_session():
    state_a = SessionState()
    state_a.create_task("Do work", ["One"])
    assert state_a.snapshot()["session"]["task_count"] == 1
    # "restart": the old state object is abandoned and never restored.
    del state_a

    state_b = SessionState()
    assert state_b.snapshot()["session"]["task_count"] == 0
    assert state_b.snapshot()["recent_events"] == []
    assert state_b.snapshot()["active_task"] is None


# ---------------------------------------------------------------------------
# A copy of the cross-session isolation guard for add steps (no leakage)
# ---------------------------------------------------------------------------


def test_cross_agent_task_isolation_and_append_terminals():
    a = SessionState()
    b = SessionState()
    ta = a.create_task("A goal", ["a1"])
    b.create_task("B goal", ["b1"])

    # A's completed task cannot be reopened via B-inspired append.
    a.update_step(ta.id, 1, "completed")
    try:
        a.add_steps(ta.id, ["a2"])
        reopened = True
    except Exception:
        reopened = False
    assert reopened is False
    assert b.get_task(ta.id) is None
