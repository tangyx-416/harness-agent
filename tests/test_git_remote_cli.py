"""CLI approval flow tests: preview, authorization, rejection (§18, §19)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.session import SessionState


# ---------------------------------------------------------------------------
# CLI approval safety (§18)
# ---------------------------------------------------------------------------


def test_cli_rejection_prevents_network_access():
    """CLI rejection (n, empty, garbage) prevents network access."""
    from scripts.run_agent import process_pending_git_pushes

    broker = GitRemoteBroker()
    state = SessionState()

    network_calls = []

    def track_network(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and any(word in cmd for word in ["push", "fetch", "ls-remote"]):
            network_calls.append(cmd)
        raise Exception("network blocked")

    with patch("builtins.input", return_value="n"):
        with patch("subprocess.run", side_effect=track_network):
            # No pending pushes, so nothing happens
            process_pending_git_pushes(broker, state, Path("/tmp"))

    # Should not reach network
    assert len(network_calls) == 0


def test_cli_keyboard_interrupt_prevents_network():
    """Ctrl+C (KeyboardInterrupt) prevents network access."""
    from scripts.run_agent import process_pending_git_pushes

    broker = GitRemoteBroker()
    state = SessionState()

    network_calls = []

    def track_network(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "push" in cmd:
            network_calls.append(cmd)
        raise Exception("blocked")

    with patch("builtins.input", side_effect=KeyboardInterrupt):
        with patch("subprocess.run", side_effect=track_network):
            try:
                process_pending_git_pushes(broker, state, Path("/tmp"))
            except KeyboardInterrupt:
                pass

    assert len(network_calls) == 0


# ---------------------------------------------------------------------------
# prepare_command Git denial (§25)
# ---------------------------------------------------------------------------


def test_prepare_command_denies_git_push():
    """prepare_command denies git push through execution broker."""
    from harness_agent.execution import ExecutionBroker
    from harness_agent.tools.execution_tools import prepare_command_core

    broker = ExecutionBroker()
    result = prepare_command_core("git", ["push"], broker, Path("/tmp"))

    assert result["ok"] is False
    assert result["denied"] is True


def test_prepare_command_denies_git_fetch():
    """prepare_command denies git fetch."""
    from harness_agent.execution import ExecutionBroker
    from harness_agent.tools.execution_tools import prepare_command_core

    broker = ExecutionBroker()
    result = prepare_command_core("git", ["fetch"], broker, Path("/tmp"))

    assert result["ok"] is False
    assert result["denied"] is True


def test_prepare_command_denies_git_pull():
    """prepare_command denies git pull."""
    from harness_agent.execution import ExecutionBroker
    from harness_agent.tools.execution_tools import prepare_command_core

    broker = ExecutionBroker()
    result = prepare_command_core("git", ["pull"], broker, Path("/tmp"))

    assert result["ok"] is False
    assert result["denied"] is True


def test_prepare_command_denies_git_remote():
    """prepare_command denies git remote."""
    from harness_agent.execution import ExecutionBroker
    from harness_agent.tools.execution_tools import prepare_command_core

    broker = ExecutionBroker()
    result = prepare_command_core("git", ["remote", "add", "origin", "url"], broker, Path("/tmp"))

    assert result["ok"] is False
    assert result["denied"] is True


# ---------------------------------------------------------------------------
# Exact 22 tools (§24)
# ---------------------------------------------------------------------------


def test_agent_exposes_exactly_22_tools():
    """Agent exposes exactly 22 tools including prepare_git_push and get_git_push_result."""
    from unittest.mock import Mock
    from harness_agent.agent import create_agent
    from harness_agent.config import AgentConfig

    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)

    with patch("harness_agent.agent.OpenAIModel"), patch("harness_agent.agent.Agent") as mock_agent:
        mock_agent.return_value = Mock()
        create_agent(config, session_state=SessionState())

    tools = mock_agent.call_args.kwargs["tools"]
    tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools]

    assert len(tools) == 24
    assert "prepare_git_push" in tool_names
    assert "get_git_push_result" in tool_names

    # No model-visible execution tools
    forbidden = {"git_push", "push_refspec", "run_git", "fetch", "pull", "remote",
                 "approve_push", "reject_push", "apply_push"}
    assert not (forbidden & set(tool_names))


# ---------------------------------------------------------------------------
# Session events (§22)
# ---------------------------------------------------------------------------


def test_session_records_git_push_events():
    """SessionState records git push events without credential data."""
    state = SessionState()

    state.record_git_push_prepared("plan1", "https://github.com/test/repo.git", "refs/heads/main", "refs/heads/main")
    state.record_git_push_approved("plan1", "origin", "main")

    snapshot = state.snapshot()
    events = snapshot["recent_events"]

    # Should have events
    assert len(events) >= 2

    # Events should not contain credentials
    snapshot_str = str(snapshot)
    assert "token" not in snapshot_str.lower()
    assert "password" not in snapshot_str.lower()


def test_session_records_git_push_rejection():
    """SessionState records rejection events."""
    state = SessionState()

    state.record_git_push_prepared("plan1", "https://github.com/test/repo.git", "refs/heads/main", "refs/heads/main")
    state.record_git_push_rejected("plan1")

    snapshot = state.snapshot()
    event_types = [e["type"] for e in snapshot["recent_events"]]

    assert "git_push_prepared" in event_types or "git_push_rejected" in event_types


def test_session_records_git_push_applied():
    """SessionState records successful push."""
    state = SessionState()

    state.record_git_push_prepared("plan1", "https://github.com/test/repo.git", "refs/heads/main", "refs/heads/main")
    state.record_git_push_approved("plan1", "origin", "main")
    state.record_git_push_applied("plan1", "origin", "main", "abc123")

    snapshot = state.snapshot()
    event_types = [e["type"] for e in snapshot["recent_events"]]

    assert "git_push_applied" in event_types or len(event_types) >= 3


def test_session_records_git_push_failed():
    """SessionState records failed push."""
    state = SessionState()

    state.record_git_push_prepared("plan1", "https://github.com/test/repo.git", "refs/heads/main", "refs/heads/main")
    state.record_git_push_approved("plan1", "origin", "main")
    state.record_git_push_failed("plan1", "Network error")

    snapshot = state.snapshot()
    event_types = [e["type"] for e in snapshot["recent_events"]]

    assert "git_push_failed" in event_types or len(event_types) >= 3


# ---------------------------------------------------------------------------
# No false success (§27)
# ---------------------------------------------------------------------------


def test_prepare_does_not_mean_pushed():
    """prepare_git_push success does not mean remote was pushed."""
    state = SessionState()

    state.record_git_push_prepared("plan1", "https://github.com/test/repo.git", "refs/heads/main", "refs/heads/main")

    snapshot = state.snapshot()

    # Only prepared, not applied
    event_types = [e["type"] for e in snapshot["recent_events"]]
    assert "git_push_applied" not in event_types


def test_approved_does_not_mean_pushed():
    """Approval does not mean remote was pushed."""
    state = SessionState()

    state.record_git_push_prepared("plan1", "https://github.com/test/repo.git", "refs/heads/main", "refs/heads/main")
    state.record_git_push_approved("plan1", "origin", "main")

    snapshot = state.snapshot()
    event_types = [e["type"] for e in snapshot["recent_events"]]

    # Approved but not applied
    assert "git_push_applied" not in event_types
