"""Data models for remote Git fetch operations.

Immutable frozen dataclasses representing fetch plans and results.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class FetchState(str, Enum):
    """Fetch plan lifecycle states."""

    PENDING = "pending"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class GitFetchPlan:
    """Immutable remote fetch proposal.

    Represents a complete fetch operation: exact upstream remote branch into
    the local remote-tracking ref. Approved by the user after seeing the exact
    fetch scope (HTTPS URL, remote branch, tracking ref).

    v0.9 fetch does NOT modify:
    - working tree
    - Git index
    - HEAD
    - local branches
    - tags
    - remote repository

    v0.9 fetch MAY modify:
    - Git object database (add remote objects)
    - exactly one existing remote-tracking ref (fast-forward only)
    """

    plan_id: str
    kind: str  # Always "fetch"
    summary: str

    # Local state
    local_branch: str
    local_head_oid: str

    # Remote source
    remote_name: str
    remote_branch: str

    # Tracking ref that will be updated
    tracking_ref: str
    expected_tracking_oid: str

    # Approved remote URL (HTTPS only)
    approved_remote_url: str
    remote_host: str

    # Metadata
    created_at: datetime

    def __post_init__(self):
        """Validate immutability constraints."""
        if self.kind != "fetch":
            raise ValueError(f"GitFetchPlan kind must be 'fetch', got {self.kind!r}")
        if not self.tracking_ref.startswith("refs/remotes/"):
            raise ValueError(
                f"tracking_ref must be a remote-tracking ref, got {self.tracking_ref!r}"
            )


@dataclass(frozen=True)
class GitFetchResult:
    """Result of a fetch operation."""

    plan_id: str
    state: FetchState

    remote_name: str
    remote_branch: str
    tracking_ref: str

    # Tracking ref state
    previous_tracking_oid: str
    observed_remote_oid: Optional[str]
    updated_tracking_oid: Optional[str]

    # Whether tracking ref was updated
    changed: bool

    message: str
    created_at: datetime
