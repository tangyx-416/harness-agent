"""Immutable models for the User-Approved Source Editing subsystem (v0.6.0).

All models are ``frozen`` dataclasses so the exact text a user approves is
byte-for-byte what the host applies:

* :class:`PatchReplacement` -- on exact-text replacement for an ``edit`` plan.
* :class:`PatchPlan`        -- one validated, immutable, single-file proposal.
  It carries the COMPLETE unified diff (never a truncated preview) and the
  proposed content the host will write. It is created only by the policy and
  never mutated afterwards.
* :class:`PatchResult`      -- the structured outcome of one host-side apply
  attempt (``applied`` / ``conflict`` / ``failed``).

Replacement/match text, the complete diff and the proposed content are
deliberately held ONLY by the broker (in process memory), never copied into
:class:`~harness_agent.session.SessionState`, which keeps metadata only.
"""

from __future__ import annotations

from dataclasses import dataclass

OP_EDIT = "edit"
OP_CREATE = "create"

#: Applicable patch plan lifecycle statuses (see :mod:`.broker`).
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_APPLYING = "applying"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_CONFLICT = "conflict"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class PatchReplacement:
    """One exact, single-occurrence text replacement within the target file.

    ``old_text`` must match the target content exactly once (uniquely) so the
    replacement is deterministic and independent of the order in which the
    model listed the replacements.
    """

    old_text: str
    new_text: str


@dataclass(frozen=True)
class PatchPlan:
    """An immutable, validated, single-file source editing proposal.

    Attributes:
        id: Unique plan identifier (the handle for approve/apply/reject).
        operation: ``"edit"`` (replace text in an existing file) or
            ``"create"`` (write a brand new file).
        repo_path: Repository-relative POSIX path of the single target file.
        abs_path: Absolute path the host will write, confined to the repo root.
        summaries: Concise, user-visible change summaries (no secrets).
        replacements: Ordered, non-overlapping, unique-match replacements
            (empty for ``create``).
        proposed_content: The complete proposed file content (UTF-8 text).
        has_bom: Whether the original (edit) file, or intended (create) file,
            has a UTF-8 byte-order mark.
        original_sha256: SHA-256 of the target's current bytes at prepare time
            (empty for a brand-new ``create`` plan).
        proposed_sha256: SHA-256 of the bytes that will be written on apply.
        diff: The COMPLETE unified diff of the change (never truncated).
        created_at: Creation timestamp (``time.time()``).
    """

    id: str
    operation: str
    repo_path: str
    abs_path: str
    summaries: tuple[str, ...]
    replacements: tuple[PatchReplacement, ...]
    proposed_content: str
    has_bom: bool
    original_sha256: str
    proposed_sha256: str
    diff: str
    created_at: float


@dataclass(frozen=True)
class PatchResult:
    """Structured outcome of a host-side apply attempt."""

    plan_id: str
    repo_path: str
    operation: str
    status: str
    sha256: str | None
    message: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly dict (used by tools and the CLI)."""
        return {
            "plan_id": self.plan_id,
            "repo_path": self.repo_path,
            "operation": self.operation,
            "status": self.status,
            "sha256": self.sha256,
            "message": self.message,
        }
