"""Tests for Git remote push broker."""

import threading
from datetime import datetime, timezone

import pytest

from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.git_remote.models import GitPushPlan, GitPushResult, PushCommit, PushState


def _make_test_plan(plan_id="push-test-123") -> GitPushPlan:
    """Create a test push plan."""
    return GitPushPlan(
        plan_id=plan_id,
        kind="push",
        summary="Test push",
        local_branch="main",
        head_oid="a" * 40,
        head_tree_oid="b" * 40,
        remote_name="origin",
        remote_branch="main",
        approved_remote_url="https://github.com/test/repo.git",
        remote_host="github.com",
        expected_remote_oid="c" * 40,
        expected_tree_oid="d" * 40,
        outgoing_commits=(
            PushCommit(oid="e" * 40, short_oid="e" * 7, subject="Test commit"),
        ),
        commit_count=1,
        diff="diff --git a/test.txt b/test.txt\n",
        diff_sha256="f" * 64,
        created_at=datetime.now(timezone.utc),
    )


def test_broker_starts_empty():
    """New broker has no plans."""
    broker = GitRemoteBroker()
    assert broker.pending_plans() == []
    assert broker.get_plan("nonexistent") is None
    assert broker.get_state("nonexistent") is None
    assert broker.get_result("nonexistent") is None


def test_broker_register_plan():
    """Broker can register a new plan."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()

    broker.register(plan)

    assert broker.get_plan(plan.plan_id) == plan
    assert broker.get_state(plan.plan_id) == PushState.PENDING
    assert plan in broker.pending_plans()


def test_broker_register_duplicate_fails():
    """Cannot register same plan ID twice."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()

    broker.register(plan)

    with pytest.raises(ValueError, match="already exists"):
        broker.register(plan)


def test_broker_approve_pending_plan():
    """Broker can approve a pending plan."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)

    broker.approve(plan.plan_id)

    assert broker.get_state(plan.plan_id) == PushState.APPROVED
    assert broker.pending_plans() == []


def test_broker_approve_unknown_plan_fails():
    """Cannot approve unknown plan."""
    broker = GitRemoteBroker()

    with pytest.raises(ValueError, match="Unknown plan"):
        broker.approve("nonexistent")


def test_broker_approve_non_pending_fails():
    """Cannot approve a non-pending plan."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.reject(plan.plan_id)

    with pytest.raises(ValueError, match="not pending"):
        broker.approve(plan.plan_id)


def test_broker_reject_pending_plan():
    """Broker can reject a pending plan."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)

    broker.reject(plan.plan_id)

    assert broker.get_state(plan.plan_id) == PushState.REJECTED
    assert broker.pending_plans() == []


def test_broker_reject_unknown_plan_fails():
    """Cannot reject unknown plan."""
    broker = GitRemoteBroker()

    with pytest.raises(ValueError, match="Unknown plan"):
        broker.reject("nonexistent")


def test_broker_reject_non_pending_fails():
    """Cannot reject a non-pending plan."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.approve(plan.plan_id)

    with pytest.raises(ValueError, match="not pending"):
        broker.reject(plan.plan_id)


def test_broker_take_for_apply_success():
    """take_for_apply transitions approved to applying."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.approve(plan.plan_id)

    claimed = broker.take_for_apply(plan.plan_id)

    assert claimed is True
    assert broker.get_state(plan.plan_id) == PushState.APPLYING


def test_broker_take_for_apply_unknown_fails():
    """take_for_apply returns False for unknown plan."""
    broker = GitRemoteBroker()

    claimed = broker.take_for_apply("nonexistent")

    assert claimed is False


def test_broker_take_for_apply_not_approved_fails():
    """take_for_apply returns False for non-approved plan."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)

    claimed = broker.take_for_apply(plan.plan_id)

    assert claimed is False
    assert broker.get_state(plan.plan_id) == PushState.PENDING


def test_broker_take_for_apply_exactly_once():
    """take_for_apply can only succeed once."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.approve(plan.plan_id)

    first = broker.take_for_apply(plan.plan_id)
    second = broker.take_for_apply(plan.plan_id)

    assert first is True
    assert second is False


def test_broker_record_result():
    """Broker can record a push result."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.approve(plan.plan_id)
    broker.take_for_apply(plan.plan_id)

    result = GitPushResult(
        plan_id=plan.plan_id,
        state=PushState.APPLIED,
        remote_name="origin",
        remote_branch="main",
        head_oid="a" * 40,
        remote_oid_after="a" * 40,
        message="Success",
        created_at=datetime.now(timezone.utc),
    )

    broker.record_result(result)

    assert broker.get_state(plan.plan_id) == PushState.APPLIED
    assert broker.get_result(plan.plan_id) == result


def test_broker_record_result_unknown_plan_fails():
    """Cannot record result for unknown plan."""
    broker = GitRemoteBroker()

    result = GitPushResult(
        plan_id="nonexistent",
        state=PushState.APPLIED,
        remote_name="origin",
        remote_branch="main",
        head_oid="a" * 40,
        remote_oid_after="a" * 40,
        message="Success",
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="Unknown plan"):
        broker.record_result(result)


def test_broker_record_result_not_applying_fails():
    """Cannot record result for plan not in applying state."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.approve(plan.plan_id)

    result = GitPushResult(
        plan_id=plan.plan_id,
        state=PushState.APPLIED,
        remote_name="origin",
        remote_branch="main",
        head_oid="a" * 40,
        remote_oid_after="a" * 40,
        message="Success",
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="not awaiting a result"):
        broker.record_result(result)


def test_broker_pending_plans_filters_correctly():
    """pending_plans returns only pending plans."""
    broker = GitRemoteBroker()
    plan1 = _make_test_plan("push-1")
    plan2 = _make_test_plan("push-2")
    plan3 = _make_test_plan("push-3")

    broker.register(plan1)
    broker.register(plan2)
    broker.register(plan3)

    broker.approve(plan2.plan_id)
    broker.reject(plan3.plan_id)

    pending = broker.pending_plans()

    assert len(pending) == 1
    assert pending[0] == plan1


def test_broker_concurrent_take_for_apply():
    """Only one thread can take_for_apply successfully."""
    broker = GitRemoteBroker()
    plan = _make_test_plan()
    broker.register(plan)
    broker.approve(plan.plan_id)

    results = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()  # Synchronize all threads
        claimed = broker.take_for_apply(plan.plan_id)
        results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread should have claimed it
    assert sum(results) == 1


def test_two_brokers_are_isolated():
    """Two broker instances are completely isolated."""
    broker_a = GitRemoteBroker()
    broker_b = GitRemoteBroker()

    plan = _make_test_plan()
    broker_a.register(plan)

    assert broker_b.get_plan(plan.plan_id) is None
    assert broker_b.get_state(plan.plan_id) is None
    assert broker_b.pending_plans() == []
