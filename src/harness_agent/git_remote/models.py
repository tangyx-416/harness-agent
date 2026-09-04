"""Data models for remote Git push operations.

Immutable frozen dataclasses representing push plans and results.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class PushState(str, Enum):
    """Push plan lifecycle states."""

    PENDING = "pending"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class PushCommit:
    """A single commit in the outgoing push range."""

    oid: str
    short_oid: str
    subject: str


@dataclass(frozen=True)
class GitPushPlan:
    """Immutable remote push proposal.

    Represents a complete push operation: exact local HEAD to exact remote
    branch destination. Approved by the user after seeing the complete outgoing
    commit list and full diff.
    """

    plan_id: str
    kind: str  # Always "push"
    summary: str

    # Local state
    local_branch: str
    head_oid: str
    head_tree_oid: str

    # Remote destination
    remote_name: str
    remote_branch: str
    approved_remote_url: str
    remote_host: str

    # Expected remote state (from local tracking ref)
    expected_remote_oid: str
    expected_tree_oid: str

    # Outgoing commits
    outgoing_commits: tuple[PushCommit, ...]
    commit_count: int

    # Complete diff preview
    diff: str
    diff_sha256: str

    # Metadata
    created_at: datetime

    def __post_init__(self):
        """Validate immutability constraints."""
        if self.kind != "push":
            raise ValueError(f"GitPushPlan kind must be 'push', got {self.kind!r}")
        if not isinstance(self.outgoing_commits, tuple):
            raise ValueError("outgoing_commits must be a tuple")
        if len(self.outgoing_commits) != self.commit_count:
            raise ValueError(
                f"commit_count {self.commit_count} does not match "
                f"outgoing_commits length {len(self.outgoing_commits)}"
            )


@dataclass(frozen=True)
class GitPushResult:
    """Result of a push operation."""

    plan_id: str
    state: PushState

    remote_name: str
    remote_branch: str

    head_oid: str
    remote_oid_after: Optional[str]

    message: str
    created_at: datetime
