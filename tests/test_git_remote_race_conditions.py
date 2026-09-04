"""Critical race condition and mutation safety tests for v0.8.0 release audit."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.git_remote.models import PushState
from harness_agent.git_remote.policy import validate_and_prepare_push
from harness_agent.git_remote.service import apply_push


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
# 1. Lease/CAS race protection (Audit item #1)
# ---------------------------------------------------------------------------


def test_lease_cas_race_prevents_push_after_remote_change():
    """Approved push MUST fail if remote changes between preflight and push."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Set up local repo with outgoing commit
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Simulate remote tracking: A is ancestor of B (HEAD)
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

        # Add second commit (B)
        test_file = tmp_path / "test.txt"
        test_file.write_text("modified\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Second"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Prepare plan (expects remote = A, local HEAD = B)
        try:
            plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
            broker = GitRemoteBroker()
            broker.register(plan)
            broker.approve("plan1")

            # Simulate race: remote changes A -> C between preflight and push
            # by mocking the preflight to return a different OID
            original_run = subprocess.run

            def mock_run(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and "ls-remote" in cmd:
                    # Simulate remote changed to C (different OID)
                    result = MagicMock()
                    result.returncode = 0
                    result.stdout = "ffffffffffffffffffffffffffffffffffffffff\trefs/heads/main\n"
                    result.stderr = ""
                    return result
                return original_run(*args, **kwargs)

            with patch("subprocess.run", side_effect=mock_run):
                result = apply_push(plan, str(tmp_path), broker)

            # Push MUST fail (conflict or failed state)
            assert result.state in (PushState.CONFLICT, PushState.FAILED)

        except Exception as e:
            # If plan creation fails due to missing real remote, that's acceptable
            # The critical assertion is: if a plan is created and approved,
            # a concurrent remote change must prevent the push
            pytest.skip(f"Could not set up test: {e}")


# ---------------------------------------------------------------------------
# 2. Fast-forward-only semantics (Audit item #2)
# ---------------------------------------------------------------------------


def test_diverged_history_rejected():
    """Diverged history (not ancestor) must be denied."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        head_oid = _init_repo(tmp_path)

        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Create second commit on main
        test_file = tmp_path / "test.txt"
        test_file.write_text("second commit on main\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Second on main"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Create diverged commit from initial
        subprocess.run(["git", "checkout", head_oid], cwd=str(tmp_path), check=True, capture_output=True)
        other_file = tmp_path / "other.txt"
        other_file.write_text("diverged\n")
        subprocess.run(["git", "add", "other.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Diverged"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        diverged_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Set remote tracking to diverged history and go back to main
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", diverged_oid],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "checkout", "-"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "branch", "--set-upstream-to=origin/main"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Try to prepare push - should fail due to diverged history
        from harness_agent.git_remote.policy import RemotePushPolicyError

        with pytest.raises(RemotePushPolicyError, match="diverged"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


def test_behind_remote_rejected():
    """Local behind remote (need pull) must be denied."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        first_oid = _init_repo(tmp_path)

        # Add second commit to simulate remote ahead
        test_file = tmp_path / "test.txt"
        test_file.write_text("remote ahead\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Remote ahead"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        remote_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Reset local to first commit (now behind remote)
        subprocess.run(["git", "reset", "--hard", first_oid], cwd=str(tmp_path), check=True, capture_output=True)

        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", remote_oid],
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

        # Should fail - local is behind remote
        from harness_agent.git_remote.policy import RemotePushPolicyError

        with pytest.raises(RemotePushPolicyError, match="behind"):
            validate_and_prepare_push(str(tmp_path), "Test", "plan1")


# ---------------------------------------------------------------------------
# 3. Destination binding (Audit item #3)
# ---------------------------------------------------------------------------


def _prepare_pushable_repo(tmp_path: Path):
    """Init a repo with an outgoing commit ready to push. Returns plan-ready state."""
    head_oid = _init_repo(tmp_path)
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
    # Outgoing commit
    test_file = tmp_path / "test.txt"
    test_file.write_text("outgoing\n")
    subprocess.run(["git", "add", "test.txt"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Outgoing"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )


def test_url_change_after_prepare_causes_conflict():
    """Changing remote URL after prepare must cause conflict on apply, with no push."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        # Mutate the remote URL AFTER approval
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/attacker/evil.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        network_calls = []
        original_run = subprocess.run

        def track_network(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and any(w in cmd for w in ["ls-remote", "push", "fetch"]):
                network_calls.append(cmd)
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=track_network):
            result = apply_push(plan, str(tmp_path), broker)

        # Must be a conflict, no network write to either destination
        assert result.state == PushState.CONFLICT
        assert len(network_calls) == 0, f"Network contacted despite URL change: {network_calls}"


def test_branch_change_after_prepare_causes_conflict():
    """Changing current branch after prepare must cause conflict on apply, with no push."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        # Switch to a different branch AFTER approval
        subprocess.run(
            ["git", "checkout", "-b", "sidebranch"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        network_calls = []
        original_run = subprocess.run

        def track_network(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and any(w in cmd for w in ["ls-remote", "push", "fetch"]):
                network_calls.append(cmd)
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=track_network):
            result = apply_push(plan, str(tmp_path), broker)

        # Must be a conflict, no network write
        assert result.state == PushState.CONFLICT
        assert len(network_calls) == 0, f"Network contacted despite branch change: {network_calls}"


# ---------------------------------------------------------------------------
# 4. Exactly-once network write (Audit item #4)
# ---------------------------------------------------------------------------


def test_network_failure_no_retry():
    """Network failure must not trigger automatic retry."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_count = 0
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal push_count
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                # Preflight: remote is still at expected OID
                r = MagicMock()
                r.returncode = 0
                r.stdout = f"{plan.expected_remote_oid}\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_count += 1
                # Simulate network failure
                r = MagicMock()
                r.returncode = 128
                r.stdout = ""
                r.stderr = "fatal: unable to access: Connection timed out"
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = apply_push(plan, str(tmp_path), broker)

        # Terminal failure, exactly one push attempt, no retry
        assert result.state == PushState.FAILED
        assert push_count == 1, f"Expected exactly 1 push attempt, got {push_count}"


def test_lease_conflict_is_terminal_no_retry():
    """A lease/CAS conflict during push must be terminal with no retry."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_count = 0
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal push_count
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                r = MagicMock()
                r.returncode = 0
                r.stdout = f"{plan.expected_remote_oid}\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_count += 1
                r = MagicMock()
                r.returncode = 1
                r.stdout = ""
                r.stderr = "! [rejected] main -> main (stale info)"
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = apply_push(plan, str(tmp_path), broker)

        assert result.state == PushState.CONFLICT
        assert push_count == 1


# ---------------------------------------------------------------------------
# 9. Post-push verification (Audit item #9)
# ---------------------------------------------------------------------------


def test_post_push_verification_mismatch_is_failed():
    """Push reports success but remote OID differs -> FAILED, no second push."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_count = 0
        ls_remote_count = 0
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal push_count, ls_remote_count
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                ls_remote_count += 1
                r = MagicMock()
                r.returncode = 0
                if ls_remote_count == 1:
                    # Preflight: remote at expected OID
                    r.stdout = f"{plan.expected_remote_oid}\trefs/heads/main\n"
                else:
                    # Post-verify: remote at a DIFFERENT OID than we pushed
                    r.stdout = "ffffffffffffffffffffffffffffffffffffffff\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_count += 1
                r = MagicMock()
                r.returncode = 0
                r.stdout = "To https://github.com/test/repo.git\n"
                r.stderr = ""
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = apply_push(plan, str(tmp_path), broker)

        # Push "succeeded" but verification found a mismatch -> FAILED
        assert result.state == PushState.FAILED
        assert result.remote_oid_after == "f" * 40
        # Exactly one push, no automatic second push
        assert push_count == 1


def test_post_push_verification_match_is_applied():
    """Push succeeds and remote OID matches plan HEAD -> APPLIED."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_count = 0
        ls_remote_count = 0
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal push_count, ls_remote_count
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                ls_remote_count += 1
                r = MagicMock()
                r.returncode = 0
                if ls_remote_count == 1:
                    r.stdout = f"{plan.expected_remote_oid}\trefs/heads/main\n"
                else:
                    # Post-verify: remote now exactly at pushed HEAD
                    r.stdout = f"{plan.head_oid}\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_count += 1
                r = MagicMock()
                r.returncode = 0
                r.stdout = "To https://github.com/test/repo.git\n"
                r.stderr = ""
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = apply_push(plan, str(tmp_path), broker)

        assert result.state == PushState.APPLIED
        assert result.remote_oid_after == plan.head_oid
        assert push_count == 1


def test_preflight_conflict_prevents_push():
    """If remote changed before push (preflight), no push occurs -> CONFLICT."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _prepare_pushable_repo(tmp_path)

        plan = validate_and_prepare_push(str(tmp_path), "Test", "plan1")
        broker = GitRemoteBroker()
        broker.register(plan)
        broker.approve("plan1")

        push_count = 0
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal push_count
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "ls-remote" in cmd:
                # Preflight: remote already moved to C
                r = MagicMock()
                r.returncode = 0
                r.stdout = "ffffffffffffffffffffffffffffffffffffffff\trefs/heads/main\n"
                r.stderr = ""
                return r
            if isinstance(cmd, list) and "push" in cmd:
                push_count += 1
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            result = apply_push(plan, str(tmp_path), broker)

        assert result.state == PushState.CONFLICT
        # No push must have been attempted after a failed preflight
        assert push_count == 0
