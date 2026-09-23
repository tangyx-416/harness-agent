"""Tests for Git fetch models."""

from datetime import datetime, timezone

import pytest

from harness_agent.git_fetch.models import FetchState, GitFetchPlan, GitFetchResult


def test_fetch_state_enum():
    """Test FetchState enum values."""
    assert FetchState.PENDING.value == "pending"
    assert FetchState.APPROVED.value == "approved"
    assert FetchState.APPLYING.value == "applying"
    assert FetchState.APPLIED.value == "applied"
    assert FetchState.REJECTED.value == "rejected"
    assert FetchState.CONFLICT.value == "conflict"
    assert FetchState.FAILED.value == "failed"


def test_git_fetch_plan_immutable():
    """Test GitFetchPlan is immutable."""
    plan = GitFetchPlan(
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

    # Verify immutability
    with pytest.raises(AttributeError):
        plan.remote_name = "upstream"


def test_git_fetch_plan_validates_kind():
    """Test GitFetchPlan validates kind field."""
    with pytest.raises(ValueError, match="kind must be 'fetch'"):
        GitFetchPlan(
            plan_id="fetch_test123",
            kind="push",  # Wrong kind
            summary="Test",
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


def test_git_fetch_plan_validates_tracking_ref():
    """Test GitFetchPlan validates tracking_ref is a remote-tracking ref."""
    with pytest.raises(ValueError, match="must be a remote-tracking ref"):
        GitFetchPlan(
            plan_id="fetch_test123",
            kind="fetch",
            summary="Test",
            local_branch="main",
            local_head_oid="a" * 40,
            remote_name="origin",
            remote_branch="main",
            tracking_ref="refs/heads/main",  # Not a remote-tracking ref
            expected_tracking_oid="b" * 40,
            approved_remote_url="https://github.com/example/repo.git",
            remote_host="github.com",
            created_at=datetime.now(timezone.utc),
        )


def test_git_fetch_result_immutable():
    """Test GitFetchResult is immutable."""
    result = GitFetchResult(
        plan_id="fetch_test123",
        state=FetchState.APPLIED,
        remote_name="origin",
        remote_branch="main",
        tracking_ref="refs/remotes/origin/main",
        previous_tracking_oid="a" * 40,
        observed_remote_oid="b" * 40,
        updated_tracking_oid="b" * 40,
        changed=True,
        message="Fetch successful",
        created_at=datetime.now(timezone.utc),
    )

    # Verify immutability
    with pytest.raises(AttributeError):
        result.state = FetchState.FAILED


def test_git_fetch_result_no_op():
    """Test GitFetchResult for no-op fetch."""
    result = GitFetchResult(
        plan_id="fetch_test123",
        state=FetchState.APPLIED,
        remote_name="origin",
        remote_branch="main",
        tracking_ref="refs/remotes/origin/main",
        previous_tracking_oid="a" * 40,
        observed_remote_oid="a" * 40,
        updated_tracking_oid="a" * 40,
        changed=False,
        message="Already up-to-date",
        created_at=datetime.now(timezone.utc),
    )

    assert not result.changed
    assert result.state == FetchState.APPLIED
    assert result.previous_tracking_oid == result.updated_tracking_oid


def test_git_fetch_result_conflict():
    """Test GitFetchResult for conflict state."""
    result = GitFetchResult(
        plan_id="fetch_test123",
        state=FetchState.CONFLICT,
        remote_name="origin",
        remote_branch="main",
        tracking_ref="refs/remotes/origin/main",
        previous_tracking_oid="a" * 40,
        observed_remote_oid="c" * 40,
        updated_tracking_oid=None,
        changed=False,
        message="Remote branch diverged",
        created_at=datetime.now(timezone.utc),
    )

    assert result.state == FetchState.CONFLICT
    assert not result.changed
    assert result.updated_tracking_oid is None
