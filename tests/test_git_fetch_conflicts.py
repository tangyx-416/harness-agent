"""Additional service-level behavior tests for conflict scenarios and edge cases."""

import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness_agent.git_fetch.broker import GitFetchBroker
from harness_agent.git_fetch.models import FetchState, GitFetchPlan
from harness_agent.git_fetch.service import apply_fetch


def run_git(cmd, cwd, check=True):
    """Helper to run git commands."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )
    return result


@pytest.fixture
def conflict_fetch_env():
    """Create environment for testing conflict scenarios."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create bare repository
        bare_repo = tmpdir / "bare.git"
        bare_repo.mkdir()
        run_git(["git", "init", "--bare"], bare_repo)

        # Create local repository
        local_repo = tmpdir / "local"
        local_repo.mkdir()
        run_git(["git", "init"], local_repo)
        run_git(["git", "config", "user.name", "Test"], local_repo)
        run_git(["git", "config", "user.email", "test@example.com"], local_repo)

        # Create initial commit A
        (local_repo / "file.txt").write_text("A")
        run_git(["git", "add", "file.txt"], local_repo)
        run_git(["git", "commit", "-m", "Commit A"], local_repo)
        commit_a = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()

        # Push to bare and set up tracking
        run_git(["git", "remote", "add", "origin", str(bare_repo)], local_repo)
        run_git(["git", "push", "origin", "master"], local_repo)
        run_git(["git", "branch", "--set-upstream-to=origin/master", "master"], local_repo)

        yield {
            "local_repo": local_repo,
            "bare_repo": bare_repo,
            "commit_a": commit_a,
        }


def test_remote_history_rewrite_conflict(conflict_fetch_env):
    """Test that non-fast-forward remote history is rejected.

    This is a REQUIRED behavior test per audit section 6.
    """
    local_repo = conflict_fetch_env["local_repo"]
    bare_repo = conflict_fetch_env["bare_repo"]
    commit_a = conflict_fetch_env["commit_a"]

    # Create divergent commit C (rewrite history)
    # First, create commit B on top of A
    (local_repo / "file.txt").write_text("B")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit B"], local_repo)
    run_git(["git", "push"], local_repo)

    # Reset to A and create divergent commit C
    run_git(["git", "reset", "--hard", commit_a], local_repo)
    (local_repo / "file.txt").write_text("C")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit C (divergent)"], local_repo)
    commit_c = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    run_git(["git", "push", "--force"], local_repo)

    # Now tracking points to B, but remote has C (not ancestor of B)
    # Update tracking to point to B
    (local_repo / "file.txt").write_text("B")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "--amend", "-m", "Commit B"], local_repo)
    commit_b = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()

    # Manually set tracking ref to B
    run_git(["git", "update-ref", "refs/remotes/origin/master", commit_b], local_repo)

    # Reset HEAD back to A
    run_git(["git", "reset", "--hard", commit_a], local_repo)

    # Now: tracking=B, remote=C (divergent), HEAD=A
    tracking_oid = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()

    plan = GitFetchPlan(
        plan_id="test_history_rewrite",
        kind="fetch",
        summary="Test history rewrite rejection",
        local_branch="master",
        local_head_oid=commit_a,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=tracking_oid,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    result = apply_fetch(plan, str(local_repo), broker)

    # Verify conflict detected
    assert result.state == FetchState.CONFLICT, f"Expected CONFLICT for non-fast-forward, got {result.state}"
    assert "diverged" in result.message.lower() or "not a fast-forward" in result.message.lower() or "not an ancestor" in result.message.lower()

    # Verify tracking ref unchanged
    final_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()
    assert final_tracking == tracking_oid, "Tracking ref must not change on conflict"

    # Verify HEAD unchanged
    final_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    assert final_head == commit_a, "HEAD must not change"


def test_tracking_cas_race_condition(conflict_fetch_env):
    """Test that tracking ref CAS prevents race conditions.

    This is a REQUIRED behavior test per audit section 7.
    """
    local_repo = conflict_fetch_env["local_repo"]
    bare_repo = conflict_fetch_env["bare_repo"]
    commit_a = conflict_fetch_env["commit_a"]

    # Create commit B and push
    (local_repo / "file.txt").write_text("B")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit B"], local_repo)
    commit_b = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    run_git(["git", "push"], local_repo)

    # Reset HEAD to A, tracking also at A
    run_git(["git", "reset", "--hard", commit_a], local_repo)
    run_git(["git", "update-ref", "refs/remotes/origin/master", commit_a], local_repo)

    # Create plan expecting tracking=A, will fetch B
    plan = GitFetchPlan(
        plan_id="test_cas_race",
        kind="fetch",
        summary="Test CAS race",
        local_branch="master",
        local_head_oid=commit_a,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=commit_a,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    # Simulate race: another actor changes tracking A→C before we apply
    # Create commit C
    (local_repo / "other.txt").write_text("C")
    run_git(["git", "add", "other.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit C"], local_repo)
    commit_c = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()

    # Update tracking to C (simulating another fetch)
    run_git(["git", "update-ref", "refs/remotes/origin/master", commit_c], local_repo)

    # Reset HEAD back to A
    run_git(["git", "reset", "--hard", commit_a], local_repo)

    # Now apply the fetch that expects tracking=A
    result = apply_fetch(plan, str(local_repo), broker)

    # Verify CAS conflict detected
    assert result.state == FetchState.CONFLICT, f"Expected CONFLICT for CAS race, got {result.state}"
    assert "changed" in result.message.lower() or "tracking" in result.message.lower()

    # Verify tracking still at C (not updated to B)
    final_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()
    assert final_tracking == commit_c, f"Tracking should remain at C, got {final_tracking}"


def test_apply_time_revalidation_detects_head_change(conflict_fetch_env):
    """Test that apply-time revalidation detects HEAD changes.

    This is part of audit section 19.
    """
    local_repo = conflict_fetch_env["local_repo"]
    bare_repo = conflict_fetch_env["bare_repo"]
    commit_a = conflict_fetch_env["commit_a"]

    # Create commit B
    (local_repo / "file.txt").write_text("B")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit B"], local_repo)
    commit_b = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()

    # Create plan with HEAD=A
    plan = GitFetchPlan(
        plan_id="test_revalidation",
        kind="fetch",
        summary="Test revalidation",
        local_branch="master",
        local_head_oid=commit_a,  # Plan expects A
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=commit_a,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    # But HEAD is actually at B now (changed after approval)
    # Apply should detect this and reject

    result = apply_fetch(plan, str(local_repo), broker)

    # Verify revalidation caught the change
    assert result.state == FetchState.CONFLICT, f"Expected CONFLICT for HEAD change, got {result.state}"
    assert "HEAD changed" in result.message or "changed from" in result.message


def test_exactly_one_network_attempt(conflict_fetch_env):
    """Test that only one network fetch attempt occurs.

    This is a REQUIRED behavior test per audit section 20.
    """
    local_repo = conflict_fetch_env["local_repo"]
    bare_repo = conflict_fetch_env["bare_repo"]
    commit_a = conflict_fetch_env["commit_a"]

    # Create invalid remote URL to cause fetch failure
    run_git(["git", "remote", "set-url", "origin", "https://invalid.example.com/repo.git"], local_repo)

    plan = GitFetchPlan(
        plan_id="test_one_attempt",
        kind="fetch",
        summary="Test single attempt",
        local_branch="master",
        local_head_oid=commit_a,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=commit_a,
        approved_remote_url="https://invalid.example.com/repo.git",
        remote_host="invalid.example.com",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    result = apply_fetch(plan, str(local_repo), broker)

    # Should fail (network error or timeout)
    assert result.state == FetchState.FAILED

    # Attempting to apply again should return cached result, not retry
    result2 = apply_fetch(plan, str(local_repo), broker)
    assert result2.state == FetchState.FAILED
    assert result2.message == result.message  # Same cached result


def test_temp_ref_cleanup_after_conflict(conflict_fetch_env):
    """Test temporary ref cleanup after various conflict scenarios.

    This is part of audit section 13.
    """
    local_repo = conflict_fetch_env["local_repo"]
    bare_repo = conflict_fetch_env["bare_repo"]
    commit_a = conflict_fetch_env["commit_a"]

    # Cause a revalidation conflict by changing HEAD
    (local_repo / "file.txt").write_text("B")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit B"], local_repo)
    commit_b = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()

    plan = GitFetchPlan(
        plan_id="test_cleanup_conflict",
        kind="fetch",
        summary="Test cleanup on conflict",
        local_branch="master",
        local_head_oid=commit_a,  # Expects A, but we're at B
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=commit_a,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    result = apply_fetch(plan, str(local_repo), broker)
    assert result.state == FetchState.CONFLICT

    # Verify temp ref cleaned (should not exist since fetch never happened)
    temp_ref = f"refs/harness-agent/fetch/{plan.plan_id}"
    check = run_git(["git", "rev-parse", "--verify", temp_ref], local_repo, check=False)
    assert check.returncode != 0, f"Temporary ref {temp_ref} should not exist after conflict"

    # Verify no harness-agent refs remain
    all_refs = run_git(["git", "for-each-ref", "refs/harness-agent/"], local_repo).stdout
    assert all_refs.strip() == "", "No harness-agent refs should remain"
