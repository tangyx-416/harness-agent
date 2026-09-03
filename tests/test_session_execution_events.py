"""Host execution lifecycle integration with metadata-only session events."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import run_agent  # noqa: E402

from harness_agent.execution import ExecutionBroker, ExecutionResult  # noqa: E402
from harness_agent.session import SessionState  # noqa: E402
from harness_agent.tools.execution_tools import prepare_command_core  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent.resolve()


def prepared_broker():
    broker = ExecutionBroker()
    payload = prepare_command_core(
        "python", ["--version"], broker=broker, root=REPO_ROOT
    )
    return broker, payload["plan_id"]


def fake_result(plan_id: str) -> ExecutionResult:
    return ExecutionResult(
        plan_id=plan_id,
        command="python --version --secret-do-not-store",
        cwd=str(REPO_ROOT),
        risk_level="LOW",
        approved=True,
        exit_code=0,
        stdout="OPENAI_API_KEY=sk-secret stdout",
        stderr="credential stderr",
        stdout_truncated=True,
        stderr_truncated=False,
        timed_out=False,
        duration_ms=12,
    )


def test_approval_and_completion_events_are_host_verified_metadata_only():
    broker, plan_id = prepared_broker()
    state = SessionState()
    with patch("builtins.input", return_value="yes"), patch(
        "run_agent.execute_approved", return_value=fake_result(plan_id)
    ), patch("sys.stdout", new_callable=StringIO):
        run_agent.process_pending_executions(broker, session_state=state)

    events = state.snapshot()["recent_events"]
    assert [event["type"] for event in events] == [
        "execution_approved",
        "execution_completed",
    ]
    assert [event["reference"] for event in events] == [plan_id, plan_id]
    serialized = json.dumps(state.snapshot())
    for secret in (
        "OPENAI_API_KEY",
        "sk-secret",
        "credential stderr",
        "--secret-do-not-store",
        str(REPO_ROOT),
    ):
        assert secret not in serialized
    assert "exit_code=0" in events[-1]["summary"]
    assert "stdout_truncated=True" in events[-1]["summary"]


def test_rejection_event_and_zero_subprocess_execution():
    broker, plan_id = prepared_broker()
    state = SessionState()

    def refuse_execution(*_args, **_kwargs):
        raise AssertionError("rejected execution reached subprocess service")

    with patch("builtins.input", return_value="n"), patch(
        "run_agent.execute_approved", side_effect=refuse_execution
    ), patch("sys.stdout", new_callable=StringIO):
        run_agent.process_pending_executions(broker, session_state=state)

    events = state.snapshot()["recent_events"]
    assert len(events) == 1
    assert events[0]["type"] == "execution_rejected"
    assert events[0]["reference"] == plan_id
    assert broker.get_result(plan_id) is None


def test_policy_denial_creates_no_execution_event_and_no_plan():
    broker = ExecutionBroker()
    state = SessionState()
    payload = prepare_command_core("git", ["push"], broker=broker, root=REPO_ROOT)
    assert payload["ok"] is False
    run_agent.process_pending_executions(broker, session_state=state)
    assert broker.pending() == []
    assert state.snapshot()["recent_events"] == []


def test_host_events_do_not_guess_or_mutate_task_step_status():
    broker, plan_id = prepared_broker()
    state = SessionState()
    task = state.create_task("Run tests", ["Run focused tests"])
    state.update_step(task.id, 1, "in_progress", "Prepared; awaiting approval.")

    with patch("builtins.input", return_value="yes"), patch(
        "run_agent.execute_approved", return_value=fake_result(plan_id)
    ), patch("sys.stdout", new_callable=StringIO):
        run_agent.process_pending_executions(broker, session_state=state)

    assert state.get_task(task.id).steps[0].status == "in_progress"


def test_task_note_cannot_bypass_git_execution_policy():
    state = SessionState()
    task = state.create_task("Ship change", ["Push Git commit"])
    state.update_step(task.id, 1, "blocked", "Approved: run git push")
    broker = ExecutionBroker()
    payload = prepare_command_core("git", ["push"], broker=broker, root=REPO_ROOT)
    assert payload["ok"] is False
    assert broker.pending() == []
    assert state.get_task(task.id).steps[0].status == "blocked"
