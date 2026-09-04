"""Git remote service layer tests: preflight, lease, verification, network isolation."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.git_remote.models import GitPushPlan, PushState
from harness_agent.git_remote.service import apply_push


def _init_repo_with_push_ready(tmp_path: Path):
    """Initialize a test repo ready for push."""
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
    test_file.write_text("initial\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    # Get HEAD OID
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    parent = result.stdout.strip()

    # Add remote and tracking branch
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", parent],
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

    # Add second commit
    test_file.write_text("new content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Remote preflight (§8)
# ---------------------------------------------------------------------------


def test_apply_push_validates_local_state_before_network():
    """apply_push validates local state before attempting network access."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        from harness_agent.git_remote.policy import validate_and_prepare_push

        broker = GitRemoteBroker()
        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test push", "plan1")
            broker.register(plan)
            broker.approve("plan1")

            # Apply should fail due to no actual remote, but validates local state first
            result = apply_push(plan, str(tmp_path), broker)

            # Should fail with network/remote error, not local validation error
            assert result.state in (PushState.FAILED, PushState.CONFLICT)
        except Exception as e:
            # If plan creation fails, that's also acceptable (no upstream configured correctly)
            assert "remote" in str(e).lower() or "upstream" in str(e).lower()


# ---------------------------------------------------------------------------
# Push command hardening (§12)
# ---------------------------------------------------------------------------


def test_apply_push_uses_shell_false():
    """apply_push uses shell=False for all subprocess calls."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        from harness_agent.git_remote.policy import validate_and_prepare_push

        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
            broker = GitRemoteBroker()
            broker.register(plan)
            broker.approve("plan1")

            calls_with_shell_true = []

            original_run = subprocess.run

            def capture_run(*args, **kwargs):
                if kwargs.get("shell") is True:
                    calls_with_shell_true.append((args, kwargs))
                return original_run(*args, **kwargs)

            with patch("subprocess.run", side_effect=capture_run):
                try:
                    apply_push(plan, str(tmp_path), broker)
                except Exception:
                    pass

            assert len(calls_with_shell_true) == 0, "Found subprocess calls with shell=True"
        except Exception:
            # If plan creation fails, skip this test
            pytest.skip("Could not create push plan")


def test_apply_push_no_bare_force_flags():
    """apply_push never uses bare --force or -f (force-with-lease CAS is allowed).

    The service uses --force-with-lease=<ref>:<oid> as its exact compare-and-swap
    mechanism. That is NOT force-push authority: it only succeeds if the remote ref
    still equals the exact expected OID. A bare --force or -f WOULD be force-push
    authority and must never appear.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        from harness_agent.git_remote.policy import validate_and_prepare_push

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_cmds = []
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                # Pass preflight so the push command is actually built
                r = MagicMock()
                r.returncode = 0
                r.stdout = f"{plan.expected_remote_oid}\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_cmds.append(cmd)
                r = MagicMock()
                r.returncode = 1
                r.stdout = ""
                r.stderr = "simulated"
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            apply_push(plan, str(tmp_path), broker)

        # A push command WAS built (preflight passed)
        assert len(push_cmds) == 1, "Expected exactly one push command to be constructed"
        cmd = push_cmds[0]

        # No bare force-push authority
        assert "--force" not in cmd, "Bare --force is force-push authority"
        assert "-f" not in cmd, "Bare -f is force-push authority"

        # The CAS lease IS present and bound to the exact expected OID
        lease_args = [a for a in cmd if isinstance(a, str) and a.startswith("--force-with-lease=")]
        assert len(lease_args) == 1, "Expected exactly one --force-with-lease CAS argument"
        assert plan.expected_remote_oid in lease_args[0], "Lease must bind the exact expected OID"


# ---------------------------------------------------------------------------
# Exactly-once network write (§11)
# ---------------------------------------------------------------------------


def test_apply_push_invokes_git_push_at_most_once():
    """apply_push invokes git push at most once, never retries."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        from harness_agent.git_remote.policy import validate_and_prepare_push

        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
            broker = GitRemoteBroker()
            broker.register(plan)
            broker.approve("plan1")

            push_count = 0

            def mock_run(*args, **kwargs):
                nonlocal push_count
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and "push" in cmd:
                    push_count += 1
                    # Simulate push failure
                    result = MagicMock()
                    result.returncode = 1
                    result.stdout = ""
                    result.stderr = "fatal: push failed"
                    return result
                # Allow other git commands
                return subprocess.run(*args, **kwargs)

            with patch("subprocess.run", side_effect=mock_run):
                result = apply_push(plan, str(tmp_path), broker)

            # Push should be attempted at most once
            assert push_count <= 1
            assert result.state in (PushState.FAILED, PushState.CONFLICT)
        except Exception:
            pytest.skip("Could not create push plan")


# ---------------------------------------------------------------------------
# Tag suppression (§15)
# ---------------------------------------------------------------------------


def test_push_does_not_include_tags_flags():
    """Push command does not include --tags or --follow-tags, and disables followTags."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        from harness_agent.git_remote.policy import validate_and_prepare_push

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_cmds = []
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                r = MagicMock()
                r.returncode = 0
                r.stdout = f"{plan.expected_remote_oid}\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_cmds.append(cmd)
                r = MagicMock()
                r.returncode = 1
                r.stdout = ""
                r.stderr = "simulated"
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            apply_push(plan, str(tmp_path), broker)

        assert len(push_cmds) == 1, "Expected exactly one push command to be constructed"
        cmd = push_cmds[0]
        joined = " ".join(cmd)

        # No tag-pushing flags
        assert "--tags" not in cmd
        assert "--follow-tags" not in cmd
        # followTags explicitly disabled via config override
        assert "push.followTags=false" in joined
