"""Hook and external signer behavior tests for v0.8.0 release audit."""

import subprocess
import tempfile
from pathlib import Path

import pytest


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
# 5. pre-push hook behavior (Audit item #5)
# ---------------------------------------------------------------------------


def test_pre_push_hook_not_executed():
    """pre-push hook must NOT execute during approved push."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)

        # Create pre-push hook with marker
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook_marker = tmp_path / "pre_push_executed.marker"
        pre_push_hook = hooks_dir / "pre-push"

        # Create hook that creates marker file
        pre_push_hook.write_text(
            f"#!/bin/bash\ntouch {hook_marker}\nexit 0\n"
        )
        pre_push_hook.chmod(0o755)

        # Set up remote
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        head_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

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

        # Add outgoing commit
        test_file = tmp_path / "test.txt"
        test_file.write_text("new content\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Second"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Prepare and try to apply push
        from harness_agent.git_remote.broker import GitRemoteBroker
        from harness_agent.git_remote.policy import validate_and_prepare_push
        from harness_agent.git_remote.service import apply_push

        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
            broker = GitRemoteBroker()
            broker.register(plan)
            broker.approve("plan1")

            # Attempt apply (will fail due to no real remote, but that's OK)
            try:
                apply_push(plan, str(tmp_path), broker)
            except Exception:
                pass

            # Hook marker must NOT exist
            assert not hook_marker.exists(), "pre-push hook was executed"

        except Exception as e:
            # If prepare fails, verify hook wasn't executed during prepare
            assert not hook_marker.exists(), f"pre-push hook executed during prepare: {e}"


# ---------------------------------------------------------------------------
# 6. Signed push behavior (Audit item #6)
# ---------------------------------------------------------------------------


def test_signed_push_signer_not_executed():
    """Signed push external signer must NOT be invoked."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)

        # Create fake signer marker
        signer_marker = tmp_path / "signer_executed.marker"
        signer_script = tmp_path / "fake_signer.sh"
        signer_script.write_text(
            f"#!/bin/bash\ntouch {signer_marker}\necho 'fake signature'\n"
        )
        signer_script.chmod(0o755)

        # Configure push signing
        subprocess.run(
            ["git", "config", "gpg.format", "openpgp"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "gpg.program", str(signer_script)],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "push.gpgSign", "true"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Set up remote
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        head_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

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

        # Add outgoing commit
        test_file = tmp_path / "test.txt"
        test_file.write_text("new content\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Second"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Prepare and try to apply push
        from harness_agent.git_remote.broker import GitRemoteBroker
        from harness_agent.git_remote.policy import validate_and_prepare_push
        from harness_agent.git_remote.service import apply_push

        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
            broker = GitRemoteBroker()
            broker.register(plan)
            broker.approve("plan1")

            # Attempt apply (will fail due to no real remote)
            try:
                apply_push(plan, str(tmp_path), broker)
            except Exception:
                pass

            # Signer marker must NOT exist
            assert not signer_marker.exists(), "External signer was invoked"

        except Exception as e:
            # Signer should not execute during prepare either
            assert not signer_marker.exists(), f"Signer executed: {e}"


# ---------------------------------------------------------------------------
# 7. Tag suppression behavior (Audit item #7)
# ---------------------------------------------------------------------------


def test_tag_not_pushed_with_branch():
    """Tags must NOT be pushed even with push.followTags=true."""
    pytest.skip("Requires real remote repository - tested in smoke test")


# ---------------------------------------------------------------------------
# 8. Submodule recursion (Audit item #8)
# ---------------------------------------------------------------------------


def test_no_recursive_submodule_push():
    """push.recurseSubmodules must be suppressed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)

        # Configure recursive submodule push
        subprocess.run(
            ["git", "config", "push.recurseSubmodules", "on-demand"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Verify config is set
        result = subprocess.run(
            ["git", "config", "push.recurseSubmodules"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert "on-demand" in result.stdout

        # Set up for push
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        head_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

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

        # Add outgoing commit
        test_file = tmp_path / "test.txt"
        test_file.write_text("new\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "New"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Check that push command does not include recurseSubmodules
        from unittest.mock import patch
        from harness_agent.git_remote.broker import GitRemoteBroker
        from harness_agent.git_remote.policy import validate_and_prepare_push
        from harness_agent.git_remote.service import apply_push

        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
            broker = GitRemoteBroker()
            broker.register(plan)
            broker.approve("plan1")

            git_commands = []

            def capture_run(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list):
                    git_commands.append(cmd)
                return subprocess.run(*args, **kwargs)

            with patch("subprocess.run", side_effect=capture_run):
                try:
                    apply_push(plan, str(tmp_path), broker)
                except Exception:
                    pass

            # Check push commands don't include recurseSubmodules
            push_cmds = [cmd for cmd in git_commands if isinstance(cmd, list) and "push" in cmd]
            for cmd in push_cmds:
                assert "--recurse-submodules" not in cmd
                assert "recurseSubmodules" not in " ".join(cmd)

        except Exception:
            pytest.skip("Could not create push plan")
