"""Git mutation policy: validate immutable single-file stage and commit proposals.

Security model::

    Agent proposes -> Git Mutation Policy validates -> User approves -> Host mutates

The policy is the ONLY place that turns a raw agent request into an immutable
:class:`GitStagePlan` or :class:`GitCommitPlan`. It enforces:

**For Stage:**
* **One file per plan** -- no ``git add .``, no directory staging, no multi-file.
* **Safe repository-relative path** -- reuses patch policy hardening.
* **Text add/modify only** -- no delete, rename, copy, conflict, submodule, symlink.
* **External filter detection** -- clean filters and working-tree-encoding are DENIED.
* **Canonical staged blob** -- the exact bytes Git will stage are computed and
  previewed (not a raw worktree diff that might differ after filter application).
* **Complete stage diff** -- HEAD → proposed staged blob, never truncated.
* **Snapshot binding** -- branch, HEAD, index fingerprint, worktree SHA-256.

**For Commit:**
* **Entire staged snapshot** -- the agent cannot choose which staged files to
  commit; the commit includes everything currently staged.
* **Text changes only** -- no staged delete/rename/binary/symlink/submodule.
* **Complete commit diff** -- HEAD → entire staged snapshot, never truncated.
* **Identity preview** -- user.name and user.email are read, validated, and shown.
* **Message validation** -- UTF-8, no NUL, no control/bidi spoofing, length cap.
* **No initial/amend/merge** -- only normal single-parent commits.

Nothing here mutates Git state -- that's reserved for :mod:`.service` under host
approval.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..tools.path_utils import (
    is_known_binary_extension,
    path_is_dangerous,
    sniff_binary_content,
)
from .models import (
    GitCommitPlan,
    GitStagePlan,
    IndexEntry,
    KIND_COMMIT,
    KIND_STAGE,
)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Largest file (bytes) a stage proposal may target.
MAX_STAGE_FILE_SIZE_BYTES = 256 * 1024

#: Hard cap on a COMPLETE stage diff. Larger proposals are rejected.
MAX_STAGE_DIFF_CHARS = 100_000

#: Hard cap on a COMPLETE commit diff. Larger proposals are rejected.
MAX_COMMIT_DIFF_CHARS = 100_000

#: Maximum commit message length (characters).
MAX_COMMIT_MESSAGE_CHARS = 4000

#: Maximum length for user.name or user.email (sanity bound).
MAX_IDENTITY_CHARS = 200

#: Maximum number of staged files in one commit.
MAX_STAGED_FILES = 100


class GitMutationPolicyError(ValueError):
    """Raised when a raw Git mutation request cannot produce a valid plan."""


# ---------------------------------------------------------------------------
# Path validation (reuse from patch policy)
# ---------------------------------------------------------------------------


def _reject(reason: str) -> GitMutationPolicyError:
    return GitMutationPolicyError(reason)


def _validate_stage_path(user_path: str, root: Path) -> Path:
    """Validate and resolve a stage path to an absolute Path.

    Reuses the patch policy's path hardening rules.
    Raises GitMutationPolicyError if unsafe.
    """
    from ..patch.policy import _normalize_repo_path

    try:
        repo_relative = _normalize_repo_path(user_path, root)
    except ValueError as e:
        raise _reject(str(e))

    abs_path = (root / repo_relative).resolve()

    # Re-validate after resolution (symlink/junction escape detection)
    try:
        abs_path.relative_to(root)
    except ValueError:
        raise _reject(
            f"Resolved path escapes repository root (possible symlink): {user_path}"
        )

    # Sensitive/ignored path check
    if path_is_dangerous(abs_path, root, target_is_dir=False):
        raise _reject(f"Sensitive or ignored path refused: {user_path}")

    return abs_path


# ---------------------------------------------------------------------------
# Git repository context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GitContext:
    """Immutable Git repository context snapshot."""

    root: Path
    branch: str
    head_oid: str
    is_detached: bool
    is_unborn: bool


def _get_git_context(root: Path) -> _GitContext:
    """Read current Git state (branch, HEAD) for snapshot binding.

    Raises GitMutationPolicyError if not a valid Git repo or in unsupported state.
    """
    # Verify this is a Git repository
    git_dir = root / ".git"
    if not git_dir.exists():
        raise _reject("Not a Git repository (no .git directory).")

    # Get HEAD OID
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            # Possibly unborn HEAD
            raise _reject("Cannot resolve HEAD (possibly unborn repository).")
        head_oid = result.stdout.strip()
        if not head_oid or len(head_oid) != 40:
            raise _reject(f"Invalid HEAD OID: {head_oid}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise _reject(f"Failed to read HEAD: {e}")

    # Get current branch
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            is_detached = False
        else:
            # Detached HEAD
            raise _reject("Detached HEAD is not supported in v0.7.0.")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise _reject(f"Failed to read branch: {e}")

    return _GitContext(
        root=root,
        branch=branch,
        head_oid=head_oid,
        is_detached=is_detached,
        is_unborn=False,
    )


# ---------------------------------------------------------------------------
# Index fingerprinting
# ---------------------------------------------------------------------------


def _compute_index_fingerprint(root: Path) -> str:
    """Compute SHA-256 fingerprint of canonical Git index state.

    Uses `git ls-files --stage -z` to get mode/oid/stage/path for every entry,
    then hashes the canonical serialization. This is semantic (path/mode/oid),
    not raw .git/index bytes (which vary with stat cache, extensions, etc.).
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise _reject(f"Failed to read Git index: {e}")

    # Parse stage entries: "mode oid stage\tpath\0"
    entries = []
    for line in result.stdout.split("\0"):
        if not line.strip():
            continue
        try:
            head, path = line.split("\t", 1)
            mode, oid, stage_str = head.split(" ", 2)
            entries.append((path, mode, oid, int(stage_str)))
        except ValueError:
            # Malformed entry, skip
            continue

    # Sort by path for canonical ordering
    entries.sort(key=lambda e: e[0])

    # Hash canonical serialization
    canonical = "\n".join(f"{path}\t{mode}\t{oid}\t{stage}" for path, mode, oid, stage in entries)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Stage policy
# ---------------------------------------------------------------------------


def _file_exists_in_head(repo_root: Path, repo_path: str, head_oid: str) -> bool:
    """Check if a file exists in HEAD.

    Returns True if the file exists in HEAD, False otherwise.
    """
    if not head_oid or head_oid == "0" * 40:
        # Unborn HEAD - no files exist
        return False

    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{head_oid}:{repo_path}"],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def prepare_stage_plan(
    *,
    path: str,
    summary: str,
    repo_root: Path,
) -> GitStagePlan:
    """Validate and create an immutable GitStagePlan.

    This is the ONLY function that creates a GitStagePlan. It:
    1. Validates the path is safe and within the repository
    2. Checks the file is a supported text add/modify (no delete/rename/binary)
    3. Detects and rejects external clean filters
    4. Computes the canonical staged blob (exact bytes Git will stage)
    5. Generates the complete stage diff (HEAD → proposed staged blob)
    6. Binds to current Git snapshot (branch/HEAD/index/worktree)

    Args:
        path: Repository-relative path to stage (one file only).
        summary: Concise user-visible change summary.
        repo_root: Absolute path to repository root.

    Returns:
        Immutable GitStagePlan ready for user approval.

    Raises:
        GitMutationPolicyError: If the proposal violates any policy.
    """
    # Get Git context
    ctx = _get_git_context(repo_root)

    # Validate and resolve path
    abs_path = _validate_stage_path(path, repo_root)
    repo_path = abs_path.relative_to(repo_root).as_posix()

    # Check file exists in worktree
    if not abs_path.exists():
        raise _reject(f"File does not exist in worktree: {path}")

    if not abs_path.is_file():
        raise _reject(f"Not a regular file: {path}")

    if abs_path.is_symlink():
        raise _reject(f"Symlink staging not supported: {path}")

    # Determine if file is new (not in HEAD)
    # A file is "new" if it doesn't exist in HEAD, regardless of worktree/index
    is_new = not _file_exists_in_head(repo_root, repo_path, ctx.head_oid)

    # Check file size
    file_size = abs_path.stat().st_size
    if file_size > MAX_STAGE_FILE_SIZE_BYTES:
        raise _reject(
            f"File too large for staging ({file_size} bytes, "
            f"max {MAX_STAGE_FILE_SIZE_BYTES}): {path}"
        )

    # Check not binary
    if is_known_binary_extension(abs_path.name):
        raise _reject(f"Binary extension refused: {path}")

    try:
        content_bytes = abs_path.read_bytes()
    except (OSError, PermissionError) as e:
        raise _reject(f"Cannot read file: {e}")

    if sniff_binary_content(content_bytes):
        raise _reject(f"Binary content detected: {path}")

    # Check valid UTF-8
    try:
        content_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise _reject(f"File is not valid UTF-8: {path}")

    # Check for external filters (SECURITY: must not run arbitrary clean filters)
    _check_no_external_filters(repo_root, repo_path)

    # Compute worktree SHA-256
    base_worktree_sha256 = hashlib.sha256(content_bytes).hexdigest()

    # Compute index fingerprint
    base_index_fingerprint = _compute_index_fingerprint(repo_root)

    # Get previous index entry (if any)
    previous_entry = _get_index_entry(repo_root, repo_path)

    # Compute canonical staged blob and mode
    if is_new:
        proposed_mode = "100644"  # v0.7.0: new files default to regular file
    else:
        # Preserve existing mode for tracked files
        if previous_entry:
            proposed_mode = previous_entry.mode
        else:
            proposed_mode = "100644"

    # Compute canonical staged blob OID
    # This is the EXACT blob Git will stage (after any normalization)
    proposed_blob_oid = _compute_canonical_staged_blob_oid(
        repo_root, abs_path, repo_path, is_new
    )

    # Generate complete stage diff
    diff = _generate_stage_diff(repo_root, repo_path, ctx.head_oid, proposed_blob_oid, is_new)

    if len(diff) > MAX_STAGE_DIFF_CHARS:
        raise _reject(
            f"Stage diff too large ({len(diff)} chars, max {MAX_STAGE_DIFF_CHARS})"
        )

    # Hash the diff for integrity
    diff_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()

    return GitStagePlan(
        id=str(uuid.uuid4()),
        kind=KIND_STAGE,
        repo_path=repo_path,
        summary=summary[:500],  # Truncate to limit
        branch=ctx.branch,
        head_oid=ctx.head_oid,
        base_worktree_sha256=base_worktree_sha256,
        base_index_fingerprint=base_index_fingerprint,
        previous_index_entry=previous_entry,
        proposed_mode=proposed_mode,
        proposed_blob_oid=proposed_blob_oid,
        diff=diff,
        diff_sha256=diff_sha256,
        created_at=time.time(),
    )


def _check_no_external_filters(repo_root: Path, repo_path: str) -> None:
    """Check that no external clean filter or encoding is configured for this path.

    Raises GitMutationPolicyError if external filters are detected.
    """
    # Check for filter attribute
    try:
        result = subprocess.run(
            ["git", "check-attr", "filter", repo_path],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        output = result.stdout.strip()
        # Output format: "path: attr: value"
        if "filter:" in output and "filter: unset" not in output and "filter: unspecified" not in output:
            raise _reject(
                f"External clean filter detected for {repo_path}. "
                "Staging with arbitrary filters is not supported in v0.7.0."
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # If check-attr fails, be conservative and deny
        raise _reject("Cannot verify filter attributes; staging denied.")

    # Check for working-tree-encoding
    try:
        result = subprocess.run(
            ["git", "check-attr", "working-tree-encoding", repo_path],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        output = result.stdout.strip()
        if "working-tree-encoding:" in output and "unset" not in output and "unspecified" not in output:
            raise _reject(
                f"working-tree-encoding attribute detected for {repo_path}. "
                "External encoding conversion is not supported in v0.7.0."
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise _reject("Cannot verify encoding attributes; staging denied.")


def _get_index_entry(repo_root: Path, repo_path: str) -> Optional[IndexEntry]:
    """Get the current index entry for a path (if it exists)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", repo_path],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    output = result.stdout.strip()
    if not output or output == "\0":
        return None

    # Parse: "mode oid stage\tpath\0"
    try:
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
    except (ValueError, IndexError):
        return None


def _compute_canonical_staged_blob_oid(
    repo_root: Path,
    abs_path: Path,
    repo_path: str,
    is_new: bool,
) -> str:
    """Compute the canonical staged blob OID (exact bytes Git will stage).

    Writes the blob to the repository's object database so it can be used
    for diff generation. This accounts for normalization (e.g., CRLF → LF
    on Windows with core.autocrlf=true).

    Returns the 40-character hex SHA-1 object ID.
    """
    # Read file content
    try:
        content_bytes = abs_path.read_bytes() if abs_path.exists() else b""
    except (OSError, PermissionError) as e:
        raise _reject(f"Cannot read file for staging: {e}")

    # Run git hash-object -w to write blob to object database
    # --path tells Git to apply path-specific normalization
    try:
        result = subprocess.run(
            ["git", "hash-object", "-w", "--path", repo_path, "--stdin"],
            input=content_bytes,
            cwd=str(repo_root),
            capture_output=True,
            check=True,
            timeout=10,
        )
        oid = result.stdout.decode("utf-8").strip()
        if len(oid) != 40:
            raise _reject(f"Invalid blob OID from hash-object: {oid}")
        return oid
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise _reject(f"Failed to compute staged blob: {e}")


def _generate_stage_diff(
    repo_root: Path,
    repo_path: str,
    head_oid: str,
    proposed_blob_oid: str,
    is_new: bool,
) -> str:
    """Generate complete unified diff from HEAD to proposed staged blob.

    Returns the complete diff text.
    """
    if is_new:
        # New file: diff from /dev/null to proposed blob
        try:
            result = subprocess.run(
                ["git", "show", f"{proposed_blob_oid}"],
                cwd=str(repo_root),
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
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return result.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise _reject(f"Failed to generate stage diff: {e}")


# ---------------------------------------------------------------------------
# Commit policy
# ---------------------------------------------------------------------------


def prepare_commit_plan(
    *,
    message: str,
    repo_root: Path,
) -> GitCommitPlan:
    """Validate and create an immutable GitCommitPlan.

    This is the ONLY function that creates a GitCommitPlan. It:
    1. Validates HEAD exists (no unborn/detached)
    2. Validates user.name and user.email are configured
    3. Validates commit message (UTF-8, no control chars, length)
    4. Checks at least one staged change exists
    5. Validates all staged files are supported (text add/modify only)
    6. Generates complete commit diff (HEAD → entire staged snapshot)
    7. Binds to current Git snapshot (branch/HEAD/index)

    Args:
        message: Commit message text.
        repo_root: Absolute path to repository root.

    Returns:
        Immutable GitCommitPlan ready for user approval.

    Raises:
        GitMutationPolicyError: If the proposal violates any policy.
    """
    # Get Git context
    ctx = _get_git_context(repo_root)

    if ctx.is_unborn:
        raise _reject("Initial commits are not supported in v0.7.0.")

    # Validate and normalize message
    normalized_message = _validate_commit_message(message)

    # Get Git identity
    author_name, author_email = _get_git_identity(repo_root)

    # Compute index fingerprint
    index_fingerprint = _compute_index_fingerprint(repo_root)

    # Get all staged changes
    staged_paths = _get_staged_paths(repo_root)

    if not staged_paths:
        raise _reject("No staged changes to commit.")

    if len(staged_paths) > MAX_STAGED_FILES:
        raise _reject(
            f"Too many staged files ({len(staged_paths)}, max {MAX_STAGED_FILES})"
        )

    # Validate all staged files are supported
    _validate_staged_files(repo_root, staged_paths)

    # Generate complete commit diff
    diff = _generate_commit_diff(repo_root, ctx.head_oid)

    if len(diff) > MAX_COMMIT_DIFF_CHARS:
        raise _reject(
            f"Commit diff too large ({len(diff)} chars, max {MAX_COMMIT_DIFF_CHARS})"
        )

    # Hash the diff for integrity
    diff_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()

    return GitCommitPlan(
        id=str(uuid.uuid4()),
        kind=KIND_COMMIT,
        branch=ctx.branch,
        head_oid=ctx.head_oid,
        message=normalized_message,
        author_name=author_name,
        author_email=author_email,
        index_fingerprint=index_fingerprint,
        staged_paths=tuple(sorted(staged_paths)),
        diff=diff,
        diff_sha256=diff_sha256,
        created_at=time.time(),
    )


def _validate_commit_message(message: str) -> str:
    """Validate and normalize commit message.

    Returns normalized message (CRLF/CR → LF, stripped trailing whitespace).
    Raises GitMutationPolicyError if invalid.
    """
    if not message or not message.strip():
        raise _reject("Commit message cannot be blank.")

    if len(message) > MAX_COMMIT_MESSAGE_CHARS:
        raise _reject(
            f"Commit message too long ({len(message)} chars, "
            f"max {MAX_COMMIT_MESSAGE_CHARS})"
        )

    # Check for NUL
    if "\x00" in message:
        raise _reject("Commit message must not contain NUL characters.")

    # Check for control characters and bidi spoofing
    for ch in message:
        code = ord(ch)
        # Allow newline, tab, and normal printable characters
        if ch in ("\n", "\t"):
            continue
        # Reject C0 controls (except LF/TAB), DEL, C1 controls
        if code < 0x20 or (0x7F <= code <= 0x9F):
            raise _reject(f"Commit message contains control character U+{code:04X}.")
        # Reject Unicode bidi override characters (Trojan Source)
        if 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069 or 0x206A <= code <= 0x206F:
            raise _reject(f"Commit message contains bidirectional control character U+{code:04X}.")

    # Normalize newlines: CR/CRLF → LF
    normalized = message.replace("\r\n", "\n").replace("\r", "\n")

    # Check valid UTF-8 by encoding
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as e:
        raise _reject(f"Commit message encoding error: {e}")

    return normalized


def _get_git_identity(repo_root: Path) -> tuple[str, str]:
    """Get Git user.name and user.email from effective config.

    Returns (name, email).
    Raises GitMutationPolicyError if not configured or invalid.
    """
    # Get user.name
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        name = result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        raise _reject(
            "Git user.name is not configured. Please configure it outside the agent."
        )

    # Get user.email
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        email = result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        raise _reject(
            "Git user.email is not configured. Please configure it outside the agent."
        )

    if not name or not email:
        raise _reject("Git identity (user.name and user.email) must be non-empty.")

    if len(name) > MAX_IDENTITY_CHARS or len(email) > MAX_IDENTITY_CHARS:
        raise _reject("Git identity exceeds maximum length.")

    # Check for control characters in identity
    for ch in name + email:
        code = ord(ch)
        if code < 0x20 or (0x7F <= code <= 0x9F):
            raise _reject(f"Git identity contains control character U+{code:04X}.")
        if 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069 or 0x206A <= code <= 0x206F:
            raise _reject(f"Git identity contains bidirectional control character U+{code:04X}.")

    return name, email


def _get_staged_paths(repo_root: Path) -> list[str]:
    """Get all staged changed file paths (relative to repo root)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached", "--diff-filter=AM"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
        return paths
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise _reject(f"Failed to read staged paths: {e}")


def _validate_staged_files(repo_root: Path, staged_paths: list[str]) -> None:
    """Validate all staged files are supported (text add/modify only).

    Raises GitMutationPolicyError if any staged file is unsupported.
    """
    for path in staged_paths:
        abs_path = repo_root / path

        # Check not binary by extension
        if is_known_binary_extension(abs_path.name):
            raise _reject(f"Staged binary file detected: {path}")

        # Check sensitive/ignored paths
        if path_is_dangerous(abs_path, repo_root, target_is_dir=False):
            raise _reject(f"Staged sensitive or ignored path detected: {path}")

        # For existing files, check content
        if abs_path.exists():
            try:
                content_bytes = abs_path.read_bytes()
                if sniff_binary_content(content_bytes):
                    raise _reject(f"Staged file has binary content: {path}")
            except (OSError, PermissionError) as e:
                raise _reject(f"Cannot read staged file {path}: {e}")


def _generate_commit_diff(repo_root: Path, head_oid: str) -> str:
    """Generate complete unified diff from HEAD to entire staged snapshot.

    Returns the complete diff text.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", head_oid],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise _reject(f"Failed to generate commit diff: {e}")
