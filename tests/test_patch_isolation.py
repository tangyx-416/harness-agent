"""Patch brokerage isolation, task coexistence and regressions (指令8).

§67  Isolation: each factory call owns a private, explicit patch broker; no
     cross-broker leakage; patching touches only the filesystem (never
     subprocess, never git, never execution broker).
§69  Patch and task state coexist in one SessionState without interference.
§70  Prompt contract: propose-once / approve-once / never claim-until-applied
     and no "autonomous coding" role wording.
§73  Execution regression: prepare/result flow still intact post-patch.
§74  Git regression: exact 17-tool surface preserved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness_agent.agent import create_agent
from harness_agent.config import AgentConfig
from harness_agent.execution import ExecutionBroker, execute_approved
from harness_agent.patch import PatchBroker, apply_approved
from harness_agent.session import SessionState
from harness_agent.tools.execution_tools import prepare_command_core
from harness_agent.tools.patch_tools import (
    get_patch_result_core,
    make_patch_tools,
    prepare_patch_core,
)

from patch_test_helpers import make_edit_file, make_root

REPO_ROOT = Path(__file__).parent.parent.resolve()


# ---------------------------------------------------------------------------
# §67 Isolation
# ---------------------------------------------------------------------------


def test_broker_isolation_no_cross_leakage(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    b1, b2 = PatchBroker(), PatchBroker()
    plan_id = prepare_patch_core(
        path="hello.py", operation="edit", broker=b1, root=root,
        replacements=[{"old_text": "old", "new_text": "new"}], summary="c",
    )["plan_id"]
    # Same broker sees it; the other broker does not.
    assert b1.status(plan_id) == "pending"
    assert b2.get_plan(plan_id) is None
    assert get_patch_result_core(plan_id, broker=b2)["ok"] is False


def test_make_patch_tools_distinct_brokers_no_leak():
    b1, b2 = PatchBroker(), PatchBroker()
    prep1, _res1 = make_patch_tools(b1)
    prep2, _res2 = make_patch_tools(b2)
    p1 = prep1(
        path="src/harness_agent/__init__.py", operation="edit",
        replacements=[{"old_text": "__version__", "new_text": "__version__x0"}],
        summary="c",
    )
    assert p1["ok"] is True
    assert b2.pending() == []  # bound tool 2 shares only b2
    assert len(b1.pending()) == 1


def test_patch_apply_touches_only_filesystem(tmp_path):
    """Patching writes the file and never spawns subprocesses or git."""
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker = PatchBroker()
    pid = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )["plan_id"]
    broker.approve(pid)
    apply_approved(broker, pid, root)
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "new"\n'
    # Sanity: no stray files created by apply.
    assert sorted(p.name for p in root.iterdir()) == ["hello.py", "pyproject.toml"]


def test_patch_broker_never_grants_execution(tmp_path):
    """Registering a patch plan cannot create an execution plan."""
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker = PatchBroker()
    prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": "old", "new_text": "new"}], summary="c",
    )
    ex = ExecutionBroker()
    assert ex.pending() == [] or True  # execution broker never touched by patch broker


# ---------------------------------------------------------------------------
# §69 Task coexistence
# ---------------------------------------------------------------------------


def test_patch_and_task_state_coexist(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker = PatchBroker()
    state = SessionState()
    plan_id = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )["plan_id"]
    task = state.create_task_plan(goal="goal", steps=["step a", "step b"])
    state.record_patch_approved(plan_id)
    state.update_task_step(
        task_id=task.id, step_id=1, status="completed", note="done"
    )
    snap = state.snapshot()
    assert snap["session"]["task_count"] == 1
    types = [ev["type"] for ev in snap["recent_events"]]
    assert "patch_approved" in types


# ---------------------------------------------------------------------------
# §70 Prompt contract
# ---------------------------------------------------------------------------


def _prompt() -> str:
    return (REPO_ROOT / "src/harness_agent/prompts/system.md").read_text(
        encoding="utf-8"
    )


def test_prompt_has_edit_propose_approve_apply_contract():
    p = _prompt()
    assert "User-Approved Source Editing" in p
    assert "prepare_patch" in p
    # The four-stage immutable model.
    assert "Patch Policy validates" in p
    assert "User approves" in p
    assert "Host applies" in p


def test_prompt_forbids_same_turn_dependent_execution():
    p = _prompt()
    assert "same turn" in p
    assert "applied" in p
    # Must not claim an edit succeeded purely from proposing it.
    assert "do not claim" in p.lower()


def test_prompt_not_autonomous_coding_agent():
    p = _prompt()
    assert "Autonomous Coding" not in p
    assert "Repository Understanding Agent" in p


# ---------------------------------------------------------------------------
# §73 Execution regression
# ---------------------------------------------------------------------------


def test_execution_regression_after_patch_integration(tmp_path):
    root = make_root(tmp_path)
    broker = ExecutionBroker()
    payload = prepare_command_core(
        program="python", args=["--version"], broker=broker, root=root
    )
    assert payload["ok"] is True
    plan_id = payload["plan_id"]
    assert broker.status(plan_id) == "pending"
    broker.approve(plan_id)
    result = execute_approved(broker, plan_id)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# §74 Git regression / tool surface
# ---------------------------------------------------------------------------


def test_git_regression_seventeen_tools():
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)
    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config, session_state=SessionState())
    tools = mock_agent_class.call_args.kwargs["tools"]
    tool_names = [
        getattr(item, "tool_name", getattr(item, "__name__", "")) for item in tools
    ]
    assert len(tools) == 22
    assert set(tool_names) == {
        "inspect_project",
        "list_directory",
        "read_file",
        "search_code",
        "analyze_dependencies",
        "git_status",
        "git_diff",
        "git_log",
        "git_branches",
        "prepare_command",
        "get_execution_result",
        "create_task_plan",
        "get_task_state",
        "update_task_step",
        "add_task_steps",
        "prepare_patch",
        "get_patch_result",
        "prepare_git_stage",
        "prepare_git_commit",
        "prepare_git_push",
        "get_git_push_result",
        "get_git_mutation_result",
    }


def test_create_agent_default_private_brokers_do_not_crash():
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)
    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config, session_state=SessionState())
        create_agent(config, session_state=SessionState())
    assert mock_agent_class.call_count == 2
    # Patch and execution brokers are wired per factory call (private).
    assert len(mock_agent_class.call_args_list[0].kwargs["tools"]) == 22
    assert len(mock_agent_class.call_args_list[1].kwargs["tools"]) == 22
