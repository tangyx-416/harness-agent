"""Tests for Git remote push data models."""

from datetime import datetime, timezone

import pytest

from harness_agent.git_remote.models import GitPushPlan, GitPushResult, PushCommit, PushState


def test_push_state_enum():
    """Verify PushState enum values."""
    assert PushState.PENDING == "pending"
    assert PushState.APPROVED == "approved"
    assert PushState.APPLYING == "applying"
    assert PushState.APPLIED == "applied"
    assert PushState.REJECTED == "rejected"
    assert PushState.CONFLICT == "conflict"
    assert PushState.FAILED == "failed"


def test_push_commit_frozen():
    """PushCommit is immutable."""
    commit = PushCommit(oid="abc123", short_oid="abc", subject="Test commit")
    with pytest.raises(Exception):
        commit.oid = "def456"


def test_git_push_plan_frozen():
    """GitPushPlan is immutable."""
    plan = GitPushPlan(
        plan_id="push-123",
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
            PushCommit(oid="e" * 40, short_oid="e" * 7, subject="Commit 1"),
        ),
        commit_count=1,
        diff="diff --git a/test.txt b/test.txt\n",
        diff_sha256="f" * 64,
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(Exception):
        plan.summary = "Modified"


def test_git_push_plan_requires_push_kind():
    """GitPushPlan kind must be 'push'."""
    with pytest.raises(ValueError, match="kind must be 'push'"):
        GitPushPlan(
            plan_id="push-123",
            kind="invalid",
            summary="Test",
            local_branch="main",
            head_oid="a" * 40,
            head_tree_oid="b" * 40,
            remote_name="origin",
            remote_branch="main",
            approved_remote_url="https://github.com/test/repo.git",
            remote_host="github.com",
            expected_remote_oid="c" * 40,
            expected_tree_oid="d" * 40,
            outgoing_commits=(),
            commit_count=0,
            diff="",
            diff_sha256="",
            created_at=datetime.now(timezone.utc),
        )


def test_git_push_plan_requires_tuple_commits():
    """GitPushPlan outgoing_commits must be a tuple."""
    with pytest.raises(ValueError, match="must be a tuple"):
        GitPushPlan(
            plan_id="push-123",
            kind="push",
            summary="Test",
            local_branch="main",
            head_oid="a" * 40,
            head_tree_oid="b" * 40,
            remote_name="origin",
            remote_branch="main",
            approved_remote_url="https://github.com/test/repo.git",
            remote_host="github.com",
            expected_remote_oid="c" * 40,
            expected_tree_oid="d" * 40,
            outgoing_commits=[],  # list instead of tuple
            commit_count=0,
            diff="",
            diff_sha256="",
            created_at=datetime.now(timezone.utc),
        )


def test_git_push_plan_commit_count_must_match():
    """GitPushPlan commit_count must match outgoing_commits length."""
    with pytest.raises(ValueError, match="commit_count.*does not match"):
        GitPushPlan(
            plan_id="push-123",
            kind="push",
            summary="Test",
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
                PushCommit(oid="e" * 40, short_oid="e" * 7, subject="Commit 1"),
            ),
            commit_count=2,  # Mismatch
            diff="",
            diff_sha256="",
            created_at=datetime.now(timezone.utc),
        )


def test_git_push_result_frozen():
    """GitPushResult is immutable."""
    result = GitPushResult(
        plan_id="push-123",
        state=PushState.APPLIED,
        remote_name="origin",
        remote_branch="main",
        head_oid="a" * 40,
        remote_oid_after="a" * 40,
        message="Success",
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(Exception):
        result.message = "Modified"


def test_git_push_result_allows_none_remote_oid():
    """GitPushResult remote_oid_after can be None."""
    result = GitPushResult(
        plan_id="push-123",
        state=PushState.FAILED,
        remote_name="origin",
        remote_branch="main",
        head_oid="a" * 40,
        remote_oid_after=None,
        message="Failed",
        created_at=datetime.now(timezone.utc),
    )

    assert result.remote_oid_after is None
