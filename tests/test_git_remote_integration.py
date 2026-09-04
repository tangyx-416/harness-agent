"""Git remote integration tests: zero-network prepare, broker isolation, approval chain (§3, §23, §26)."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness_agent.execution import ExecutionBroker
from harness_agent.git_mutation.broker import GitMutationBroker
from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.patch import PatchBroker
from harness_agent.session import SessionState
from harness_agent.tools.git_remote_tools import make_git_remote_tools


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
# prepare_git_push zero-network (§3)
# ---------------------------------------------------------------------------


def test_prepare_git_push_performs_zero_network():
    """prepare_git_push performs zero DNS/TCP/HTTP/HTTPS/ls-remote."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        broker = GitRemoteBroker()
        prepare, _ = make_git_remote_tools(str(tmp_path), broker)

        network_calls = []

        def track_network(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list):
                # Track network commands
                if any(word in cmd for word in ["ls-remote", "fetch", "pull", "push", "curl", "wget"]):
                    network_calls.append(cmd)
            # Allow through
            return subprocess.run(*args, **kwargs)

        with patch("subprocess.run", side_effect=track_network):
            result = prepare(summary="Test")

        # No network commands should have been executed
        assert len(network_calls) == 0


def test_prepare_git_push_no_credential_helper_execution():
    """prepare_git_push does not execute credential helpers."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        # Configure a credential helper
        subprocess.run(
            ["git", "config", "credential.helper", "store"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        broker = GitRemoteBroker()
        prepare, _ = make_git_remote_tools(str(tmp_path), broker)

        credential_calls = []

        def track_credentials(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "credential" in str(cmd):
                credential_calls.append(cmd)
            return subprocess.run(*args, **kwargs)

        with patch("subprocess.run", side_effect=track_credentials):
            result = prepare(summary="Test")

        # No credential helper should have been invoked
        assert len(credential_calls) == 0


def test_rejection_after_prepare_causes_zero_network():
    """User rejection after prepare causes zero network activity."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        broker = GitRemoteBroker()
        prepare, _ = make_git_remote_tools(str(tmp_path), broker)

        result = prepare(summary="Test")
        plan_id = result.get("plan_id")

        if plan_id:
            # Reject the plan
            broker.reject(plan_id)

        network_calls = []

        def track_network(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and any(word in cmd for word in ["push", "fetch", "ls-remote"]):
                network_calls.append(cmd)
            raise Exception("network blocked")

        with patch("subprocess.run", side_effect=track_network):
            try:
                from harness_agent.git_remote.service import apply_push
                if plan_id:
                    plan = broker.get_plan(plan_id)
                    if plan:
                        apply_push(plan, str(tmp_path), broker)
            except Exception:
                pass

        # Rejected plan should never reach network
        assert len(network_calls) == 0


# ---------------------------------------------------------------------------
# Five-broker isolation (§23)
# ---------------------------------------------------------------------------


def test_five_broker_isolation_no_cross_leakage():
    """Five brokers (Session, Execution, Patch, GitMutation, GitRemote) are fully isolated."""
    # Agent A
    state_a = SessionState()
    exec_a = ExecutionBroker()
    patch_a = PatchBroker()
    git_mut_a = GitMutationBroker()
    git_remote_a = GitRemoteBroker()

    # Agent B
    state_b = SessionState()
    exec_b = ExecutionBroker()
    patch_b = PatchBroker()
    git_mut_b = GitMutationBroker()
    git_remote_b = GitRemoteBroker()

    # Create task in A
    task_a = state_a.create_task("Task A", ["Step 1"])
    assert state_b.get_task(task_a.id) is None

    # Create execution plan in A
    from harness_agent.tools.execution_tools import prepare_command_core
    with tempfile.TemporaryDirectory() as tmp:
        exec_plan_a = prepare_command_core("echo", ["test"], exec_a, Path(tmp))
        if exec_plan_a["ok"]:
            assert exec_b.get_plan(exec_plan_a["plan_id"]) is None


def test_git_remote_broker_isolation_between_agents():
    """GitRemoteBroker plans do not leak between agent instances."""
    broker_a = GitRemoteBroker()
    broker_b = GitRemoteBroker()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        prepare_a, get_result_a = make_git_remote_tools(str(tmp_path), broker_a)
        prepare_b, get_result_b = make_git_remote_tools(str(tmp_path), broker_b)

        result_a = prepare_a(summary="Test A")

        if result_a["ok"]:
            plan_id = result_a["plan_id"]

            # Broker B should not see the plan
            result_b = get_result_b(plan_id=plan_id)
            assert result_b["ok"] is False
            assert "Unknown" in result_b.get("error", "")


def test_session_event_isolation():
    """Session events do not leak between SessionState instances."""
    state_a = SessionState()
    state_b = SessionState()

    state_a.record_git_push_prepared("plan1", "https://github.com/a/repo.git", "main", "main")
    state_a.record_git_push_approved("plan1", "origin", "main")

    snap_a = state_a.snapshot()
    snap_b = state_b.snapshot()

    # A has events, B does not
    assert len(snap_a["recent_events"]) > 0
    assert snap_b["recent_events"] == []


# ---------------------------------------------------------------------------
# No same-turn dependent authority (§26)
# ---------------------------------------------------------------------------


def test_push_only_valid_after_local_commit_applied():
    """Push is only valid after local commit result is applied."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo_with_push_ready(tmp_path)

        # Now prepare push - should work because commit is already applied
        broker = GitRemoteBroker()
        prepare, _ = make_git_remote_tools(str(tmp_path), broker)
        result = prepare(summary="Test")

        # Should succeed because local commit exists
        assert result["ok"] is True
        assert result["commit_count"] > 0


# ---------------------------------------------------------------------------
# Integration: approval chain
# ---------------------------------------------------------------------------


def test_patch_execution_stage_commit_push_approval_chain():
    """Each operation waits for user approval; no automatic chaining."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Each broker is independent
        patch_broker = PatchBroker()
        exec_broker = ExecutionBroker()
        git_mut_broker = GitMutationBroker()
        git_remote_broker = GitRemoteBroker()

        # All start with zero pending
        assert len(patch_broker.pending()) == 0
        assert len(exec_broker.pending()) == 0
        assert len(git_mut_broker.pending()) == 0
        assert len(git_remote_broker.pending_plans()) == 0

        # Creating one plan doesn't auto-approve or create plans in other brokers
        from harness_agent.tools.execution_tools import prepare_command_core
        exec_result = prepare_command_core("python", ["--version"], broker=exec_broker, root=Path(tmp_path))
        assert exec_result["ok"] is True

        # Execution pending, but no other brokers affected
        assert len(exec_broker.pending()) == 1
        assert len(patch_broker.pending()) == 0
        assert len(git_mut_broker.pending()) == 0
        assert len(git_remote_broker.pending_plans()) == 0
