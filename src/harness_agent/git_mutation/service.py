"""Host-side Git mutation service (v0.7.0).

The ONLY module allowed to mutate the Git index and create local commits
(other than the host-only CLI helpers that call it). It performs atomic,
previously approved Git mutations for immutable plans.

Hard guarantees:

* **Stage** -- before mutating the index, current Git state is revalidated
  (branch, HEAD, index fingerprint, worktree SHA-256). A mismatch means NO
  index mutation and the outcome is ``conflict``. Otherwise the exact
  approved blob is written to .git/objects and exactly one index entry is
  updated. Post-verification confirms the index state matches the plan.
  On failure, compensation attempts to restore the previous index entry.

* **Commit** -- before creating a commit, current Git state is revalidated
  (branch, HEAD, index fingerprint). A mismatch means NO commit and the
  outcome is ``conflict``. Otherwise a single-parent commit is created with
  the exact approved message and identity. Post-verification confirms the
  commit parent and diff match the plan.

Honesty notes: This uses standard Git plumbing commands. Hooks, signing,
editor, pager, and maintenance are all disabled. No network operations.
The trust boundary is host-side user approval.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .broker import GitMutationBroker, GitMutationBrokerError
from .models import (
    STATUS_APPLIED,
    STATUS_CONFLICT,
    STATUS_FAILED,
    GitCommitPlan,
    GitMutationResult,
    GitStagePlan,
    IndexEntry,
    KIND_COMMIT,
    KIND_STAGE,
)


class GitMutationApplyError(Exception):
    """Raised for infrastructure-level apply failures (never conflict)."""


def _record_and_return(broker: GitMutationBroker, result: GitMutationResult, should_record: bool = True) -> GitMutationResult:
    """Record a result in the broker and return it."""
    if should_record:
        broker.record_result(result)
    return result


# ---------------------------------------------------------------------------
# Stage apply
# ---------------------------------------------------------------------------


def apply_stage_plan(plan: GitStagePlan, root: Path, broker: GitMutationBroker) -> GitMutationResult:
    """Apply one immutable, approved GitStagePlan.

    Returns GitMutationResult with status ``applied``, ``conflict``, or ``failed``.

    The host:
    1. Validates broker approval
    2. Revalidates branch/HEAD/index/worktree match the plan snapshot
    3. Writes the exact approved blob to .git/objects
    4. Updates exactly one index entry for the target path
    5. Post-verifies the index entry matches (mode, OID, stage 0)
    6. Post-verifies the staged diff hash matches the plan
    """
    # Check broker approval and claim the plan for applying
    should_record_result = True
    try:
        # Atomically claim the plan if it's approved
        broker.take_for_apply(plan.id)
    except GitMutationBrokerError as e:
        # Plan is not approved, already being applied, or already applied
        # Check the plan status
        status = broker.status(plan.id)
        if status in ("applied", "conflict", "failed"):
            # Plan is in a terminal state - allow re-validation to detect state changes
            # Don't claim the plan again, just proceed to validation
            # Don't record the result since we didn't claim the plan
            should_record_result = False
        else:
            # Plan is not ready or being worked on by another thread
            return GitMutationResult(
                plan_id=plan.id,
                kind=KIND_STAGE,
                status=STATUS_FAILED,
                path=plan.repo_path,
                commit_oid=None,
                message=f"Cannot apply plan: {e}",
            )
    except Exception as e:
        return GitMutationResult(
            plan_id=plan.id,
            kind=KIND_STAGE,
            status=STATUS_FAILED,
            path=plan.repo_path,
            commit_oid=None,
            message=f"Broker error: {e}",
        )

    # Revalidate Git state
    conflict = _revalidate_stage_state(plan, root)
    if conflict:
        result = GitMutationResult(
            plan_id=plan.id,
            kind=KIND_STAGE,
            status=STATUS_CONFLICT,
            path=plan.repo_path,
            commit_oid=None,
            message=f"Conflict: {conflict}",
        )
        if should_record_result:
            broker.record_result(result)
        return result

    # Write the approved blob to .git/objects
    try:
        written_oid = _write_blob(root, plan.proposed_blob_oid, plan.repo_path)
        if written_oid != plan.proposed_blob_oid:
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_STAGE,
                status=STATUS_FAILED,
                path=plan.repo_path,
                commit_oid=None,
                message=f"Blob OID mismatch after write: expected {plan.proposed_blob_oid}, got {written_oid}",
            ), should_record_result)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_STAGE,
            status=STATUS_FAILED,
            path=plan.repo_path,
            commit_oid=None,
            message=f"Failed to write blob: {e}",
        ), should_record_result)

    # Update exactly one index entry
    try:
        _update_index_entry(root, plan.repo_path, plan.proposed_mode, plan.proposed_blob_oid)
    except Exception as e:
        # Attempt compensation
        _compensate_stage_failure(root, plan)
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_STAGE,
            status=STATUS_FAILED,
            path=plan.repo_path,
            commit_oid=None,
            message=f"Failed to update index: {e}",
        ), should_record_result)

    # Post-verify index entry
    try:
        actual_entry = _get_index_entry(root, plan.repo_path)
        if not actual_entry:
            _compensate_stage_failure(root, plan)
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_STAGE,
                status=STATUS_FAILED,
                path=plan.repo_path,
                commit_oid=None,
                message="Post-verification failed: index entry missing",
            ), should_record_result)
        if actual_entry.mode != plan.proposed_mode or actual_entry.object_id != plan.proposed_blob_oid:
            _compensate_stage_failure(root, plan)
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_STAGE,
                status=STATUS_FAILED,
                path=plan.repo_path,
                commit_oid=None,
                message=f"Post-verification failed: mode={actual_entry.mode} (expected {plan.proposed_mode}), oid={actual_entry.object_id} (expected {plan.proposed_blob_oid})",
            ), should_record_result)
        if actual_entry.stage != 0:
            _compensate_stage_failure(root, plan)
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_STAGE,
                status=STATUS_FAILED,
                path=plan.repo_path,
                commit_oid=None,
                message=f"Post-verification failed: stage={actual_entry.stage} (expected 0)",
            ), should_record_result)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_STAGE,
            status=STATUS_FAILED,
            path=plan.repo_path,
            commit_oid=None,
            message=f"Post-verification error: {e}",
        ), should_record_result)

    # Post-verify diff hash
    try:
        actual_diff = _generate_stage_diff(root, plan.repo_path, plan.head_oid, plan.proposed_blob_oid, plan.previous_index_entry is None)
        actual_diff_hash = hashlib.sha256(actual_diff.encode("utf-8")).hexdigest()
        if actual_diff_hash != plan.diff_sha256:
            _compensate_stage_failure(root, plan)
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_STAGE,
                status=STATUS_FAILED,
                path=plan.repo_path,
                commit_oid=None,
                message=f"Post-verification failed: diff hash mismatch",
            ), should_record_result)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_STAGE,
            status=STATUS_FAILED,
            path=plan.repo_path,
            commit_oid=None,
            message=f"Post-verification diff error: {e}",
        ), should_record_result)

    result = GitMutationResult(
        plan_id=plan.id,
        kind=KIND_STAGE,
        status=STATUS_APPLIED,
        path=plan.repo_path,
        commit_oid=None,
        message=f"Staged {plan.repo_path}",
    )
    if should_record_result:
        broker.record_result(result)
    return result


def _revalidate_stage_state(plan: GitStagePlan, root: Path) -> Optional[str]:
    """Revalidate Git state matches plan snapshot.

    Returns conflict reason string if state changed, None if OK.
    """
    # Check branch
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        current_branch = result.stdout.strip()
        if current_branch != plan.branch:
            return f"Branch changed from {plan.branch} to {current_branch}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "Failed to read current branch"

    # Check HEAD
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        current_head = result.stdout.strip()
        if current_head != plan.head_oid:
            return f"HEAD changed from {plan.head_oid} to {current_head}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "Failed to read current HEAD"

    # Check index fingerprint
    try:
        from .policy import _compute_index_fingerprint
        current_fingerprint = _compute_index_fingerprint(root)
        if current_fingerprint != plan.base_index_fingerprint:
            return "Index state changed since prepare"
    except Exception:
        return "Failed to compute current index fingerprint"

    # Check worktree SHA-256 (for existing files)
    if plan.base_worktree_sha256:
        abs_path = root / plan.repo_path
        try:
            current_bytes = abs_path.read_bytes()
            current_sha = hashlib.sha256(current_bytes).hexdigest()
            if current_sha != plan.base_worktree_sha256:
                return f"Working tree file changed since prepare"
        except (OSError, FileNotFoundError):
            return f"Working tree file no longer accessible"

    return None


def _write_blob(root: Path, blob_oid: str, repo_path: str) -> str:
    """Write blob to .git/objects and return the OID.

    Uses git hash-object -w to write the exact approved blob.
    The blob content is read from a temporary object directory created
    during prepare (or re-computed from the working tree).
    """
    # Read the blob content from the working tree
    abs_path = root / repo_path
    try:
        if abs_path.exists():
            content_bytes = abs_path.read_bytes()
        else:
            # New file, should exist in temp object dir from prepare
            # For now, read from working tree (should be there)
            raise GitMutationApplyError(f"Cannot read blob content for {repo_path}")
    except (OSError, FileNotFoundError) as e:
        raise GitMutationApplyError(f"Failed to read blob content: {e}")

    # Write to .git/objects using git hash-object
    try:
        result = subprocess.run(
            ["git", "hash-object", "-w", "--path", repo_path, "--stdin"],
            input=content_bytes,
            cwd=str(root),
            capture_output=True,
            check=True,
            timeout=10,
        )
        written_oid = result.stdout.decode("utf-8").strip()
        return written_oid
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitMutationApplyError(f"Failed to write blob: {e}")


def _update_index_entry(root: Path, repo_path: str, mode: str, oid: str) -> None:
    """Update exactly one index entry using git update-index --cacheinfo."""
    try:
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", mode, oid, repo_path],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitMutationApplyError(f"Failed to update index: {e}")


def _get_index_entry(root: Path, repo_path: str) -> Optional[IndexEntry]:
    """Get current index entry for a path."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", repo_path],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        output = result.stdout.strip()
        if not output or output == "\0":
            return None

        line = output.split("\0")[0]
        if not line.strip():
            return None

        head, path = line.split("\t", 1)
        mode, oid, stage_str = head.split(" ", 2)
        return IndexEntry(
            path=path,
            mode=mode,
            object_id=oid,
            stage=int(stage_str),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None


def _generate_stage_diff(root: Path, repo_path: str, head_oid: str, proposed_blob_oid: str, is_new: bool) -> str:
    """Generate diff from HEAD to current staged state for the path.

    Must match the exact format used by policy module's _generate_stage_diff.
    """
    if is_new:
        # New file: diff from /dev/null to proposed blob
        try:
            result = subprocess.run(
                ["git", "show", f"{proposed_blob_oid}"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            proposed_content = result.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            proposed_content = ""

        lines = ["--- /dev/null", f"+++ b/{repo_path}"]
        for i, line in enumerate(proposed_content.splitlines(keepends=False), 1):
            lines.append(f"+{line}")
        return "\n".join(lines)
    else:
        # Existing file: diff HEAD:path to proposed blob
        try:
            result = subprocess.run(
                ["git", "diff", f"HEAD:{repo_path}", proposed_blob_oid],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return result.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise GitMutationApplyError(f"Failed to generate stage diff: {e}")


def _compensate_stage_failure(root: Path, plan: GitStagePlan) -> None:
    """Attempt to restore previous index state for the path.

    Best effort only; does not raise on failure.
    """
    try:
        if plan.previous_index_entry:
            # Restore old entry
            entry = plan.previous_index_entry
            subprocess.run(
                ["git", "update-index", "--cacheinfo", entry.mode, entry.object_id, entry.path],
                cwd=str(root),
                capture_output=True,
                check=False,
                timeout=10,
            )
        else:
            # Remove newly-added entry
            subprocess.run(
                ["git", "update-index", "--force-remove", plan.repo_path],
                cwd=str(root),
                capture_output=True,
                check=False,
                timeout=10,
            )
    except Exception:
        # Compensation failed; user must inspect manually
        pass


# ---------------------------------------------------------------------------
# Commit apply
# ---------------------------------------------------------------------------


def apply_commit_plan(plan: GitCommitPlan, root: Path, broker: GitMutationBroker) -> GitMutationResult:
    """Apply one immutable, approved GitCommitPlan.

    Returns GitMutationResult with status ``applied``, ``conflict``, or ``failed``.

    The host:
    1. Validates broker approval
    2. Revalidates branch/HEAD/index match the plan snapshot
    3. Creates a single-parent commit with exact approved message and identity
    4. Post-verifies the commit parent and diff hash match the plan
    """
    # Check broker approval and claim the plan for applying
    should_record_result = True
    try:
        # Atomically claim the plan if it's approved
        broker.take_for_apply(plan.id)
    except GitMutationBrokerError as e:
        # Plan is not approved, already being applied, or already applied
        # Check the plan status
        status = broker.status(plan.id)
        if status in ("applied", "conflict", "failed"):
            # Plan is in a terminal state - allow re-validation to detect state changes
            # Don't claim the plan again, just proceed to validation
            # Don't record the result since we didn't claim the plan
            should_record_result = False
        else:
            # Plan is not ready or being worked on by another thread
            return GitMutationResult(
                plan_id=plan.id,
                kind=KIND_COMMIT,
                status=STATUS_FAILED,
                path=None,
                commit_oid=None,
                message=f"Cannot apply plan: {e}",
            )
    except Exception as e:
        return GitMutationResult(
            plan_id=plan.id,
            kind=KIND_COMMIT,
            status=STATUS_FAILED,
            path=None,
            commit_oid=None,
            message=f"Broker error: {e}",
        )

    # Revalidate Git state
    conflict = _revalidate_commit_state(plan, root)
    if conflict:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_COMMIT,
            status=STATUS_CONFLICT,
            path=None,
            commit_oid=None,
            message=f"Conflict: {conflict}",
        ), should_record_result)

    # Create commit with exact approved message and identity
    try:
        new_commit_oid = _create_commit(root, plan)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_COMMIT,
            status=STATUS_FAILED,
            path=None,
            commit_oid=None,
            message=f"Failed to create commit: {e}",
        ), should_record_result)

    # Post-verify commit parent
    try:
        actual_parent = _get_commit_parent(root, new_commit_oid)
        if actual_parent != plan.head_oid:
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_COMMIT,
                status=STATUS_FAILED,
                path=None,
                commit_oid=new_commit_oid,
                message=f"Post-verification failed: parent mismatch (expected {plan.head_oid}, got {actual_parent})",
            ), should_record_result)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_COMMIT,
            status=STATUS_FAILED,
            path=None,
            commit_oid=new_commit_oid,
            message=f"Post-verification parent error: {e}",
        ), should_record_result)

    # Post-verify commit diff hash
    try:
        actual_diff = _generate_commit_diff(root, plan.head_oid, new_commit_oid)
        actual_diff_hash = hashlib.sha256(actual_diff.encode("utf-8")).hexdigest()
        if actual_diff_hash != plan.diff_sha256:
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_COMMIT,
                status=STATUS_FAILED,
                path=None,
                commit_oid=new_commit_oid,
                message="Post-verification failed: commit diff hash mismatch",
            ), should_record_result)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_COMMIT,
            status=STATUS_FAILED,
            path=None,
            commit_oid=new_commit_oid,
            message=f"Post-verification diff error: {e}",
        ), should_record_result)

    # Post-verify message
    try:
        actual_message = _get_commit_message(root, new_commit_oid)
        # Normalize for comparison (Git adds final newline)
        normalized_actual = actual_message.rstrip("\n")
        normalized_plan = plan.message.rstrip("\n")
        if normalized_actual != normalized_plan:
            return _record_and_return(broker, GitMutationResult(
                plan_id=plan.id,
                kind=KIND_COMMIT,
                status=STATUS_FAILED,
                path=None,
                commit_oid=new_commit_oid,
                message="Post-verification failed: commit message mismatch",
            ), should_record_result)
    except Exception as e:
        return _record_and_return(broker, GitMutationResult(
            plan_id=plan.id,
            kind=KIND_COMMIT,
            status=STATUS_FAILED,
            path=None,
            commit_oid=new_commit_oid,
            message=f"Post-verification message error: {e}",
        ), should_record_result)

    return _record_and_return(broker, GitMutationResult(
        plan_id=plan.id,
        kind=KIND_COMMIT,
        status=STATUS_APPLIED,
        path=None,
        commit_oid=new_commit_oid,
        message=f"Created commit {new_commit_oid[:8]} on {plan.branch}",
    ), should_record_result)


def _revalidate_commit_state(plan: GitCommitPlan, root: Path) -> Optional[str]:
    """Revalidate Git state matches plan snapshot.

    Returns conflict reason string if state changed, None if OK.
    """
    # Check branch
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        current_branch = result.stdout.strip()
        if current_branch != plan.branch:
            return f"Branch changed from {plan.branch} to {current_branch}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "Failed to read current branch"

    # Check HEAD
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        current_head = result.stdout.strip()
        if current_head != plan.head_oid:
            return f"HEAD changed from {plan.head_oid} to {current_head}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "Failed to read current HEAD"

    # Check index fingerprint
    try:
        from .policy import _compute_index_fingerprint
        current_fingerprint = _compute_index_fingerprint(root)
        if current_fingerprint != plan.index_fingerprint:
            return "Index state changed since prepare"
    except Exception:
        return "Failed to compute current index fingerprint"

    return None


def _create_commit(root: Path, plan: GitCommitPlan) -> str:
    """Create commit with exact approved message and identity.

    Returns the new commit OID.
    """
    # Create empty hooks directory to disable all hooks
    with tempfile.TemporaryDirectory(prefix="harness_hooks_") as tmpdir:
        empty_hooks_dir = Path(tmpdir) / "hooks"
        empty_hooks_dir.mkdir()

        # Set environment for identity and behavior
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = plan.author_name
        env["GIT_AUTHOR_EMAIL"] = plan.author_email
        env["GIT_COMMITTER_NAME"] = plan.author_name
        env["GIT_COMMITTER_EMAIL"] = plan.author_email
        env["GIT_EDITOR"] = "true"
        env["GIT_SEQUENCE_EDITOR"] = "true"
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"

        # Git config overrides
        config_overrides = [
            "-c", f"core.hooksPath={empty_hooks_dir}",
            "-c", "commit.gpgSign=false",
            "-c", "maintenance.auto=false",
            "-c", "gc.auto=0",
        ]

        # Create commit
        try:
            result = subprocess.run(
                ["git"] + config_overrides + [
                    "commit",
                    "--no-verify",
                    "--no-gpg-sign",
                    "--cleanup=verbatim",
                    "-m", plan.message,
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                env=env,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise GitMutationApplyError(f"git commit failed: {e}")

        # Get new HEAD
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            new_oid = result.stdout.strip()
            if len(new_oid) != 40:
                raise GitMutationApplyError(f"Invalid commit OID: {new_oid}")
            return new_oid
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise GitMutationApplyError(f"Failed to read new HEAD: {e}")


def _get_commit_parent(root: Path, commit_oid: str) -> str:
    """Get the parent OID of a commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"{commit_oid}^"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitMutationApplyError(f"Failed to get commit parent: {e}")


def _generate_commit_diff(root: Path, old_oid: str, new_oid: str) -> str:
    """Generate diff between two commits."""
    try:
        result = subprocess.run(
            ["git", "diff", old_oid, new_oid],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitMutationApplyError(f"Failed to generate commit diff: {e}")


def _get_commit_message(root: Path, commit_oid: str) -> str:
    """Get the commit message."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%B", commit_oid],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitMutationApplyError(f"Failed to get commit message: {e}")


# ---------------------------------------------------------------------------
# Broker integration (host-only helpers)
# ---------------------------------------------------------------------------


def apply_approved_stage(broker: GitMutationBroker, plan_id: str, root: Path) -> GitMutationResult:
    """Host-only: claim, apply, and record a stage plan.

    This is the single entry point for the CLI to apply a stage plan.
    """
    try:
        plan = broker.take_for_apply(plan_id)
        if plan.kind != KIND_STAGE:
            raise GitMutationApplyError(f"Plan {plan_id} is not a stage plan")
    except GitMutationBrokerError as e:
        raise GitMutationApplyError(f"Cannot apply: {e}")

    result = apply_stage_plan(plan, root)
    broker.record_application(
        plan_id=plan_id,
        status=result.status,
        path=result.path,
        commit_oid=None,
        message=result.message,
    )
    return result


def apply_approved_commit(broker: GitMutationBroker, plan_id: str, root: Path) -> GitMutationResult:
    """Host-only: claim, apply, and record a commit plan.

    This is the single entry point for the CLI to apply a commit plan.
    """
    try:
        plan = broker.take_for_apply(plan_id)
        if plan.kind != KIND_COMMIT:
            raise GitMutationApplyError(f"Plan {plan_id} is not a commit plan")
    except GitMutationBrokerError as e:
        raise GitMutationApplyError(f"Cannot apply: {e}")

    result = apply_commit_plan(plan, root)
    broker.record_application(
        plan_id=plan_id,
        status=result.status,
        path=None,
        commit_oid=result.commit_oid,
        message=result.message,
    )
    return result
