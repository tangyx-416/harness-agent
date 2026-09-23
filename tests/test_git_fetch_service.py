"""Service-level behavior tests for Git fetch operations.

Tests actual fetch execution with disposable repositories, WITHOUT requiring
real internet access. Uses local bare repositories to simulate remote HTTPS.
"""

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
def isolated_fetch_env():
    """Create disposable local and bare repos for fetch testing.

    This simulates HTTPS remote without actual network access by using
    a local bare repo that the fetch service can access via file path.

    For testing purposes, we temporarily accept file:// URLs in the
    test environment only.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create bare repository (simulates remote server)
        bare_repo = tmpdir / "bare.git"
        bare_repo.mkdir()
        run_git(["git", "init", "--bare"], bare_repo)

        # Create initial local repository
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

        # Push to bare repo
        run_git(["git", "remote", "add", "origin", str(bare_repo)], local_repo)
        run_git(["git", "push", "origin", "master"], local_repo)

        # Set up tracking
        run_git(["git", "branch", "--set-upstream-to=origin/master", "master"], local_repo)

        yield {
            "local_repo": local_repo,
            "bare_repo": bare_repo,
            "commit_a": commit_a,
        }


def test_successful_fast_forward_fetch(isolated_fetch_env):
    """Test successful fast-forward fetch updates tracking ref.

    This is a REQUIRED behavior test per audit section 4.
    """
    local_repo = isolated_fetch_env["local_repo"]
    bare_repo = isolated_fetch_env["bare_repo"]
    commit_a = isolated_fetch_env["commit_a"]

    # Snapshot initial state
    initial_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    initial_branch = run_git(["git", "rev-parse", "master"], local_repo).stdout.strip()
    initial_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()

    # Create commit B directly in local repo and push to bare
    (local_repo / "file.txt").write_text("B")
    run_git(["git", "add", "file.txt"], local_repo)
    run_git(["git", "commit", "-m", "Commit B"], local_repo)
    commit_b = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    run_git(["git", "push"], local_repo)

    # Reset local master back to A, but leave tracking at A (simulating another repo pushed B)
    run_git(["git", "reset", "--hard", commit_a], local_repo)

    # Now tracking ref is at A, but remote (bare) is at B
    # Fetch should update tracking A→B
    current_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()

    # Update tracking ref to point to A (before the push)
    run_git(["git", "update-ref", "refs/remotes/origin/master", commit_a], local_repo)

    # Verify B is descendant of A in local repo
    merge_base = run_git(["git", "merge-base", commit_a, commit_b], local_repo).stdout.strip()
    assert merge_base == commit_a, "Commit B must be descendant of A"

    # Create fetch plan using file:// URL for testing
    # NOTE: Production policy rejects file://, but for testing we use it
    # to simulate HTTPS behavior without network access
    plan = GitFetchPlan(
        plan_id="test_ff_fetch",
        kind="fetch",
        summary="Test fast-forward fetch",
        local_branch="master",
        local_head_oid=commit_a,  # HEAD is at A
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=commit_a,  # Tracking is at A
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    # Apply fetch
    result = apply_fetch(plan, str(local_repo), broker)

    # Verify results
    assert result.state == FetchState.APPLIED, f"Expected APPLIED, got {result.state}: {result.message}"
    assert result.changed is True, "Fetch should report changed=True for fast-forward"
    assert result.observed_remote_oid == commit_b, "Should observe commit B from remote"
    assert result.updated_tracking_oid == commit_b, "Tracking ref should be updated to B"

    # Verify tracking ref actually updated
    final_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()
    assert final_tracking == commit_b, f"Tracking ref should be {commit_b}, got {final_tracking}"

    # Verify HEAD and local branch UNCHANGED
    final_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    final_branch = run_git(["git", "rev-parse", "master"], local_repo).stdout.strip()
    assert final_head == commit_a, "HEAD must not change (still at A)"
    assert final_branch == commit_a, "Local branch must not change (still at A)"

    # Verify temporary ref cleaned up
    temp_ref = f"refs/harness-agent/fetch/{plan.plan_id}"
    temp_check = run_git(["git", "rev-parse", "--verify", temp_ref], local_repo, check=False)
    assert temp_check.returncode != 0, f"Temporary ref {temp_ref} should be cleaned up"


def test_no_op_fetch_when_up_to_date(isolated_fetch_env):
    """Test no-op fetch when tracking ref already matches remote.

    This is a REQUIRED behavior test per audit section 5.
    """
    local_repo = isolated_fetch_env["local_repo"]
    bare_repo = isolated_fetch_env["bare_repo"]

    # Snapshot state (tracking already matches remote from fixture setup)
    initial_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()
    initial_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()

    # Create fetch plan
    plan = GitFetchPlan(
        plan_id="test_noop_fetch",
        kind="fetch",
        summary="Test no-op fetch",
        local_branch="master",
        local_head_oid=initial_head,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=initial_tracking,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    # Apply fetch
    result = apply_fetch(plan, str(local_repo), broker)

    # Verify no-op
    assert result.state == FetchState.APPLIED, f"Expected APPLIED, got {result.state}: {result.message}"
    assert result.changed is False, "Fetch should report changed=False when already up-to-date"
    assert result.observed_remote_oid == initial_tracking, "Remote should match tracking"
    assert result.updated_tracking_oid == initial_tracking, "Tracking should remain unchanged"

    # Verify tracking ref unchanged
    final_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()
    assert final_tracking == initial_tracking, "Tracking ref should not change"

    # Verify temporary ref cleaned
    temp_ref = f"refs/harness-agent/fetch/{plan.plan_id}"
    temp_check = run_git(["git", "rev-parse", "--verify", temp_ref], local_repo, check=False)
    assert temp_check.returncode != 0, f"Temporary ref {temp_ref} should be cleaned up"


def test_fetch_head_preservation(isolated_fetch_env):
    """Test that FETCH_HEAD is preserved during fetch.

    This is a REQUIRED behavior test per audit section 9.
    """
    local_repo = isolated_fetch_env["local_repo"]
    bare_repo = isolated_fetch_env["bare_repo"]

    # Create known FETCH_HEAD content
    fetch_head_path = local_repo / ".git" / "FETCH_HEAD"
    original_content = "test_marker_content_12345\n"
    fetch_head_path.write_text(original_content)

    # Snapshot state
    initial_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    initial_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()

    # Create fetch plan
    plan = GitFetchPlan(
        plan_id="test_fetch_head",
        kind="fetch",
        summary="Test FETCH_HEAD preservation",
        local_branch="master",
        local_head_oid=initial_head,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=initial_tracking,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    # Apply fetch
    result = apply_fetch(plan, str(local_repo), broker)

    assert result.state == FetchState.APPLIED, f"Fetch should succeed: {result.message}"

    # Verify FETCH_HEAD preserved
    final_content = fetch_head_path.read_text()
    assert final_content == original_content, "FETCH_HEAD must be byte-identical after fetch"


def test_dirty_workspace_preservation(isolated_fetch_env):
    """Test that dirty workspace is preserved during fetch.

    This is a REQUIRED behavior test per audit section 8.
    """
    local_repo = isolated_fetch_env["local_repo"]
    bare_repo = isolated_fetch_env["bare_repo"]

    # Create dirty workspace with:
    # 1. Tracked unstaged modification
    # 2. Staged modification
    # 3. Untracked file

    # Tracked unstaged modification
    (local_repo / "file.txt").write_text("MODIFIED_UNSTAGED")

    # Staged modification
    (local_repo / "staged.txt").write_text("STAGED_CONTENT")
    run_git(["git", "add", "staged.txt"], local_repo)

    # Untracked file
    (local_repo / "untracked.txt").write_text("UNTRACKED_CONTENT")

    # Snapshot all state
    initial_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    initial_branch = run_git(["git", "rev-parse", "master"], local_repo).stdout.strip()
    initial_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()
    initial_worktree_bytes = (local_repo / "file.txt").read_text()
    initial_untracked_bytes = (local_repo / "untracked.txt").read_text()
    initial_staged_bytes = (local_repo / "staged.txt").read_text()

    # Create fetch plan
    plan = GitFetchPlan(
        plan_id="test_dirty_ws",
        kind="fetch",
        summary="Test workspace preservation",
        local_branch="master",
        local_head_oid=initial_head,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=initial_tracking,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    # Apply fetch
    result = apply_fetch(plan, str(local_repo), broker)

    assert result.state == FetchState.APPLIED, f"Fetch should succeed: {result.message}"

    # Verify ALL workspace state preserved
    final_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    final_branch = run_git(["git", "rev-parse", "master"], local_repo).stdout.strip()
    final_worktree_bytes = (local_repo / "file.txt").read_text()
    final_untracked_bytes = (local_repo / "untracked.txt").read_text()
    final_staged_bytes = (local_repo / "staged.txt").read_text()

    assert final_head == initial_head, "HEAD must be identical"
    assert final_branch == initial_branch, "Local branch must be identical"
    assert final_worktree_bytes == initial_worktree_bytes, "Working tree must be identical"
    assert final_untracked_bytes == initial_untracked_bytes, "Untracked file must be identical"
    assert final_staged_bytes == initial_staged_bytes, "Staged file must be identical"

    # Verify git status shows same dirty state
    status_output = run_git(["git", "status", "--short"], local_repo).stdout
    assert "M file.txt" in status_output, "Unstaged modification should remain"
    assert "A  staged.txt" in status_output, "Staged file should remain"
    assert "?? untracked.txt" in status_output, "Untracked file should remain"


def test_temp_ref_cleanup_on_success(isolated_fetch_env):
    """Test temporary ref cleanup after successful fetch.

    This is a REQUIRED behavior test per audit section 15.
    """
    local_repo = isolated_fetch_env["local_repo"]
    bare_repo = isolated_fetch_env["bare_repo"]

    initial_head = run_git(["git", "rev-parse", "HEAD"], local_repo).stdout.strip()
    initial_tracking = run_git(["git", "rev-parse", "refs/remotes/origin/master"], local_repo).stdout.strip()

    plan = GitFetchPlan(
        plan_id="test_cleanup_success",
        kind="fetch",
        summary="Test cleanup on success",
        local_branch="master",
        local_head_oid=initial_head,
        remote_name="origin",
        remote_branch="master",
        tracking_ref="refs/remotes/origin/master",
        expected_tracking_oid=initial_tracking,
        approved_remote_url=f"file://{bare_repo}",
        remote_host="localhost",
        created_at=datetime.now(timezone.utc),
    )

    broker = GitFetchBroker()
    broker.register(plan)
    broker.approve(plan.plan_id)

    result = apply_fetch(plan, str(local_repo), broker)
    assert result.state == FetchState.APPLIED

    # Verify temp ref cleaned
    temp_ref = f"refs/harness-agent/fetch/{plan.plan_id}"
    check = run_git(["git", "rev-parse", "--verify", temp_ref], local_repo, check=False)
    assert check.returncode != 0, f"Temporary ref {temp_ref} should be cleaned up"

    # Verify no other harness-agent refs remain
    all_refs = run_git(["git", "for-each-ref", "refs/harness-agent/"], local_repo).stdout
    assert all_refs.strip() == "", "No harness-agent refs should remain"
