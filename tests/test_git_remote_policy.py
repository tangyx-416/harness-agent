"""Tests for Git remote push policy validation."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from harness_agent.git_remote.policy import (
    MAX_PUSH_COMMITS,
    RemotePushPolicyError,
    validate_and_prepare_push,
)


def _init_test_repo(tmp_path: Path):
    """Initialize a test Git repository with a commit using subprocess."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Create initial commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("initial content\n")

    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )


def test_push_policy_requires_attached_head(tmp_path):
    """Detached HEAD is rejected."""
    _init_test_repo(tmp_path)

    # Get current HEAD
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    head_oid = result.stdout.strip()

    # Detach HEAD
    subprocess.run(
        ["git", "checkout", head_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    with pytest.raises(RemotePushPolicyError, match="HEAD is detached"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_requires_upstream(tmp_path):
    """Branch without upstream is rejected."""
    _init_test_repo(tmp_path)

    # No upstream configured
    with pytest.raises(RemotePushPolicyError, match="no configured upstream"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_requires_commits_ahead(tmp_path):
    """Up-to-date branch (nothing to push) is rejected."""
    _init_test_repo(tmp_path)

    # Add remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Get current HEAD
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    head_oid = result.stdout.strip()

    # Create tracking branch at same commit
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", head_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Set upstream
    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    with pytest.raises(RemotePushPolicyError, match="nothing to push"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_rejects_https_with_embedded_credentials(tmp_path):
    """HTTPS URL with embedded credentials is rejected."""
    _init_test_repo(tmp_path)

    # Add remote with embedded credentials
    subprocess.run(
        ["git", "remote", "add", "origin", "https://user:pass@github.com/test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Get parent commit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    # Create tracking branch
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Set upstream
    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add a new commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    with pytest.raises(RemotePushPolicyError, match="embedded credentials"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_rejects_ssh_url(tmp_path):
    """SSH URL is rejected (HTTPS only in v0.8)."""
    _init_test_repo(tmp_path)

    # Add SSH remote
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    with pytest.raises(RemotePushPolicyError, match="HTTPS.*unsupported scheme"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_rejects_file_url(tmp_path):
    """file:// URL is rejected."""
    _init_test_repo(tmp_path)

    subprocess.run(
        ["git", "remote", "add", "origin", "file:///tmp/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    with pytest.raises(RemotePushPolicyError, match="scheme not supported"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_rejects_url_with_query(tmp_path):
    """URL with query string is rejected."""
    _init_test_repo(tmp_path)

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git?param=value"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    with pytest.raises(RemotePushPolicyError, match="query string"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_rejects_uncommitted_changes(tmp_path):
    """Uncommitted changes block push."""
    _init_test_repo(tmp_path)

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Modify working tree without committing
    test_file.write_text("uncommitted change\n")

    with pytest.raises(RemotePushPolicyError, match="uncommitted changes"):
        validate_and_prepare_push(str(tmp_path), "Test push", "push-123")


def test_push_policy_allows_untracked_files(tmp_path):
    """Untracked files do not block push."""
    _init_test_repo(tmp_path)

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add untracked file
    untracked = tmp_path / "untracked.txt"
    untracked.write_text("untracked content\n")

    # Should not raise
    plan = validate_and_prepare_push(str(tmp_path), "Test push", "push-123")
    assert plan is not None


def test_push_policy_successful_validation(tmp_path):
    """Valid push creates complete plan."""
    _init_test_repo(tmp_path)

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent_oid = result.stdout.strip()

    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent_oid],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Add commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    new_commit = result.stdout.strip()

    plan = validate_and_prepare_push(str(tmp_path), "Test push", "push-123")

    assert plan.plan_id == "push-123"
    assert plan.kind == "push"
    assert plan.summary == "Test push"
    assert plan.local_branch in ("main", "master")  # Accept either default branch name
    assert plan.remote_name == "origin"
    assert plan.remote_branch in ("main", "master")
    assert plan.approved_remote_url == "https://github.com/test/repo.git"
    assert plan.remote_host == "github.com"
    assert plan.head_oid == new_commit
    assert plan.expected_remote_oid == parent_oid
    assert plan.commit_count == 1
    assert len(plan.outgoing_commits) == 1
    assert plan.outgoing_commits[0].subject == "Second commit"
    assert len(plan.diff) > 0
    assert len(plan.diff_sha256) == 64
