"""Tests for the CLI approval UX and execution tool integration.

Covers the trusted host layer: only explicit y/yes approves, Enter or
anything else denies, Ctrl+C cancels, denied commands never reach a
subprocess, and approved plans execute exactly the validated argv.
"""

import dataclasses
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import run_agent  # noqa: E402

from harness_agent.execution import (  # noqa: E402
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ExecutionBroker,
    ExecutionResult,
)
from harness_agent.tools.execution_tools import (  # noqa: E402
    get_execution_result_core,
    prepare_command_core,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()


def prepared_broker(program="python", args=None):
    """Fresh broker with one pending, policy-approved plan."""
    broker = ExecutionBroker()
    result = prepare_command_core(
        program, args=args if args is not None else ["--version"],
        broker=broker, root=REPO_ROOT,
    )
    assert result["ok"] is True
    return broker, result["plan_id"]


def refuse_execution_guard(*_args, **_kwargs):
    raise AssertionError("ExecutionService must not be called on denial")


# ---------------------------------------------------------------------------
# Approval UX
# ---------------------------------------------------------------------------


def test_y_approves_and_executes_exact_validated_argv():
    broker, plan_id = prepared_broker("python", ["--version"])
    plan = broker.get_plan(plan_id)

    # The plan holds exactly the validated argv -- nothing else can run.
    assert plan.program == sys.executable
    assert plan.args == ("--version",)

    with patch("builtins.input", return_value="y"):
        with patch("sys.stdout", new_callable=StringIO) as output:
            run_agent.process_pending_executions(broker)

    assert broker.status(plan_id) == STATUS_EXECUTED
    result = broker.get_result(plan_id)
    assert result.exit_code == 0
    text = output.getvalue()
    assert "Execution approval required" in text
    assert "python --version" in text
    assert "OS-user privileges" in text
    assert "Execution finished" in text


def test_yes_approves():
    broker, plan_id = prepared_broker()
    with patch("builtins.input", return_value="yes"):
        with patch("sys.stdout", new_callable=StringIO):
            run_agent.process_pending_executions(broker)
    assert broker.status(plan_id) == STATUS_EXECUTED


def test_enter_defaults_to_deny():
    broker, plan_id = prepared_broker()
    with patch("builtins.input", return_value=""):
        with patch("run_agent.execute_approved", side_effect=refuse_execution_guard):
            with patch("sys.stdout", new_callable=StringIO):
                run_agent.process_pending_executions(broker)
    assert broker.status(plan_id) == STATUS_REJECTED
    assert broker.get_result(plan_id) is None


def test_n_denies():
    broker, plan_id = prepared_broker()
    with patch("builtins.input", return_value="n"):
        with patch("run_agent.execute_approved", side_effect=refuse_execution_guard):
            with patch("sys.stdout", new_callable=StringIO):
                run_agent.process_pending_executions(broker)
    assert broker.status(plan_id) == STATUS_REJECTED


def test_random_input_denies():
    broker, plan_id = prepared_broker()
    for answer in ("YESS", "approve", "1", " y extra"):
        with patch("builtins.input", return_value=answer):
            with patch(
                "run_agent.execute_approved", side_effect=refuse_execution_guard
            ):
                with patch("sys.stdout", new_callable=StringIO):
                    run_agent.process_pending_executions(broker)
        assert broker.status(plan_id) == STATUS_REJECTED


def test_keyboard_interrupt_cancels_without_execution():
    broker, plan_id = prepared_broker()
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        with patch("run_agent.execute_approved", side_effect=refuse_execution_guard):
            with patch("sys.stdout", new_callable=StringIO):
                run_agent.process_pending_executions(broker)
    assert broker.status(plan_id) == STATUS_REJECTED
    assert broker.pending() == []


def test_denied_command_never_reaches_subprocess():
    broker = ExecutionBroker()
    for program, args in (
        ("python", ["-c", "print('hello')"]),
        ("pip", ["install", "requests"]),
        ("git", ["status"]),
        ("pytest", ["&&", "evil"]),
    ):
        result = prepare_command_core(program, args=args, broker=broker, root=REPO_ROOT)
        assert result["ok"] is False
        assert result["denied"] is True

    assert broker.pending() == []
    assert list(broker._entries) == []


# ---------------------------------------------------------------------------
# Tool integration (prepare + result lookup, host executes)
# ---------------------------------------------------------------------------


def test_prepare_command_returns_pending_plan():
    broker, plan_id = prepared_broker("pytest", ["-q", "tests"])
    payload = get_execution_result_core(plan_id, broker=broker)

    assert payload["ok"] is True
    assert payload["executed"] is False
    assert payload["status"] == STATUS_PENDING
    assert "do not claim" in payload["message"].lower()


def test_get_execution_result_unknown_plan():
    payload = get_execution_result_core("does-not-exist", broker=ExecutionBroker())
    assert payload["ok"] is False
    assert "unknown" in payload["error"].lower()


def test_result_flow_after_host_execution():
    broker, plan_id = prepared_broker("python", ["--version"])

    broker.approve(plan_id)
    broker.take_for_execution(plan_id)
    broker.record_result(
        ExecutionResult(
            plan_id=plan_id,
            command="python --version",
            cwd=str(REPO_ROOT),
            risk_level="LOW",
            approved=True,
            exit_code=0,
            stdout="Python 3.11.5",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            duration_ms=42,
        )
    )

    payload = get_execution_result_core(plan_id, broker=broker)
    assert payload["ok"] is True
    assert payload["executed"] is True
    assert payload["exit_code"] == 0
    assert "Python" in payload["stdout"]


def test_prepare_denial_payload_shape():
    broker = ExecutionBroker()
    payload = prepare_command_core(
        "python", args=["-m", "pip", "install", "requests"], broker=broker, root=REPO_ROOT
    )
    assert payload["ok"] is False
    assert payload["denied"] is True
    assert "pip" in payload["error"]
    assert "allowlist" in payload["hint"].lower()


# ---------------------------------------------------------------------------
# v0.3.0 release audit: independent approvals, isolation, display/argv equivalence
# ---------------------------------------------------------------------------


def test_multiple_pending_plans_approved_independently():
    """Three plans in one turn: yes / no / Enter -> exactly one execution."""
    broker = ExecutionBroker()
    ids = []
    for _ in range(3):
        prep = prepare_command_core(
            "python", ["--version"], broker=broker, root=REPO_ROOT
        )
        ids.append(prep["plan_id"])

    executed_plan_ids = []
    real_execute = run_agent.execute_approved

    def counting_execute(b, plan_id, **kwargs):
        executed_plan_ids.append(plan_id)
        return real_execute(b, plan_id, **kwargs)

    with patch("builtins.input", side_effect=["y", "n", ""]):
        with patch("run_agent.execute_approved", side_effect=counting_execute):
            with patch("sys.stdout", new_callable=StringIO):
                run_agent.process_pending_executions(broker)

    assert executed_plan_ids == [ids[0]]
    assert broker.status(ids[0]) == STATUS_EXECUTED
    assert broker.status(ids[1]) == STATUS_REJECTED
    assert broker.status(ids[2]) == STATUS_REJECTED
    # No plan inherited approval from another.
    assert broker.get_result(ids[1]) is None
    assert broker.get_result(ids[2]) is None


def test_display_command_is_never_executed():
    """Approval sees display_command; execution uses only program + args."""
    broker, plan_id = prepared_broker("python", ["--version"])
    plan = broker.get_plan(plan_id)

    # display is a pure rendering of the validated plan
    assert plan.display_command == "python --version"
    # the frozen plan cannot be tampered with after approval
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.display_command = "python -c evil"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.args = ("-c", "evil")  # type: ignore[misc]
    # the execution path composes argv solely from program + args
    assert plan.program == sys.executable
    assert plan.args == ("--version",)


def test_get_execution_result_is_read_only():
    """The read-only tool cannot enumerate, mutate, approve or execute."""
    broker, plan_id = prepared_broker("python", ["--version"])

    payload = get_execution_result_core(plan_id, broker=broker)
    assert payload["ok"] is True
    assert payload["executed"] is False
    assert payload["status"] == STATUS_PENDING

    # status untouched: no approval, no mutation, no execution
    assert broker.status(plan_id) == STATUS_PENDING
    assert broker.get_result(plan_id) is None
    assert len(broker.pending()) == 1


# ---------------------------------------------------------------------------
# CLI module structure
# ---------------------------------------------------------------------------


def test_run_agent_module_has_execution_host_functions():
    assert hasattr(run_agent, "process_pending_executions")
    assert hasattr(run_agent, "request_approval")
    assert hasattr(run_agent, "show_approval_request")


def test_approval_has_no_bypass_flags():
    """No AUTO_APPROVE / --yes / --force style bypass may exist."""
    source = Path(run_agent.__file__).read_text(encoding="utf-8")
    assert "AUTO_APPROVE" not in source
    assert "--yes" not in source
    assert "--force" not in source
    assert run_agent._APPROVAL_WORDS == frozenset({"y", "yes"})


def test_process_pending_executions_remains_compatible_without_session_state():
    """Existing callers may omit v0.5 task state without changing approval UX."""
    broker, plan_id = prepared_broker()
    with patch("builtins.input", return_value="n"), patch(
        "sys.stdout", new_callable=StringIO
    ):
        run_agent.process_pending_executions(broker)
    assert broker.status(plan_id) == STATUS_REJECTED


def test_cli_banner_discloses_ephemeral_session_state():
    with patch("sys.stdout", new_callable=StringIO) as output:
        run_agent.print_banner()
    text = output.getvalue().lower()
    assert "v0.6" in text
    assert "session state: ephemeral" in text
    assert "source edits" in text
