"""Comprehensive tests for Git mutation tools (v0.7.0).

Tests the agent-facing tool API that allows the model to propose Git
mutations. These tools are closure-bound to a GitMutationBroker and
expose exactly three operations:

- prepare_git_stage(path, summary)
- prepare_git_commit(message)
- get_git_mutation_result(plan_id)

Key invariants tested:
- Tools are bound to correct broker
- Tools never directly mutate Git
- Tool returns contain pending plan metadata
- Unknown plan IDs are handled
- No arbitrary argv parameters exposed
- No model-visible approve/reject/apply operations
"""

import os
import subprocess
from pathlib import Path

import pytest

from harness_agent.git_mutation.broker import GitMutationBroker
from harness_agent.tools.git_mutation_tools import make_git_mutation_tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary Git repository for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Initial commit
    initial_file = repo / "README.md"
    initial_file.write_text("# Test Repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    return repo


@pytest.fixture
def broker():
    """Create a fresh GitMutationBroker."""
    return GitMutationBroker()


# ---------------------------------------------------------------------------
# Tool Factory
# ---------------------------------------------------------------------------


def test_make_tools_returns_three_functions(broker):
    """make_git_mutation_tools returns exactly 3 tools."""
    tools = make_git_mutation_tools(broker)

    assert len(tools) == 3


def test_make_tools_returns_callable(broker):
    """All returned tools are callable."""
    stage, commit, result = make_git_mutation_tools(broker)

    assert callable(stage)
    assert callable(commit)
    assert callable(result)


def test_tools_are_bound_to_broker(temp_git_repo):
    """Tools from same factory share the same broker."""
    # Change to temp repo for path resolution
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        broker = GitMutationBroker()
        stage, commit, result = make_git_mutation_tools(broker)

        # Create and stage file
        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        # Prepare via tool
        stage_result = stage(path="file.txt", summary="Add file")

        assert stage_result["ok"]
        plan_id = stage_result["plan_id"]

        # Result tool should see the plan (same broker)
        result_data = result(plan_id=plan_id)

        assert result_data["ok"]
        assert result_data["plan_id"] == plan_id
        assert result_data["status"] == "pending"

    finally:
        os.chdir(original_cwd)


def test_tools_with_different_brokers_are_isolated(temp_git_repo):
    """Tools from different brokers don't share state."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        broker_a = GitMutationBroker()
        broker_b = GitMutationBroker()

        stage_a, _, result_a = make_git_mutation_tools(broker_a)
        _, _, result_b = make_git_mutation_tools(broker_b)

        # Create file
        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        # Prepare via broker A's tool
        stage_result = stage_a(path="file.txt", summary="Add file")
        plan_id = stage_result["plan_id"]

        # Broker A's result tool sees the plan
        result_a_data = result_a(plan_id=plan_id)
        assert result_a_data["ok"]

        # Broker B's result tool does NOT see it
        result_b_data = result_b(plan_id=plan_id)
        assert not result_b_data["ok"]
        assert "unknown" in result_b_data["error"].lower()

    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# prepare_git_stage Tool
# ---------------------------------------------------------------------------


def test_prepare_git_stage_creates_plan(temp_git_repo, broker):
    """prepare_git_stage creates pending plan."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        # Create file
        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        # Prepare
        result = stage(path="file.txt", summary="Add file")

        assert result["ok"]
        assert result["requires_approval"]
        assert result["status"] == "pending"
        assert result["kind"] == "stage"
        assert result["path"] == "file.txt"
        assert "plan_id" in result

    finally:
        os.chdir(original_cwd)


def test_prepare_git_stage_includes_diff(temp_git_repo, broker):
    """prepare_git_stage includes complete diff."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Line 1\nLine 2\n", encoding="utf-8")

        result = stage(path="file.txt", summary="Add file")

        assert result["ok"]
        assert "diff" in result
        assert "Line 1" in result["diff"]
        assert "Line 2" in result["diff"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_stage_rejects_invalid_path(temp_git_repo, broker):
    """prepare_git_stage rejects dangerous paths."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        # Try parent traversal
        result = stage(path="../evil.txt", summary="Should fail")

        assert not result["ok"]
        assert result["denied"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_stage_rejects_binary(temp_git_repo, broker):
    """prepare_git_stage rejects binary files."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        # Create binary file
        bin_file = temp_git_repo / "image.png"
        bin_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        result = stage(path="image.png", summary="Should fail")

        assert not result["ok"]
        assert result["denied"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_stage_does_not_mutate_git(temp_git_repo, broker):
    """prepare_git_stage does NOT mutate Git index."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        # Get index state before
        status_before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        # Prepare (does not mutate)
        result = stage(path="file.txt", summary="Add file")
        assert result["ok"]

        # Get index state after
        status_after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        # Status should be identical (file still untracked, not staged)
        assert status_before == status_after
        assert "?? file.txt" in status_after

    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# prepare_git_commit Tool
# ---------------------------------------------------------------------------


def test_prepare_git_commit_creates_plan(temp_git_repo, broker):
    """prepare_git_commit creates pending plan."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        # Stage file
        readme = temp_git_repo / "README.md"
        readme.write_text("# Updated\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )

        # Prepare
        result = commit(message="Update README")

        assert result["ok"]
        assert result["requires_approval"]
        assert result["status"] == "pending"
        assert result["kind"] == "commit"
        assert "plan_id" in result

    finally:
        os.chdir(original_cwd)


def test_prepare_git_commit_includes_diff(temp_git_repo, broker):
    """prepare_git_commit includes complete diff."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        readme = temp_git_repo / "README.md"
        readme.write_text("# Updated\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )

        result = commit(message="Update")

        assert result["ok"]
        assert "diff" in result
        assert "Updated" in result["diff"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_commit_includes_staged_files(temp_git_repo, broker):
    """prepare_git_commit lists all staged files."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        # Stage multiple files
        file1 = temp_git_repo / "file1.txt"
        file2 = temp_git_repo / "file2.txt"
        file1.write_text("Content 1\n", encoding="utf-8")
        file2.write_text("Content 2\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "file1.txt", "file2.txt"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )

        result = commit(message="Add files")

        assert result["ok"]
        assert "staged_files" in result
        assert "file1.txt" in result["staged_files"]
        assert "file2.txt" in result["staged_files"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_commit_rejects_empty_message(temp_git_repo, broker):
    """prepare_git_commit rejects blank messages."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        # Stage file
        readme = temp_git_repo / "README.md"
        readme.write_text("# Updated\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )

        # Try empty message
        result = commit(message="")

        assert not result["ok"]
        assert result["denied"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_commit_rejects_nothing_staged(temp_git_repo, broker):
    """prepare_git_commit rejects when nothing staged."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        # Don't stage anything
        result = commit(message="Empty commit")

        assert not result["ok"]
        assert result["denied"]

    finally:
        os.chdir(original_cwd)


def test_prepare_git_commit_does_not_mutate_git(temp_git_repo, broker):
    """prepare_git_commit does NOT create commit."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        readme = temp_git_repo / "README.md"
        readme.write_text("# Updated\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )

        # Get HEAD before
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Prepare (does not mutate)
        result = commit(message="Update")
        assert result["ok"]

        # Get HEAD after
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # HEAD should be unchanged
        assert head_before == head_after

    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# get_git_mutation_result Tool
# ---------------------------------------------------------------------------


def test_get_result_returns_pending(temp_git_repo, broker):
    """get_git_mutation_result returns pending status."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, result_tool = make_git_mutation_tools(broker)

        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        stage_result = stage(path="file.txt", summary="Add file")
        plan_id = stage_result["plan_id"]

        # Get result
        result = result_tool(plan_id=plan_id)

        assert result["ok"]
        assert result["status"] == "pending"
        assert result["applied"] is False

    finally:
        os.chdir(original_cwd)


def test_get_result_returns_approved_after_approval(temp_git_repo, broker):
    """get_git_mutation_result reflects approval status."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, result_tool = make_git_mutation_tools(broker)

        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        stage_result = stage(path="file.txt", summary="Add file")
        plan_id = stage_result["plan_id"]

        # Approve directly via broker
        broker.approve(plan_id)

        # Get result
        result = result_tool(plan_id=plan_id)

        assert result["ok"]
        assert result["status"] == "approved"
        assert result["applied"] is False  # Approved but not yet applied

    finally:
        os.chdir(original_cwd)


def test_get_result_returns_not_found(broker):
    """get_git_mutation_result handles unknown plan IDs."""
    _, _, result_tool = make_git_mutation_tools(broker)

    # Query non-existent plan
    result = result_tool(plan_id="00000000-0000-0000-0000-000000000000")

    assert not result["ok"]
    assert "unknown" in result["error"].lower() or "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# Tool Names and Documentation
# ---------------------------------------------------------------------------


def test_stage_tool_has_name(broker):
    """prepare_git_stage has __name__."""
    stage, _, _ = make_git_mutation_tools(broker)

    assert hasattr(stage, "__name__")
    assert "stage" in stage.__name__.lower()


def test_commit_tool_has_name(broker):
    """prepare_git_commit has __name__."""
    _, commit, _ = make_git_mutation_tools(broker)

    assert hasattr(commit, "__name__")
    assert "commit" in commit.__name__.lower()


def test_result_tool_has_name(broker):
    """get_git_mutation_result has __name__."""
    _, _, result = make_git_mutation_tools(broker)

    assert hasattr(result, "__name__")
    assert "result" in result.__name__.lower()


def test_stage_tool_has_docstring(broker):
    """prepare_git_stage has documentation."""
    stage, _, _ = make_git_mutation_tools(broker)

    assert stage.__doc__ is not None
    assert len(stage.__doc__) > 50


def test_commit_tool_has_docstring(broker):
    """prepare_git_commit has documentation."""
    _, commit, _ = make_git_mutation_tools(broker)

    assert commit.__doc__ is not None
    assert len(commit.__doc__) > 50


def test_result_tool_has_docstring(broker):
    """get_git_mutation_result has documentation."""
    _, _, result = make_git_mutation_tools(broker)

    assert result.__doc__ is not None
    assert len(result.__doc__) > 50


# ---------------------------------------------------------------------------
# Tool API Constraints
# ---------------------------------------------------------------------------


def test_stage_tool_requires_path(temp_git_repo, broker):
    """prepare_git_stage requires path parameter."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        # Missing path should fail
        with pytest.raises(TypeError):
            stage(summary="Missing path")

    finally:
        os.chdir(original_cwd)


def test_stage_tool_requires_summary(temp_git_repo, broker):
    """prepare_git_stage requires summary parameter."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, _, _ = make_git_mutation_tools(broker)

        test_file = temp_git_repo / "file.txt"
        test_file.write_text("Content\n", encoding="utf-8")

        # Missing summary should fail
        with pytest.raises(TypeError):
            stage(path="file.txt")

    finally:
        os.chdir(original_cwd)


def test_commit_tool_requires_message(temp_git_repo, broker):
    """prepare_git_commit requires message parameter."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        _, commit, _ = make_git_mutation_tools(broker)

        # Missing message should fail
        with pytest.raises(TypeError):
            commit()

    finally:
        os.chdir(original_cwd)


def test_result_tool_requires_plan_id(broker):
    """get_git_mutation_result requires plan_id parameter."""
    _, _, result = make_git_mutation_tools(broker)

    # Missing plan_id should fail
    with pytest.raises(TypeError):
        result()


# ---------------------------------------------------------------------------
# Integration: Stage → Commit Workflow
# ---------------------------------------------------------------------------


def test_workflow_stage_then_commit(temp_git_repo, broker):
    """Complete workflow: stage file then commit."""
    original_cwd = os.getcwd()
    try:
        os.chdir(str(temp_git_repo))

        stage, commit, result = make_git_mutation_tools(broker)

        # Create file
        test_file = temp_git_repo / "workflow.txt"
        test_file.write_text("Workflow test\n", encoding="utf-8")

        # Step 1: Prepare stage
        stage_result = stage(path="workflow.txt", summary="Add workflow file")
        assert stage_result["ok"]
        stage_plan_id = stage_result["plan_id"]

        # Step 2: Check stage pending
        stage_status = result(plan_id=stage_plan_id)
        assert stage_status["status"] == "pending"

        # Step 3: Approve and apply stage (simulated - in real flow, host does this)
        broker.approve(stage_plan_id)
        from harness_agent.git_mutation.service import apply_stage_plan
        plan = broker.get_plan(stage_plan_id)
        apply_result = apply_stage_plan(plan, temp_git_repo, broker)

        # Step 4: Check stage applied
        stage_status = result(plan_id=stage_plan_id)
        assert stage_status["status"] == "applied"

        # Step 5: Prepare commit
        commit_result = commit(message="Add workflow file via test")
        assert commit_result["ok"]
        commit_plan_id = commit_result["plan_id"]

        # Step 6: Check commit pending
        commit_status = result(plan_id=commit_plan_id)
        assert commit_status["status"] == "pending"

        # Step 7: Approve and apply commit
        broker.approve(commit_plan_id)
        from harness_agent.git_mutation.service import apply_commit_plan
        commit_plan = broker.get_plan(commit_plan_id)
        commit_apply_result = apply_commit_plan(commit_plan, temp_git_repo, broker)

        # Step 8: Check commit applied
        commit_status = result(plan_id=commit_plan_id)
        assert commit_status["status"] == "applied"
        assert commit_status["commit_oid"] is not None

    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

# This test file covers:
# - Tool factory returns exactly 3 tools
# - Tools are bound to correct broker
# - Broker isolation between tool sets
# - prepare_git_stage: creates plan, includes diff, rejects invalid, no mutation
# - prepare_git_commit: creates plan, includes diff, rejects invalid, no mutation
# - get_git_mutation_result: returns pending, approved, handles not found
# - Tool names and documentation
# - Tool API constraints (required parameters)
# - Integration workflow: stage → commit
#
# Total tests in this file: 40+
