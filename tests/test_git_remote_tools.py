"""Tests for Git remote push tools."""

import subprocess
import tempfile
from pathlib import Path

from harness_agent.git_remote.broker import GitRemoteBroker
from harness_agent.git_remote.models import PushState
from harness_agent.tools.git_remote_tools import make_git_remote_tools


def _init_test_repo_with_push_ready(tmp_path: Path):
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


def test_make_tools_returns_two_functions():
    """make_git_remote_tools returns two callables."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        tools = make_git_remote_tools(tmp, broker)
        assert len(tools) == 2
        assert callable(tools[0])
        assert callable(tools[1])


def test_tools_have_names():
    """Tools have proper names."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        prepare, get_result = make_git_remote_tools(tmp, broker)
        assert prepare.tool_name == "prepare_git_push"
        assert get_result.tool_name == "get_git_push_result"


def test_tools_have_docstrings():
    """Tools have docstrings."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        prepare, get_result = make_git_remote_tools(tmp, broker)
        assert prepare.__doc__ is not None
        assert "prepare" in prepare.__doc__.lower() or "push" in prepare.__doc__.lower()
        assert get_result.__doc__ is not None
        assert "result" in get_result.__doc__.lower()


def test_tools_are_bound_to_broker():
    """Tools use the provided broker."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_test_repo_with_push_ready(tmp_path)

        prepare, get_result = make_git_remote_tools(tmp, broker)

        result = prepare(summary="Test push")

        assert result["ok"] is True
        plan_id = result["plan_id"]

        # Broker should have the plan
        assert broker.get_plan(plan_id) is not None
        assert broker.get_state(plan_id) == PushState.PENDING


def test_two_brokers_are_isolated():
    """Tools with different brokers are isolated."""
    broker_a = GitRemoteBroker()
    broker_b = GitRemoteBroker()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_test_repo_with_push_ready(tmp_path)

        prepare_a, _ = make_git_remote_tools(tmp, broker_a)
        _, get_result_b = make_git_remote_tools(tmp, broker_b)

        result = prepare_a(summary="Test push")
        assert result["ok"] is True
        plan_id = result["plan_id"]

        # Broker B should not see the plan
        result_b = get_result_b(plan_id=plan_id)
        assert result_b["ok"] is False
        assert "Unknown" in result_b["error"]


def test_prepare_git_push_requires_summary():
    """prepare_git_push requires non-empty summary."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        prepare, _ = make_git_remote_tools(tmp, broker)

        result = prepare(summary="")
        assert result["ok"] is False
        assert "Summary is required" in result["error"]


def test_prepare_git_push_creates_plan():
    """prepare_git_push creates a pending plan."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_test_repo_with_push_ready(tmp_path)

        prepare, _ = make_git_remote_tools(tmp, broker)

        result = prepare(summary="Test push")

        assert result["ok"] is True
        assert "plan_id" in result
        assert result["state"] == "pending"
        assert result["local_branch"] in ("main", "master")
        assert result["remote_name"] == "origin"
        assert result["remote_branch"] in ("main", "master")
        assert "github.com" in result["remote_url"]
        assert result["commit_count"] == 1


def test_prepare_git_push_rejects_invalid_state():
    """prepare_git_push rejects invalid repository state."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        # Don't initialize repo - invalid state
        prepare, _ = make_git_remote_tools(tmp, broker)

        result = prepare(summary="Test push")

        assert result["ok"] is False
        assert "error" in result


def test_get_git_push_result_requires_plan_id():
    """get_git_push_result requires plan_id."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        _, get_result = make_git_remote_tools(tmp, broker)

        result = get_result(plan_id="")
        assert result["ok"] is False
        assert "plan_id is required" in result["error"]


def test_get_git_push_result_returns_pending():
    """get_git_push_result returns pending state."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_test_repo_with_push_ready(tmp_path)

        prepare, get_result = make_git_remote_tools(tmp, broker)

        prep_result = prepare(summary="Test push")
        plan_id = prep_result["plan_id"]

        result = get_result(plan_id=plan_id)

        assert result["ok"] is True
        assert result["state"] == "pending"
        assert "awaiting" in result["message"].lower()


def test_get_git_push_result_returns_not_found():
    """get_git_push_result returns error for unknown plan."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        _, get_result = make_git_remote_tools(tmp, broker)

        result = get_result(plan_id="nonexistent")

        assert result["ok"] is False
        assert "Unknown" in result["error"]


def test_get_git_push_result_returns_approved():
    """get_git_push_result returns approved state."""
    broker = GitRemoteBroker()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_test_repo_with_push_ready(tmp_path)

        prepare, get_result = make_git_remote_tools(tmp, broker)

        prep_result = prepare(summary="Test push")
        plan_id = prep_result["plan_id"]

        broker.approve(plan_id)

        result = get_result(plan_id=plan_id)

        assert result["ok"] is True
        assert result["state"] == "approved"
