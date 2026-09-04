"""Git remote isolation tests: config interference, credential boundary, URL redirection (§6, §7)."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.git_remote.policy import validate_and_prepare_push, RemotePushPolicyError


def _init_repo(path: Path) -> str:
    """Initialize a test repo and return HEAD OID."""
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    test_file = path / "test.txt"
    test_file.write_text("content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# HTTPS policy enforcement (§5)
# ---------------------------------------------------------------------------


def test_http_url_rejected():
    """HTTP URLs (non-HTTPS) are rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "http://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        with pytest.raises(RemotePushPolicyError, match="HTTPS"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_ssh_url_rejected():
    """SSH URLs are rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "ssh://git@github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        with pytest.raises(RemotePushPolicyError):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_git_at_url_rejected():
    """git@ URLs (SCP-style) are rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        with pytest.raises(RemotePushPolicyError):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_embedded_username_rejected():
    """URLs with embedded usernames are rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "https://user@github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        with pytest.raises(RemotePushPolicyError, match="credential"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_embedded_password_rejected():
    """URLs with embedded passwords are rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "https://user:pass@github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        with pytest.raises(RemotePushPolicyError, match="credential"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_file_url_rejected():
    """file:// URLs are rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "file:///tmp/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        with pytest.raises(RemotePushPolicyError):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


# ---------------------------------------------------------------------------
# Credential helper boundary (§7)
# ---------------------------------------------------------------------------


def test_prepare_does_not_execute_credential_helper():
    """validate_and_prepare_push does not execute credential helper."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        # Configure credential helper with marker
        marker = tmp_path / "credential_executed.marker"
        helper_script = tmp_path / "helper.sh"
        helper_script.write_text(f"#!/bin/bash\ntouch {marker}\n")
        helper_script.chmod(0o755)

        subprocess.run(
            ["git", "config", "credential.helper", str(helper_script)],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head_oid],
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

        # Add a second commit to have outgoing changes
        test_file = tmp_path / "test.txt"
        test_file.write_text("modified\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Second"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        try:
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        except Exception:
            pass

        # Credential helper marker should not exist
        assert not marker.exists()


# ---------------------------------------------------------------------------
# URL redirection rejection (§O: pushurl / insteadOf / pushInsteadOf)
# ---------------------------------------------------------------------------


def _init_pushable(path: Path) -> str:
    """Init a repo with an outgoing commit and origin tracking. Returns HEAD OID."""
    head_oid = _init_repo(path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", head_oid],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    test_file = path / "test.txt"
    test_file.write_text("outgoing\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Outgoing"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    return head_oid


def test_push_insteadof_redirect_rejected():
    """url.*.pushInsteadOf rewriting the approved URL must be rejected at prepare."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_pushable(tmp_path)

        # Attacker rewrites github.com pushes to evil.example.com
        subprocess.run(
            [
                "git", "config",
                "url.https://evil.example.com/.pushInsteadOf",
                "https://github.com/",
            ],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        with pytest.raises(RemotePushPolicyError, match="pushinsteadof|redirect|destination"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_insteadof_redirect_rejected():
    """url.*.insteadOf rewriting the approved URL must be rejected at prepare."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_pushable(tmp_path)

        subprocess.run(
            [
                "git", "config",
                "url.https://evil.example.com/.insteadOf",
                "https://github.com/",
            ],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        with pytest.raises(RemotePushPolicyError, match="insteadof|redirect|destination"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_pushurl_redirect_rejected():
    """remote.<name>.pushurl configuring a separate push destination must be rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_pushable(tmp_path)

        subprocess.run(
            [
                "git", "config",
                "remote.origin.pushurl",
                "https://evil.example.com/test/repo.git",
            ],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        with pytest.raises(RemotePushPolicyError, match="pushurl|redirect|destination"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_unrelated_insteadof_does_not_block_push():
    """An insteadOf rule that does NOT match the approved URL must not block the push."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_pushable(tmp_path)

        # Rewrite rule for an unrelated host
        subprocess.run(
            [
                "git", "config",
                "url.https://mirror.example.com/.insteadOf",
                "https://gitlab.com/",
            ],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Should succeed - the rule does not affect github.com
        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        assert plan.approved_remote_url == "https://github.com/test/repo.git"


# ---------------------------------------------------------------------------
# Credential data boundary in session state (§7)
# ---------------------------------------------------------------------------


def test_no_credential_data_in_session_state():
    """SessionState must not persist credential data."""
    from harness_agent.session import SessionState

    state = SessionState()
    state.record_git_push_prepared(
        "plan1",
        "https://github.com/test/repo.git",
        "refs/heads/main",
        "refs/heads/main",
    )
    state.record_git_push_approved("plan1", "origin", "main")

    snapshot = state.snapshot()
    snapshot_str = str(snapshot)

    # Should not contain credential data
    assert "token" not in snapshot_str.lower()
    assert "password" not in snapshot_str.lower()
    assert "authorization" not in snapshot_str.lower()
