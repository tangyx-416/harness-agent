"""Immutable models for User-Approved Git Stage & Commit (v0.7.0).

All models are ``frozen`` dataclasses so what the user approves in the CLI
preview is byte-for-byte what the host stages or commits.

* :class:`IndexEntry`       -- One immutable Git index entry (path/mode/oid/stage).
* :class:`GitStagePlan`     -- One validated, immutable, single-file stage proposal.
* :class:`GitCommitPlan`    -- One validated, immutable commit of the entire staged snapshot.
* :class:`GitMutationResult` -- Structured outcome of a host-side stage or commit.

The complete staged diff and commit diff are held ONLY by the broker (in process
memory), never copied into SessionState, which keeps metadata only.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Git mutation plan kinds
KIND_STAGE = "stage"
KIND_COMMIT = "commit"

#: Git mutation lifecycle statuses (see :mod:`.broker`)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_APPLYING = "applying"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_CONFLICT = "conflict"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class IndexEntry:
    """One immutable Git index entry snapshot.

    Attributes:
        path: Repository-relative path (UTF-8, forward slashes).
        mode: Git mode string (e.g. "100644", "100755").
        object_id: Full 40-character hex SHA-1 object ID.
        stage: Merge stage (0 for normal, 1-3 for conflicts).
    """

    path: str
    mode: str
    object_id: str
    stage: int


@dataclass(frozen=True)
class GitStagePlan:
    """An immutable, validated, single-file Git stage proposal.

    One StagePlan = One Repository File. To stage multiple files, create
    multiple StagePlans and approve each independently.

    Attributes:
        id: Unique plan identifier (the handle for approve/apply/reject).
        kind: Always ``"stage"``.
        repo_path: Repository-relative POSIX path of the single target file.
        summary: Concise user-visible change summary.
        branch: Branch name at prepare time.
        head_oid: HEAD commit OID at prepare time.
        base_worktree_sha256: SHA-256 of raw working-tree file content.
        base_index_fingerprint: SHA-256 of canonical index state (all entries).
        previous_index_entry: Previous index entry for this path (None if new file).
        proposed_mode: Proposed Git index mode (e.g. "100644").
        proposed_blob_oid: Proposed Git blob object ID (canonical staged bytes).
        diff: COMPLETE unified diff from HEAD to proposed staged state.
        diff_sha256: SHA-256 of the complete diff for integrity verification.
        created_at: Creation timestamp (``time.time()``).
    """

    id: str
    kind: str  # Always KIND_STAGE
    repo_path: str
    summary: str
    branch: str
    head_oid: str
    base_worktree_sha256: str
    base_index_fingerprint: str
    previous_index_entry: IndexEntry | None
    proposed_mode: str
    proposed_blob_oid: str
    diff: str
    diff_sha256: str
    created_at: float


@dataclass(frozen=True)
class GitCommitPlan:
    """An immutable, validated local commit proposal of the entire staged snapshot.

    One CommitPlan = Entire Current Index Staged Snapshot. The agent cannot
    choose which staged files to commit; the commit includes everything currently
    staged, and the user sees the complete staged diff before approval.

    Attributes:
        id: Unique plan identifier (the handle for approve/apply/reject).
        kind: Always ``"commit"``.
        branch: Branch name at prepare time.
        head_oid: HEAD commit OID (parent) at prepare time.
        message: Commit message text (normalized, UTF-8).
        author_name: Git user.name from effective config.
        author_email: Git user.email from effective config.
        index_fingerprint: SHA-256 of canonical index state (all staged entries).
        staged_paths: Tuple of all staged changed paths (sorted, immutable).
        diff: COMPLETE unified diff from HEAD to entire staged snapshot.
        diff_sha256: SHA-256 of the complete diff for integrity verification.
        created_at: Creation timestamp (``time.time()``).
    """

    id: str
    kind: str  # Always KIND_COMMIT
    branch: str
    head_oid: str
    message: str
    author_name: str
    author_email: str
    index_fingerprint: str
    staged_paths: tuple[str, ...]
    diff: str
    diff_sha256: str
    created_at: float


@dataclass(frozen=True)
class GitMutationResult:
    """Structured outcome of a host-side Git mutation (stage or commit).

    Attributes:
        plan_id: Plan ID that was applied.
        kind: ``"stage"`` or ``"commit"``.
        status: ``"applied"`` / ``"conflict"`` / ``"failed"``.
        path: Repository path for stage operations (None for commit).
        commit_oid: New commit OID for successful commit (None for stage).
        message: Human-readable outcome message.
    """

    plan_id: str
    kind: str
    status: str
    path: str | None
    commit_oid: str | None
    message: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly dict (used by tools and CLI)."""
        return {
            "plan_id": self.plan_id,
            "kind": self.kind,
            "status": self.status,
            "path": self.path,
            "commit_oid": self.commit_oid,
            "message": self.message,
        }