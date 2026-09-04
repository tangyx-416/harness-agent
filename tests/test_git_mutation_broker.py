"""Tests for GitMutationBroker (v0.7.0).

Validates exactly-once semantics, state machine, isolation, and bounds.
"""

import pytest
import threading
import time

from harness_agent.git_mutation import (
    GitMutationBroker,
    GitMutationBrokerError,
    GitStagePlan,
    GitCommitPlan,
    IndexEntry,
    KIND_STAGE,
    KIND_COMMIT,
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_APPLYING,
    STATUS_APPLIED,
    STATUS_REJECTED,
    STATUS_CONFLICT,
    STATUS_FAILED,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_stage_plan(plan_id="stage-001", path="test.txt"):
    return GitStagePlan(
        id=plan_id,
        kind=KIND_STAGE,
        repo_path=path,
        summary="Test change",
        branch="main",
        head_oid="a" * 40,
        base_worktree_sha256="b" * 64,
        base_index_fingerprint="c" * 64,
        previous_index_entry=None,
        proposed_mode="100644",
        proposed_blob_oid="d" * 40,
        diff="diff content",
        diff_sha256="e" * 64,
        created_at=time.time(),
    )


def _make_commit_plan(plan_id="commit-001"):
    return GitCommitPlan(
        id=plan_id,
        kind=KIND_COMMIT,
        branch="main",
        head_oid="a" * 40,
        message="Test commit",
        author_name="Test User",
        author_email="test@example.com",
        index_fingerprint="b" * 64,
        staged_paths=("file1.txt",),
        diff="diff content",
        diff_sha256="c" * 64,
        created_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Broker lifecycle
# ---------------------------------------------------------------------------


def test_broker_starts_empty():
    broker = GitMutationBroker()
    assert broker.pending() == []


def test_broker_register_stage_succeeds():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    assert len(broker.pending()) == 1


def test_broker_register_commit_succeeds():
    broker = GitMutationBroker()
    plan = _make_commit_plan()
    broker.register(plan)
    assert len(broker.pending()) == 1


def test_broker_get_plan_returns_none_for_unknown_id():
    broker = GitMutationBroker()
    assert broker.get_plan("unknown-id") is None


def test_broker_get_plan_returns_registered_plan():
    broker = GitMutationBroker()
    plan = _make_stage_plan("stage-123")
    broker.register(plan)
    retrieved = broker.get_plan("stage-123")
    assert retrieved is not None
    assert retrieved.id == "stage-123"


def test_broker_status_returns_none_for_unknown_id():
    broker = GitMutationBroker()
    assert broker.status("unknown") is None


def test_broker_status_returns_pending_initially():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    assert broker.status(plan.id) == STATUS_PENDING


# ---------------------------------------------------------------------------
# State machine: pending → approved → applying → applied/conflict/failed
# ---------------------------------------------------------------------------


def test_broker_approve_transitions_to_approved():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)

    broker.approve(plan.id)
    assert broker.status(plan.id) == STATUS_APPROVED


def test_broker_reject_transitions_to_rejected():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)

    broker.reject(plan.id)
    assert broker.status(plan.id) == STATUS_REJECTED


def test_broker_reject_from_approved_fails():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    broker.approve(plan.id)

    with pytest.raises(GitMutationBrokerError, match="rejected"):
        broker.reject(plan.id)


def test_broker_take_for_apply_transitions_to_applying():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    broker.approve(plan.id)

    taken = broker.take_for_apply(plan.id)
    assert taken is not None
    assert broker.status(plan.id) == STATUS_APPLYING


def test_broker_take_for_apply_fails_if_not_approved():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)

    # Still pending, not approved
    with pytest.raises(GitMutationBrokerError, match="not been approved"):
        broker.take_for_apply(plan.id)


def test_broker_take_for_apply_is_exactly_once():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    broker.approve(plan.id)

    first = broker.take_for_apply(plan.id)
    assert first is not None

    # Second attempt should fail
    with pytest.raises(GitMutationBrokerError, match="already claimed"):
        broker.take_for_apply(plan.id)


def test_broker_record_application_stores_result():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)

    result = broker.record_application(
        plan.id,
        status=STATUS_APPLIED,
        path="test.txt",
        commit_oid=None,
        message="Applied successfully",
    )

    assert result.status == STATUS_APPLIED
    assert broker.status(plan.id) == STATUS_APPLIED
    assert broker.get_result(plan.id) is not None


# ---------------------------------------------------------------------------
# Exactly-once semantics (concurrent safety)
# ---------------------------------------------------------------------------


def test_broker_concurrent_take_for_apply_only_one_succeeds():
    broker = GitMutationBroker()
    plan = _make_stage_plan()
    broker.register(plan)
    broker.approve(plan.id)

    results = []
    errors = []

    def try_take():
        try:
            broker.take_for_apply(plan.id)
            results.append(True)
        except GitMutationBrokerError:
            errors.append(True)

    threads = [threading.Thread(target=try_take) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread succeeded
    assert len(results) == 1
    assert len(errors) == 9


def test_broker_concurrent_register_all_succeed():
    broker = GitMutationBroker()
    plan_ids = []
    lock = threading.Lock()

    def register_plan(i):
        plan = _make_stage_plan(f"stage-{i:03d}", f"file{i}.txt")
        broker.register(plan)
        with lock:
            plan_ids.append(plan.id)

    threads = [threading.Thread(target=register_plan, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(plan_ids) == 10
    assert len(set(plan_ids)) == 10  # All unique


# ---------------------------------------------------------------------------
# Bounds and rejection
# ---------------------------------------------------------------------------


def test_broker_rejects_beyond_max_plans():
    broker = GitMutationBroker(max_plans=10)

    # Register 10 plans (should succeed)
    for i in range(10):
        plan = _make_stage_plan(f"stage-{i:03d}", f"file{i}.txt")
        broker.register(plan)

    # 11th should raise
    with pytest.raises(GitMutationBrokerError, match="limit"):
        broker.register(_make_stage_plan("stage-011"))


def test_broker_pending_only_returns_pending():
    broker = GitMutationBroker()

    p1 = _make_stage_plan("stage-001", "f1.txt")
    p2 = _make_stage_plan("stage-002", "f2.txt")
    p3 = _make_stage_plan("stage-003", "f3.txt")

    broker.register(p1)
    broker.register(p2)
    broker.register(p3)

    # Approve one, reject one, leave one pending
    broker.approve(p1.id)
    broker.reject(p2.id)

    pending = broker.pending()
    assert len(pending) == 1
    assert pending[0].id == p3.id


def test_broker_pending_stage_plans_filters_by_kind():
    broker = GitMutationBroker()

    stage = _make_stage_plan()
    commit = _make_commit_plan()

    broker.register(stage)
    broker.register(commit)

    stage_plans = broker.pending_stage_plans()
    assert len(stage_plans) == 1
    assert stage_plans[0].kind == KIND_STAGE


def test_broker_pending_commit_plans_filters_by_kind():
    broker = GitMutationBroker()

    stage = _make_stage_plan()
    commit = _make_commit_plan()

    broker.register(stage)
    broker.register(commit)

    commit_plans = broker.pending_commit_plans()
    assert len(commit_plans) == 1
    assert commit_plans[0].kind == KIND_COMMIT


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_two_brokers_are_isolated():
    broker1 = GitMutationBroker()
    broker2 = GitMutationBroker()

    plan1 = _make_stage_plan("stage-001")
    broker1.register(plan1)

    # broker2 cannot see broker1's plans
    assert broker2.get_plan("stage-001") is None
    assert len(broker2.pending()) == 0
