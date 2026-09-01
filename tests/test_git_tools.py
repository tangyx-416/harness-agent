"""Tests for the Git Awareness tool surface: registration, mutation
absence, prepare_command still denying git, and prompt principles."""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness_agent.agent import create_agent, load_system_prompt
from harness_agent.config import AgentConfig
from harness_agent.tools.execution_tools import prepare_command_core
from harness_agent.tools.git_tools import (
    git_branches_core,
    git_diff_core,
    git_log_core,
    git_status_core,
)

from git_test_utils import commit_file, make_git_repo

REPO_ROOT = Path(__file__).parent.parent.resolve()

MUTATION_TOOL_NAMES = {
    "git_add",
    "git_commit",
    "git_push",
    "git_pull",
    "git_fetch",
    "git_checkout",
    "git_switch",
    "git_restore",
    "git_reset",
    "git_merge",
    "git_rebase",
    "git_cherry_pick",
    "git_clean",
    "git_tag",
    "git_rm",
    "git_mv",
    "run_git",
    "git_command",
    "execute_git",
}


@pytest.fixture()
def git_repo(tmp_path):
    repo = make_git_repo(tmp_path)
    commit_file(repo, "README.md", "hello\n", "initial commit")
    return repo


# ---------------------------------------------------------------------------
# Tool registration (v0.4.0 surface = 11 tools)
# ---------------------------------------------------------------------------


def test_create_agent_registers_all_v03_tools():
    """All seven v0.3 tools remain registered (superset check)."""
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)
    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config)
        tools = mock_agent_class.call_args.kwargs["tools"]
        tool_names = {
            getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools
        }
    expected = {
        "inspect_project",
        "list_directory",
        "read_file",
        "search_code",
        "analyze_dependencies",
        "prepare_command",
        "get_execution_result",
    }
    assert expected <= tool_names


def test_create_agent_registers_all_v04_tools():
    """v0.4.0: exactly eleven agent-visible tools including Git tools."""
    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)
    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config)
        tools = mock_agent_class.call_args.kwargs["tools"]
        tool_names = {
            getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools
        }

    expected = {
        "inspect_project",
        "list_directory",
        "read_file",
        "search_code",
        "analyze_dependencies",
        "prepare_command",
        "get_execution_result",
        "git_status",
        "git_diff",
        "git_log",
        "git_branches",
    }
    assert tool_names == expected
    assert len(tools) == 11

    # No Git mutation / generic git interface may be exposed.
    assert tool_names.isdisjoint(MUTATION_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Mutation boundary
# ---------------------------------------------------------------------------


def test_prepare_command_still_denies_git():
    broker_fresh = __import__(
        "harness_agent.execution", fromlist=["ExecutionBroker"]
    ).ExecutionBroker()
    for args in (["status"], ["diff"], ["log"], ["push"], ["commit"], ["add", "."]):
        result = prepare_command_core(
            "git", args=args, broker=broker_fresh, root=REPO_ROOT
        )
        assert result["ok"] is False, args
        assert result["denied"] is True, args
    assert broker_fresh.pending() == []


def test_git_tools_source_has_no_generic_interface():
    source_dir = REPO_ROOT / "src" / "harness_agent"
    for rel in ("tools/git_tools.py", "git_awareness/service.py"):
        source = (source_dir / rel).read_text(encoding="utf-8")
        for forbidden in (
            "def run_git",
            "def git_command",
            "def execute_git",
            "subprocess.run(",
        ):
            assert forbidden not in source, (rel, forbidden)


# ---------------------------------------------------------------------------
# Structured results against a disposable repo
# ---------------------------------------------------------------------------


def test_git_tools_return_structured_payloads(git_repo):
    status = git_status_core(root=git_repo)
    diff = git_diff_core(root=git_repo)
    log = git_log_core(root=git_repo)
    branches = git_branches_core(root=git_repo)

    assert status["ok"] is True and status["branch"] == "main"
    assert diff["ok"] is True and diff["empty"] is True
    assert log["ok"] is True and log["commits"][0]["subject"] == "initial commit"
    assert branches["ok"] is True and branches["current"] == "main"


# ---------------------------------------------------------------------------
# Prompt principles
# ---------------------------------------------------------------------------


def test_system_prompt_documents_git_principles():
    prompt = load_system_prompt()
    assert "UNTRUSTED REPOSITORY DATA" in prompt
    assert "Git mutation is not supported" in prompt
    assert "Git Evidence Principle" in prompt
    assert "Read-Only Git Awareness" in prompt
