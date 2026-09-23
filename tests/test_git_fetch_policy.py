"""Tests for Git fetch policy validation.

Tests zero-network preparation and security constraints.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from harness_agent.git_fetch.policy import (
    RemoteFetchPolicyError,
    validate_and_prepare_fetch,
)


def run_git(cmd, cwd):
    """Helper to run git commands."""
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def test_repo():
    """Create a test repository with upstream configured."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # Initialize repo
        run_git(["git", "init"], repo)
        run_git(["git", "config", "user.name", "Test"], repo)
        run_git(["git", "config", "user.email", "test@example.com"], repo)

        # Create commit
        (repo / "file.txt").write_text("content")
        run_git(["git", "add", "file.txt"], repo)
        run_git(["git", "commit", "-m", "Initial"], repo)

        # Configure HTTPS remote
        run_git(["git", "remote", "add", "origin", "https://github.com/example/repo.git"], repo)

        # Create tracking ref
        head_oid = run_git(["git", "rev-parse", "HEAD"], repo).stdout.strip()
        run_git(["git", "update-ref", "refs/remotes/origin/master", head_oid], repo)

        # Set upstream
        run_git(["git", "branch", "--set-upstream-to=origin/master", "master"], repo)

        yield repo


def test_successful_prepare(test_repo):
    """Test successful fetch plan preparation."""
    plan = validate_and_prepare_fetch(
        repo_path=str(test_repo),
        summary="Test fetch",
        plan_id="test_001",
    )

    assert plan.plan_id == "test_001"
    assert plan.kind == "fetch"
    assert plan.remote_name == "origin"
    assert plan.remote_branch == "master"
    assert plan.approved_remote_url == "https://github.com/example/repo.git"
    assert plan.tracking_ref == "refs/remotes/origin/master"


def test_reject_detached_head(test_repo):
    """Test rejection when HEAD is detached."""
    run_git(["git", "checkout", "--detach"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="detached"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_002",
        )


def test_reject_no_upstream(test_repo):
    """Test rejection when branch has no upstream."""
    run_git(["git", "branch", "--unset-upstream"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="no configured upstream"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_003",
        )


def test_reject_missing_tracking_ref(test_repo):
    """Test rejection when tracking ref doesn't exist."""
    run_git(["git", "update-ref", "-d", "refs/remotes/origin/master"], test_repo)

    with pytest.raises(RemoteFetchPolicyError):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_004",
        )


def test_reject_non_https_url(test_repo):
    """Test rejection of non-HTTPS URLs."""
    run_git(["git", "remote", "set-url", "origin", "git@github.com:example/repo.git"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="HTTPS"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_005",
        )


def test_reject_http_url(test_repo):
    """Test rejection of insecure HTTP URLs."""
    run_git(["git", "remote", "set-url", "origin", "http://github.com/example/repo.git"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="HTTPS|insecure HTTP"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_006",
        )


def test_reject_file_url(test_repo):
    """Test rejection of file:// URLs."""
    run_git(["git", "remote", "set-url", "origin", "file:///tmp/repo.git"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="not supported|not HTTPS"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_007",
        )


def test_reject_embedded_credentials(test_repo):
    """Test rejection of URLs with embedded credentials."""
    run_git(["git", "remote", "set-url", "origin", "https://user:pass@github.com/example/repo.git"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="embedded credentials"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_008",
        )


def test_reject_url_with_query_string(test_repo):
    """Test rejection of URLs with query strings."""
    run_git(["git", "remote", "set-url", "origin", "https://github.com/example/repo.git?token=abc"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="query string"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_009",
        )


def test_reject_url_with_fragment(test_repo):
    """Test rejection of URLs with fragments."""
    run_git(["git", "remote", "set-url", "origin", "https://github.com/example/repo.git#main"], test_repo)

    with pytest.raises(RemoteFetchPolicyError, match="fragment"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_010",
        )


def test_reject_during_merge(test_repo):
    """Test rejection when merge is in progress."""
    # Create MERGE_HEAD to simulate merge in progress
    (test_repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")

    with pytest.raises(RemoteFetchPolicyError, match="Merge in progress"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_011",
        )


def test_reject_during_rebase(test_repo):
    """Test rejection when rebase is in progress."""
    # Create rebase-merge to simulate rebase in progress
    (test_repo / ".git" / "rebase-merge").mkdir()

    with pytest.raises(RemoteFetchPolicyError, match="Rebase in progress"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_012",
        )


def test_reject_during_cherry_pick(test_repo):
    """Test rejection when cherry-pick is in progress."""
    # Create CHERRY_PICK_HEAD to simulate cherry-pick in progress
    (test_repo / ".git" / "CHERRY_PICK_HEAD").write_text("0" * 40 + "\n")

    with pytest.raises(RemoteFetchPolicyError, match="Cherry-pick in progress"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_013",
        )


def test_reject_url_insteadof_redirection(test_repo):
    """Test rejection when url.*.insteadOf would redirect fetch."""
    # Configure insteadOf that would redirect
    run_git(
        ["git", "config", "url.https://attacker.com/.insteadOf", "https://github.com/"],
        test_repo
    )

    with pytest.raises(RemoteFetchPolicyError, match="URL redirection detected"):
        validate_and_prepare_fetch(
            repo_path=str(test_repo),
            summary="Test",
            plan_id="test_014",
        )


def test_zero_network_operation():
    """Test that prepare performs zero network operations.

    This is a documentation test - the real verification would require
    network instrumentation/mocking.
    """
    # This test documents the requirement that prepare_git_fetch
    # performs ZERO:
    # - DNS lookups
    # - TCP connections
    # - HTTP/HTTPS requests
    # - Git ls-remote
    # - Git fetch
    # - Credential helper execution

    # Actual network isolation testing would require:
    # - Process sandboxing
    # - Network namespace isolation
    # - Socket interception
    # - DNS mock

    # The smoke test verifies this by using an HTTPS URL that
    # doesn't exist and confirming prepare succeeds while apply
    # would fail if it attempted network access.
    pass
