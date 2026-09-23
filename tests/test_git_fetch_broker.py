"""Tests for Git fetch broker."""

from datetime import datetime, timezone

import pytest

from harness_agent.git_fetch.broker import GitFetchBroker
from harness_agent.git_fetch.models import FetchState, GitFetchPlan, GitFetchResult


@pytest.fixture
def broker():
    """Create a fresh GitFetchBroker for each test."""
    return GitFetchBroker()


@pytest.fixture
def sample_plan():
    """Create a sample GitFetchPlan for testing."""
    return GitFetchPlan(
        plan_id="fetch_test123",
        kind="fetch",
        summary="Test fetch",
        local_branch="main",
        local_head_oid="a" * 40,
        remote_name="origin",
        remote_branch="main",
        tracking_ref="refs/remotes/origin/main",
        expected_tracking_oid="b" * 40,
        approved_remote_url="https://github.com/example/repo.git",
        remote_host="github.com",
        created_at=datetime.now(timezone.utc),
    )


def test_broker_register_plan(broker, sample_plan):
    """Test registering a new fetch plan."""
    broker.register(sample_plan)

    assert broker.get_plan(sample_plan.plan_id) == sample_plan
    assert broker.get_state(sample_plan.plan_id) == FetchState.PENDING


def test_broker_register_duplicate_plan_id(broker, sample_plan):
    """Test registering a plan with duplicate ID fails."""
    broker.register(sample_plan)

    with pytest.raises(ValueError, match="already exists"):
        broker.register(sample_plan)


def test_broker_approve_pending_plan(broker, sample_plan):
    """Test approving a pending plan."""
    broker.register(sample_plan)
    broker.approve(sample_plan.plan_id)

    assert broker.get_state(sample_plan.plan_id) == FetchState.APPROVED


def test_broker_reject_pending_plan(broker, sample_plan):
    """Test rejecting a pending plan."""
    broker.register(sample_plan)
    broker.reject(sample_plan.plan_id)

    assert broker.get_state(sample_plan.plan_id) == FetchState.REJECTED


def test_broker_approve_non_pending_plan_fails(broker, sample_plan):
    """Test approving a non-pending plan fails."""
    broker.register(sample_plan)
    broker.approve(sample_plan.plan_id)

    with pytest.raises(ValueError, match="not pending"):
        broker.approve(sample_plan.plan_id)


def test_broker_take_for_apply_success(broker, sample_plan):
    """Test atomically claiming an approved plan for application."""
    broker.register(sample_plan)
    broker.approve(sample_plan.plan_id)

    assert broker.take_for_apply(sample_plan.plan_id) is True
    assert broker.get_state(sample_plan.plan_id) == FetchState.APPLYING


def test_broker_take_for_apply_only_once(broker, sample_plan):
    """Test take_for_apply succeeds only once (atomic)."""
    broker.register(sample_plan)
    broker.approve(sample_plan.plan_id)

    # First take succeeds
    assert broker.take_for_apply(sample_plan.plan_id) is True

    # Second take fails
    assert broker.take_for_apply(sample_plan.plan_id) is False


def test_broker_take_for_apply_pending_plan_fails(broker, sample_plan):
    """Test take_for_apply on pending plan fails."""
    broker.register(sample_plan)

    assert broker.take_for_apply(sample_plan.plan_id) is False
    assert broker.get_state(sample_plan.plan_id) == FetchState.PENDING


def test_broker_record_result(broker, sample_plan):
    """Test recording a fetch result."""
    broker.register(sample_plan)
    broker.approve(sample_plan.plan_id)
    broker.take_for_apply(sample_plan.plan_id)

    result = GitFetchResult(
        plan_id=sample_plan.plan_id,
        state=FetchState.APPLIED,
        remote_name=sample_plan.remote_name,
        remote_branch=sample_plan.remote_branch,
        tracking_ref=sample_plan.tracking_ref,
        previous_tracking_oid=sample_plan.expected_tracking_oid,
        observed_remote_oid="c" * 40,
        updated_tracking_oid="c" * 40,
        changed=True,
        message="Fetch successful",
        created_at=datetime.now(timezone.utc),
    )

    broker.record_result(result)

    assert broker.get_state(sample_plan.plan_id) == FetchState.APPLIED
    assert broker.get_result(sample_plan.plan_id) == result


def test_broker_record_result_without_applying_fails(broker, sample_plan):
    """Test recording result without claiming plan first fails."""
    broker.register(sample_plan)
    broker.approve(sample_plan.plan_id)

    result = GitFetchResult(
        plan_id=sample_plan.plan_id,
        state=FetchState.APPLIED,
        remote_name=sample_plan.remote_name,
        remote_branch=sample_plan.remote_branch,
        tracking_ref=sample_plan.tracking_ref,
        previous_tracking_oid=sample_plan.expected_tracking_oid,
        observed_remote_oid="c" * 40,
        updated_tracking_oid="c" * 40,
        changed=True,
        message="Fetch successful",
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="not awaiting a result"):
        broker.record_result(result)


def test_broker_pending_plans(broker, sample_plan):
    """Test retrieving all pending plans."""
    broker.register(sample_plan)

    pending = broker.pending_plans()
    assert len(pending) == 1
    assert pending[0] == sample_plan

    # After approval, no longer pending
    broker.approve(sample_plan.plan_id)
    pending = broker.pending_plans()
    assert len(pending) == 0


def test_broker_get_unknown_plan(broker):
    """Test getting unknown plan returns None."""
    assert broker.get_plan("unknown_id") is None
    assert broker.get_state("unknown_id") is None
    assert broker.get_result("unknown_id") is None


def test_broker_isolation():
    """Test two brokers are isolated from each other."""
    broker1 = GitFetchBroker()
    broker2 = GitFetchBroker()

    plan1 = GitFetchPlan(
        plan_id="fetch_1",
        kind="fetch",
        summary="Broker 1",
        local_branch="main",
        local_head_oid="a" * 40,
        remote_name="origin",
        remote_branch="main",
        tracking_ref="refs/remotes/origin/main",
        expected_tracking_oid="b" * 40,
        approved_remote_url="https://github.com/example/repo.git",
        remote_host="github.com",
        created_at=datetime.now(timezone.utc),
    )

    broker1.register(plan1)

    # Broker 2 should not see broker 1's plan
    assert broker2.get_plan(plan1.plan_id) is None
