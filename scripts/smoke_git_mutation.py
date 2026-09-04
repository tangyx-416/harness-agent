#!/usr/bin/env python
"""Smoke test for v0.7.0 Git mutation subsystem.

Validates end-to-end stage and commit workflows in isolated temporary Git repos.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


def run_cmd(cmd, cwd=None, check=True):
    """Run a command and return stdout."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def test_git_mutation_broker_isolation():
    """Test that two brokers are isolated."""
    print("  Testing broker isolation...", end=" ")

    from harness_agent.git_mutation import GitMutationBroker, GitStagePlan, KIND_STAGE

    broker1 = GitMutationBroker()
    broker2 = GitMutationBroker()

    plan = GitStagePlan(
        id="test-001",
        kind=KIND_STAGE,
        repo_path="test.txt",
        summary="Test",
        branch="main",
        head_oid="a" * 40,
        base_worktree_sha256="b" * 64,
        base_index_fingerprint="c" * 64,
        previous_index_entry=None,
        proposed_mode="100644",
        proposed_blob_oid="d" * 40,
        diff="diff",
        diff_sha256="e" * 64,
        created_at=0.0,
    )

    broker1.register(plan)

    assert broker1.get_plan("test-001") is not None
    assert broker2.get_plan("test-001") is None
    assert len(broker1.pending()) == 1
    assert len(broker2.pending()) == 0

    print("✓")


def test_git_mutation_exactly_once():
    """Test exactly-once apply semantics."""
    print("  Testing exactly-once semantics...", end=" ")

    from harness_agent.git_mutation import GitMutationBroker, GitMutationBrokerError, GitStagePlan, KIND_STAGE

    broker = GitMutationBroker()

    plan = GitStagePlan(
        id="test-002",
        kind=KIND_STAGE,
        repo_path="test.txt",
        summary="Test",
        branch="main",
        head_oid="a" * 40,
        base_worktree_sha256="b" * 64,
        base_index_fingerprint="c" * 64,
        previous_index_entry=None,
        proposed_mode="100644",
        proposed_blob_oid="d" * 40,
        diff="diff",
        diff_sha256="e" * 64,
        created_at=0.0,
    )

    broker.register(plan)
    broker.approve("test-002")

    # First take succeeds
    first = broker.take_for_apply("test-002")
    assert first is not None

    # Second take fails
    try:
        broker.take_for_apply("test-002")
        raise AssertionError("Should have raised GitMutationBrokerError")
    except GitMutationBrokerError:
        pass

    print("✓")


def test_stage_policy_path_safety():
    """Test stage policy rejects unsafe paths."""
    print("  Testing stage policy path safety...", end=" ")

    from harness_agent.git_mutation.policy import GitMutationPolicyError, prepare_stage_plan

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        run_cmd(["git", "init"], cwd=repo)
        run_cmd(["git", "config", "user.name", "Test"], cwd=repo)
        run_cmd(["git", "config", "user.email", "test@test.com"], cwd=repo)

        # Absolute path should be rejected
        try:
            prepare_stage_plan(
                path="/absolute/path.txt",
                summary="Test",
                repo_root=repo,
            )
            raise AssertionError("Should have rejected absolute path")
        except GitMutationPolicyError:
            pass

        # Parent traversal should be rejected
        try:
            prepare_stage_plan(
                path="../escape.txt",
                summary="Test",
                repo_root=repo,
            )
            raise AssertionError("Should have rejected parent traversal")
        except GitMutationPolicyError:
            pass

    print("✓")


def test_commit_policy_message_validation():
    """Test commit policy validates messages."""
    print("  Testing commit policy message validation...", end=" ")

    from harness_agent.git_mutation.policy import GitMutationPolicyError, prepare_commit_plan

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        run_cmd(["git", "init"], cwd=repo)
        run_cmd(["git", "config", "user.name", "Test"], cwd=repo)
        run_cmd(["git", "config", "user.email", "test@test.com"], cwd=repo)

        # Create initial commit
        test_file = repo / "test.txt"
        test_file.write_text("content\n", encoding="utf-8")
        run_cmd(["git", "add", "test.txt"], cwd=repo)
        run_cmd(["git", "commit", "-m", "Initial"], cwd=repo)

        # Empty message should be rejected
        test_file.write_text("modified\n", encoding="utf-8")
        run_cmd(["git", "add", "test.txt"], cwd=repo)

        try:
            prepare_commit_plan(message="", repo_root=repo)
            raise AssertionError("Should have rejected empty message")
        except GitMutationPolicyError:
            pass

        # Control character should be rejected
        try:
            prepare_commit_plan(message="Test\x00commit", repo_root=repo)
            raise AssertionError("Should have rejected control character")
        except GitMutationPolicyError:
            pass

    print("✓")


def test_agent_has_git_mutation_tools():
    """Test agent exposes Git mutation tools."""
    print("  Testing agent has Git mutation tools...", end=" ")

    from harness_agent.agent import create_agent
    from harness_agent.config import AgentConfig
    from unittest.mock import patch, Mock

    config = AgentConfig(api_key="test-key", model_id="gpt-4", base_url=None)

    # Mock the Agent class to capture tools
    with patch("harness_agent.agent.OpenAIModel"), patch(
        "harness_agent.agent.Agent"
    ) as mock_agent_class:
        mock_agent_class.return_value = Mock()
        create_agent(config)

        tools = mock_agent_class.call_args.kwargs["tools"]
        tool_names = {
            getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools
        }

        assert "prepare_git_stage" in tool_names
        assert "prepare_git_commit" in tool_names
        assert "get_git_mutation_result" in tool_names
        assert len(tools) == 20  # v0.7.0: 17 + 3

    print("✓")


def test_session_records_git_mutation_events():
    """Test session state records Git mutation events."""
    print("  Testing session state Git mutation events...", end=" ")

    from harness_agent.session import SessionState

    state = SessionState()

    # Record various events
    state.record_git_stage_approved("stage-001")
    state.record_git_stage_applied("stage-001", "test.txt")
    state.record_git_commit_approved("commit-001")
    state.record_git_commit_applied("commit-001", "a" * 40)

    events = state.recent_events  # Property, not method
    event_types = [e.type for e in events]

    assert "git_stage_approved" in event_types
    assert "git_stage_applied" in event_types
    assert "git_commit_approved" in event_types
    assert "git_commit_applied" in event_types

    print("✓")


def main():
    print("=" * 60)
    print("Git Mutation Smoke Tests (v0.7.0)")
    print("=" * 60)

    try:
        test_git_mutation_broker_isolation()
        test_git_mutation_exactly_once()
        test_stage_policy_path_safety()
        test_commit_policy_message_validation()
        test_agent_has_git_mutation_tools()
        test_session_records_git_mutation_events()

        print()
        print("✅ All smoke tests passed")
        return 0
    except Exception as e:
        print()
        print(f"❌ Smoke test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
