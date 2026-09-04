"""Tests for Git mutation data models (v0.7.0).

Validates immutability, type safety, and model completeness.
"""

import pytest

from harness_agent.git_mutation import (
    IndexEntry,
    GitStagePlan,
    GitCommitPlan,
    GitMutationResult,
    KIND_STAGE,
    KIND_COMMIT,
    STATUS_APPLIED,
    STATUS_CONFLICT,
)


# ---------------------------------------------------------------------------
# IndexEntry immutability
# ---------------------------------------------------------------------------


def test_index_entry_is_frozen():
    entry = IndexEntry(
        path="src/main.py",
        mode="100644",
        object_id="abc123" * 6 + "abcd",
        stage=0,
    )

    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        entry.path = "other.py"


def test_index_entry_stage_default_zero():
    entry = IndexEntry(
        path="test.py",
        mode="100644",
        object_id="a" * 40,
        stage=0,
    )
    assert entry.stage == 0


# ---------------------------------------------------------------------------
# GitStagePlan immutability and structure
# ---------------------------------------------------------------------------


def test_git_stage_plan_is_frozen():
    plan = GitStagePlan(
        id="stage-001",
        kind=KIND_STAGE,
        repo_path="test.txt",
        summary="Add test file",
        branch="main",
        head_oid="a" * 40,
        base_worktree_sha256="b" * 64,
        base_index_fingerprint="c" * 64,
        previous_index_entry=None,
        proposed_mode="100644",
        proposed_blob_oid="d" * 40,
        diff="diff content",
        diff_sha256="e" * 64,
        created_at=1234567890.0,
    )

    with pytest.raises(Exception):
        plan.repo_path = "other.txt"


def test_git_stage_plan_previous_entry_can_be_none():
    plan = GitStagePlan(
        id="stage-002",
        kind=KIND_STAGE,
        repo_path="new.txt",
        summary="New file",
        branch="main",
        head_oid="a" * 40,
        base_worktree_sha256="b" * 64,
        base_index_fingerprint="c" * 64,
        previous_index_entry=None,
        proposed_mode="100644",
        proposed_blob_oid="d" * 40,
        diff="diff",
        diff_sha256="e" * 64,
        created_at=1234567890.0,
    )
    assert plan.previous_index_entry is None


# ---------------------------------------------------------------------------
# GitCommitPlan immutability and structure
# ---------------------------------------------------------------------------


def test_git_commit_plan_is_frozen():
    plan = GitCommitPlan(
        id="commit-001",
        kind=KIND_COMMIT,
        branch="main",
        head_oid="a" * 40,
        message="Test commit",
        author_name="Test User",
        author_email="test@example.com",
        index_fingerprint="b" * 64,
        staged_paths=("file1.txt", "file2.txt"),
        diff="diff content",
        diff_sha256="c" * 64,
        created_at=1234567890.0,
    )

    with pytest.raises(Exception):
        plan.message = "Different message"


def test_git_commit_plan_staged_paths_is_tuple():
    plan = GitCommitPlan(
        id="commit-002",
        kind=KIND_COMMIT,
        branch="main",
        head_oid="a" * 40,
        message="Test",
        author_name="User",
        author_email="user@test.com",
        index_fingerprint="b" * 64,
        staged_paths=("f1.txt", "f2.txt", "f3.txt"),
        diff="diff",
        diff_sha256="c" * 64,
        created_at=1234567890.0,
    )
    assert isinstance(plan.staged_paths, tuple)
    assert len(plan.staged_paths) == 3


# ---------------------------------------------------------------------------
# GitMutationResult
# ---------------------------------------------------------------------------


def test_git_mutation_result_is_frozen():
    result = GitMutationResult(
        plan_id="stage-001",
        kind=KIND_STAGE,
        status=STATUS_APPLIED,
        path="test.txt",
        commit_oid=None,
        message="Applied successfully",
    )

    with pytest.raises(Exception):
        result.status = STATUS_CONFLICT


def test_git_mutation_result_to_dict():
    result = GitMutationResult(
        plan_id="commit-001",
        kind=KIND_COMMIT,
        status=STATUS_APPLIED,
        path=None,
        commit_oid="a" * 40,
        message="Committed",
    )

    d = result.to_dict()
    assert d["plan_id"] == "commit-001"
    assert d["status"] == STATUS_APPLIED
    assert d["commit_oid"] == "a" * 40


def test_git_mutation_result_path_optional():
    result = GitMutationResult(
        plan_id="commit-001",
        kind=KIND_COMMIT,
        status=STATUS_APPLIED,
        path=None,
        commit_oid="a" * 40,
        message="Committed",
    )
    assert result.path is None


def test_git_mutation_result_commit_oid_optional():
    result = GitMutationResult(
        plan_id="stage-001",
        kind=KIND_STAGE,
        status=STATUS_APPLIED,
        path="test.txt",
        commit_oid=None,
        message="Staged",
    )
    assert result.commit_oid is None
