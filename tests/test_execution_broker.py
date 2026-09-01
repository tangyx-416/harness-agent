"""Tests for the execution broker lifecycle (pending -> approved -> executed,
pending -> rejected; single-use semantics)."""

import dataclasses
import time

import pytest

from harness_agent.execution import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    BrokerError,
    ExecutionBroker,
    ExecutionPlan,
    ExecutionResult,
)


def make_plan(plan_id: str = "plan-1", **overrides) -> ExecutionPlan:
    fields = dict(
        id=plan_id,
        program="python.exe",
        args=("--version",),
        cwd="C:/repo",
        display_command="python --version",
        risk_level="LOW",
        risk_reason="version only",
        timeout_seconds=30,
        created_at=time.time(),
    )
    fields.update(overrides)
    return ExecutionPlan(**fields)


def make_result(plan_id: str = "plan-1", **overrides) -> ExecutionResult:
    fields = dict(
        plan_id=plan_id,
        command="python --version",
        cwd="C:/repo",
        risk_level="LOW",
        approved=True,
        exit_code=0,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        duration_ms=12,
    )
    fields.update(overrides)
    return ExecutionResult(**fields)


def test_create_pending_plan():
    broker = ExecutionBroker()
    plan = make_plan()
    broker.register(plan)

    assert broker.get_plan(plan.id) == plan
    assert broker.status(plan.id) == STATUS_PENDING
    assert broker.pending() == [plan]
    assert broker.get_result(plan.id) is None


def test_plan_is_immutable():
    plan = make_plan()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.args = ("--version", "-q")  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.program = "evil.exe"  # type: ignore[misc]


def test_unknown_plan_rejected_everywhere():
    broker = ExecutionBroker()
    with pytest.raises(BrokerError):
        broker.approve("nope")
    with pytest.raises(BrokerError):
        broker.reject("nope")
    with pytest.raises(BrokerError):
        broker.take_for_execution("nope")
    assert broker.get_plan("nope") is None
    assert broker.status("nope") is None


def test_reject_plan():
    broker = ExecutionBroker()
    broker.register(make_plan())
    rejected = broker.reject("plan-1")

    assert broker.status("plan-1") == STATUS_REJECTED
    assert broker.pending() == []
    assert rejected.id == "plan-1"


def test_rejected_plan_cannot_be_executed():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.reject("plan-1")

    with pytest.raises(BrokerError):
        broker.take_for_execution("plan-1")


def test_approve_plan():
    broker = ExecutionBroker()
    broker.register(make_plan())
    approved = broker.approve("plan-1")

    assert broker.status("plan-1") == STATUS_APPROVED
    assert broker.pending() == []
    assert approved.id == "plan-1"


def test_execute_once_then_denied():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.approve("plan-1")

    claimed = broker.take_for_execution("plan-1")
    assert claimed.id == "plan-1"
    assert broker.status("plan-1") == STATUS_EXECUTED

    with pytest.raises(BrokerError):
        broker.take_for_execution("plan-1")


def test_unapproved_plan_cannot_execute():
    broker = ExecutionBroker()
    broker.register(make_plan())
    with pytest.raises(BrokerError):
        broker.take_for_execution("plan-1")


def test_double_approval_denied():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.approve("plan-1")
    with pytest.raises(BrokerError):
        broker.approve("plan-1")


def test_record_and_read_result():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.approve("plan-1")
    broker.take_for_execution("plan-1")

    result = make_result()
    broker.record_result(result)

    assert broker.get_result("plan-1") == result


def test_pending_returns_only_pending_in_order():
    broker = ExecutionBroker()
    for index in range(3):
        broker.register(make_plan(f"plan-{index}"))
    broker.approve("plan-0")

    pending_ids = [plan.id for plan in broker.pending()]
    assert pending_ids == ["plan-1", "plan-2"]


# ---------------------------------------------------------------------------
# v0.3.0 release audit: explicit state-machine enforcement
# ---------------------------------------------------------------------------


def test_rejected_cannot_become_approved():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.reject("plan-1")
    with pytest.raises(BrokerError):
        broker.approve("plan-1")
    assert broker.status("plan-1") == STATUS_REJECTED


def test_rejected_cannot_be_executed():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.reject("plan-1")
    with pytest.raises(BrokerError):
        broker.take_for_execution("plan-1")


def test_executed_cannot_be_approved():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.approve("plan-1")
    broker.take_for_execution("plan-1")
    with pytest.raises(BrokerError):
        broker.approve("plan-1")
    assert broker.status("plan-1") == STATUS_EXECUTED


def test_executed_cannot_be_rejected():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.approve("plan-1")
    broker.take_for_execution("plan-1")
    with pytest.raises(BrokerError):
        broker.reject("plan-1")
    assert broker.status("plan-1") == STATUS_EXECUTED


def test_approved_can_only_transition_to_executed():
    broker = ExecutionBroker()
    broker.register(make_plan())
    broker.approve("plan-1")
    # The ONLY legal transition out of approved is single-use execution.
    claimed = broker.take_for_execution("plan-1")
    assert broker.status(claimed.id) == STATUS_EXECUTED
    with pytest.raises(BrokerError):
        broker.take_for_execution("plan-1")


def test_unknown_id_never_implicitly_creates():
    broker = ExecutionBroker()
    with pytest.raises(BrokerError):
        broker.approve("ghost-id")
    with pytest.raises(BrokerError):
        broker.take_for_execution("ghost-id")
    assert broker.get_plan("ghost-id") is None
    assert broker.pending() == []
