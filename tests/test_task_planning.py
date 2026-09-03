"""Deterministic planning-flow and authority-boundary integration tests."""

from pathlib import Path

from harness_agent.execution import ExecutionBroker
from harness_agent.session import SessionState
from harness_agent.tools.execution_tools import prepare_command_core
from harness_agent.tools.git_tools import git_log_core, git_status_core
from harness_agent.tools.task_tools import (
    create_task_plan_core,
    update_task_step_core,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()


def test_planning_flow_tracks_observed_git_work_without_execution():
    state = SessionState()
    created = create_task_plan_core(
        state,
        "Inspect repository and verify tests",
        ["Inspect Git state", "Inspect history", "Run focused tests"],
    )
    task_id = created["task"]["id"]

    status = git_status_core(REPO_ROOT)
    assert status["ok"] is True
    update_task_step_core(state, task_id, 1, "completed", "git_status succeeded.")

    history = git_log_core(limit=1, root=REPO_ROOT)
    assert history["ok"] is True
    update_task_step_core(state, task_id, 2, "completed", "git_log returned HEAD.")

    broker = ExecutionBroker()
    prepared = prepare_command_core(
        "pytest", ["-q", "tests/test_session_state.py"], broker=broker, root=REPO_ROOT
    )
    assert prepared["ok"] is True
    update_task_step_core(
        state,
        task_id,
        3,
        "in_progress",
        f"Execution plan {prepared['plan_id']} prepared; awaiting user approval.",
    )

    task = state.get_task(task_id)
    assert task.status == "active"
    assert task.steps[2].status == "in_progress"
    assert broker.status(prepared["plan_id"]) == "pending"
    assert broker.get_result(prepared["plan_id"]) is None


def test_planning_never_expands_the_execution_allowlist():
    state = SessionState()
    task = state.create_task("Change repository", ["Edit source", "Commit changes"])
    state.update_step(task.id, 1, "blocked", "Source editing is unsupported.")
    state.update_step(task.id, 2, "blocked", "Git mutation is unsupported.")

    broker = ExecutionBroker()
    for program, args in (
        ("git", ["commit", "-m", "done"]),
        ("python", ["-c", "open('x', 'w').write('x')"]),
        ("pip", ["install", "requests"]),
    ):
        assert prepare_command_core(
            program, args, broker=broker, root=REPO_ROOT
        )["ok"] is False
    assert state.get_task(task.id).status == "blocked"
    assert broker.pending() == []
