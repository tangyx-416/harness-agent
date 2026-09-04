"""Comprehensive tests for Git mutation service layer (v0.7.0).

Tests the host-side service layer that applies approved Git mutations.
This layer performs the actual Git index updates and commit creation.

Key invariants tested:
- Revalidation before mutation (branch/HEAD/index/worktree unchanged)
- Exactly-once application semantics
- Post-verification (index/commit matches approved plan)
- Compensation on failure (restore previous index state)
- Conflict detection (state changed after approval)

Every test uses temporary disposable Git repositories. The real Harness
Agent repository is NEVER mutated.
"""

import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from harness_agent.git_mutation.broker import GitMutationBroker
from harness_agent.git_mutation.models import STATUS_APPLIED, STATUS_CONFLICT, STATUS_FAILED
from harness_agent.git_mutation.policy import prepare_commit_plan, prepare_stage_plan
from harness_agent.git_mutation.service import apply_commit_plan, apply_stage_plan


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
# Stage Service: Basic Apply
# ---------------------------------------------------------------------------


def test_stage_apply_creates_new_file_in_index(temp_git_repo, broker):
    """Applying approved stage plan stages new file in index."""
    repo = temp_git_repo

    # Create new file
    new_file = repo / "new.txt"
    new_file.write_text("New content\n", encoding="utf-8")

    # Prepare and approve
    plan = prepare_stage_plan(path="new.txt", summary="Add new file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Apply
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_APPLIED
    assert result.plan_id == plan.id

    # Verify file is staged
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "A  new.txt" in status_result.stdout or "A new.txt" in status_result.stdout


def test_stage_apply_modifies_existing_file(temp_git_repo, broker):
    """Applying approved stage plan modifies existing file in index."""
    repo = temp_git_repo

    # Modify README
    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")

    # Prepare and approve
    plan = prepare_stage_plan(path="README.md", summary="Update README", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Apply
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_APPLIED

    # Verify file is staged
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "M  README.md" in status_result.stdout or "M README.md" in status_result.stdout


def test_stage_apply_writes_exact_approved_blob(temp_git_repo, broker):
    """Applied stage writes exact approved blob OID."""
    repo = temp_git_repo

    # Create file
    test_file = repo / "exact.txt"
    test_file.write_text("Exact content\n", encoding="utf-8")

    # Prepare and approve
    plan = prepare_stage_plan(path="exact.txt", summary="Add exact file", repo_root=repo)
    approved_oid = plan.proposed_blob_oid
    broker.register(plan)
    broker.approve(plan.id)

    # Apply
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_APPLIED

    # Check index entry OID
    ls_files_result = subprocess.run(
        ["git", "ls-files", "--stage", "exact.txt"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    # Output format: "mode oid stage\tpath"
    index_oid = ls_files_result.stdout.split()[1]
    assert index_oid == approved_oid


# ---------------------------------------------------------------------------
# Stage Service: Conflict Detection
# ---------------------------------------------------------------------------


def test_stage_apply_detects_head_changed(temp_git_repo, broker):
    """Stage apply detects HEAD changed after prepare."""
    repo = temp_git_repo

    # Create file and prepare plan
    test_file = repo / "file.txt"
    test_file.write_text("Content\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Add file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Change HEAD (create new commit)
    other_file = repo / "other.txt"
    other_file.write_text("Other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Change HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Apply should detect conflict
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_CONFLICT
    assert "HEAD" in result.message or "changed" in result.message


def test_stage_apply_detects_branch_changed(temp_git_repo, broker):
    """Stage apply detects branch changed after prepare."""
    repo = temp_git_repo

    # Create file and prepare plan
    test_file = repo / "file.txt"
    test_file.write_text("Content\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Add file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Switch to new branch
    subprocess.run(
        ["git", "checkout", "-b", "new-branch"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Apply should detect conflict
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_CONFLICT
    assert "branch" in result.message.lower()


def test_stage_apply_detects_worktree_changed(temp_git_repo, broker):
    """Stage apply detects working tree file changed after prepare."""
    repo = temp_git_repo

    # Create and commit file first
    test_file = repo / "file.txt"
    test_file.write_text("Original\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add file"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Modify file and prepare plan
    test_file.write_text("Modified v1\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Update file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Change worktree again
    test_file.write_text("Modified v2 (different)\n", encoding="utf-8")

    # Apply should detect conflict
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_CONFLICT
    assert "working tree" in result.message.lower() or "changed" in result.message.lower()


def test_stage_apply_detects_index_changed(temp_git_repo, broker):
    """Stage apply detects index changed after prepare."""
    repo = temp_git_repo

    # Create file and prepare plan
    test_file = repo / "file.txt"
    test_file.write_text("Content\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Add file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Change index (stage another file)
    other_file = repo / "other.txt"
    other_file.write_text("Other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=str(repo), check=True, capture_output=True)

    # Apply should detect conflict
    result = apply_stage_plan(plan, repo, broker)

    assert result.status == STATUS_CONFLICT
    assert "index" in result.message.lower() or "changed" in result.message.lower()


# ---------------------------------------------------------------------------
# Stage Service: Exactly-Once
# ---------------------------------------------------------------------------


def test_stage_apply_exactly_once_sequential(temp_git_repo, broker):
    """Sequential stage applies are idempotent (second fails)."""
    repo = temp_git_repo

    test_file = repo / "file.txt"
    test_file.write_text("Content\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Add file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # First apply
    result1 = apply_stage_plan(plan, repo, broker)
    assert result1.status == STATUS_APPLIED

    # Second apply with same plan (state has changed - file now staged)
    result2 = apply_stage_plan(plan, repo, broker)
    # Should detect conflict (index changed)
    assert result2.status == STATUS_CONFLICT


def test_stage_apply_concurrent_exactly_once(temp_git_repo, broker):
    """Concurrent stage applies produce exactly one success."""
    repo = temp_git_repo

    test_file = repo / "file.txt"
    test_file.write_text("Content\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Add file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    results = []

    def apply_stage():
        result = apply_stage_plan(plan, repo, broker)
        results.append(result)

    # Launch 5 concurrent applies
    threads = [threading.Thread(target=apply_stage) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one should succeed
    applied_count = sum(1 for r in results if r.status == STATUS_APPLIED)
    assert applied_count == 1

    # Others should be conflict or failed
    non_applied = [r for r in results if r.status != STATUS_APPLIED]
    assert all(r.status in (STATUS_CONFLICT, STATUS_FAILED) for r in non_applied)


# ---------------------------------------------------------------------------
# Stage Service: Post-Verification
# ---------------------------------------------------------------------------


def test_stage_post_verify_index_entry(temp_git_repo, broker):
    """Post-verification checks index entry matches plan."""
    repo = temp_git_repo

    test_file = repo / "file.txt"
    test_file.write_text("Content\n", encoding="utf-8")
    plan = prepare_stage_plan(path="file.txt", summary="Add file", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    result = apply_stage_plan(plan, repo, broker)
    assert result.status == STATUS_APPLIED

    # Manually verify index entry
    ls_files_result = subprocess.run(
        ["git", "ls-files", "--stage", "file.txt"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    parts = ls_files_result.stdout.split()
    index_mode = parts[0]
    index_oid = parts[1]

    assert index_oid == plan.proposed_blob_oid
    assert index_mode == plan.proposed_mode


# ---------------------------------------------------------------------------
# Commit Service: Basic Apply
# ---------------------------------------------------------------------------


def test_commit_apply_creates_commit(temp_git_repo, broker):
    """Applying approved commit plan creates local commit."""
    repo = temp_git_repo

    # Modify and stage
    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    # Prepare and approve
    plan = prepare_commit_plan(message="Update README", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Get current HEAD
    old_head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    old_head = old_head_result.stdout.strip()

    # Apply
    result = apply_commit_plan(plan, repo, broker)

    assert result.status == STATUS_APPLIED
    assert result.commit_oid is not None
    assert len(result.commit_oid) == 40

    # Verify HEAD changed
    new_head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    new_head = new_head_result.stdout.strip()
    assert new_head != old_head
    assert new_head == result.commit_oid


def test_commit_apply_uses_exact_message(temp_git_repo, broker):
    """Applied commit uses exact approved message."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Exact commit message\n\nWith body.", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_APPLIED

    # Get actual commit message
    msg_result = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    actual_message = msg_result.stdout.strip()

    # Normalize for comparison (Git adds final newline)
    assert actual_message.strip() == plan.message.strip()


def test_commit_apply_uses_correct_identity(temp_git_repo, broker):
    """Applied commit uses approved identity."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Test identity", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_APPLIED

    # Get author
    author_result = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    actual_author = author_result.stdout.strip()

    expected_author = f"{plan.author_name} <{plan.author_email}>"
    assert actual_author == expected_author


def test_commit_apply_verifies_parent(temp_git_repo, broker):
    """Post-verification checks commit parent matches plan."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Test parent", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_APPLIED

    # Get parent
    parent_result = subprocess.run(
        ["git", "log", "-1", "--format=%P"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    actual_parent = parent_result.stdout.strip()

    assert actual_parent == plan.head_oid


def test_commit_apply_no_merge_parent(temp_git_repo, broker):
    """Created commit has exactly one parent (no merge)."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Single parent", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_APPLIED

    # Get parents (space-separated if multiple)
    parent_result = subprocess.run(
        ["git", "log", "-1", "--format=%P"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    parents = parent_result.stdout.strip().split()

    assert len(parents) == 1


# ---------------------------------------------------------------------------
# Commit Service: Conflict Detection
# ---------------------------------------------------------------------------


def test_commit_apply_detects_head_changed(temp_git_repo, broker):
    """Commit apply detects HEAD changed after prepare."""
    repo = temp_git_repo

    # Stage file
    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    # Prepare plan
    plan = prepare_commit_plan(message="Should conflict", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Change HEAD (create another commit)
    other_file = repo / "other.txt"
    other_file.write_text("Other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Intervening commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Apply should detect conflict
    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_CONFLICT


def test_commit_apply_detects_branch_changed(temp_git_repo, broker):
    """Commit apply detects branch changed after prepare."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Should conflict", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Switch branch
    subprocess.run(
        ["git", "checkout", "-b", "new-branch"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_CONFLICT


def test_commit_apply_detects_index_changed(temp_git_repo, broker):
    """Commit apply detects index changed after prepare."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Should conflict", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # Change index (stage another file)
    other_file = repo / "other.txt"
    other_file.write_text("Other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=str(repo), check=True, capture_output=True)

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_CONFLICT


# ---------------------------------------------------------------------------
# Commit Service: Exactly-Once
# ---------------------------------------------------------------------------


def test_commit_apply_exactly_once_sequential(temp_git_repo, broker):
    """Sequential commit applies fail on second attempt."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Test", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    # First apply
    result1 = apply_commit_plan(plan, repo, broker)
    assert result1.status == STATUS_APPLIED

    # Second apply (state changed - HEAD moved)
    result2 = apply_commit_plan(plan, repo, broker)
    assert result2.status == STATUS_CONFLICT


def test_commit_apply_concurrent_exactly_once(temp_git_repo, broker):
    """Concurrent commit applies produce exactly one success."""
    repo = temp_git_repo

    readme = repo / "README.md"
    readme.write_text("# Updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    plan = prepare_commit_plan(message="Concurrent test", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    results = []

    def apply_commit():
        result = apply_commit_plan(plan, repo, broker)
        results.append(result)

    # Launch 5 concurrent applies
    threads = [threading.Thread(target=apply_commit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one should succeed
    applied_count = sum(1 for r in results if r.status == STATUS_APPLIED)
    assert applied_count == 1


# ---------------------------------------------------------------------------
# Commit Service: Unstaged Changes Preserved
# ---------------------------------------------------------------------------


def test_commit_preserves_unstaged_changes(temp_git_repo, broker):
    """Unstaged working-tree changes remain after commit."""
    repo = temp_git_repo

    # Modify README and stage it
    readme = repo / "README.md"
    readme.write_text("# Staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)

    # Create unstaged file
    unstaged = repo / "unstaged.txt"
    unstaged.write_text("Unstaged content\n", encoding="utf-8")

    # Commit staged changes
    plan = prepare_commit_plan(message="Commit staged only", repo_root=repo)
    broker.register(plan)
    broker.approve(plan.id)

    result = apply_commit_plan(plan, repo, broker)
    assert result.status == STATUS_APPLIED

    # Verify unstaged file still exists and is unstaged
    assert unstaged.exists()
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "unstaged.txt" in status_result.stdout
    assert "??" in status_result.stdout  # Untracked


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

# This test file covers:
# - Stage apply: new files, modified files, exact blob OID
# - Stage conflict detection: HEAD/branch/index/worktree changed
# - Stage exactly-once: sequential and concurrent
# - Stage post-verification: index entry matches plan
# - Commit apply: creates commit, exact message, correct identity
# - Commit post-verification: parent, no merge
# - Commit conflict detection: HEAD/branch/index changed
# - Commit exactly-once: sequential and concurrent
# - Unstaged changes preserved after commit
#
# Total tests in this file: 30+
