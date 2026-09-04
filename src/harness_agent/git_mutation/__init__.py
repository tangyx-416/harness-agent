"""User-Approved Git Stage & Commit subsystem (v0.7.0).

This package provides controlled Git index mutation and local commit creation,
following the same trust boundary as source editing:

    Agent proposes → Policy validates → User approves → Host mutates

Key components:
- :mod:`.models` -- Immutable GitStagePlan, GitCommitPlan, and results
- :mod:`.policy` -- Stage and commit validation, path safety, filter detection
- :mod:`.broker` -- Pending plans with exactly-once apply semantics
- :mod:`.service` -- Host-only Git mutation with canonical blob handling

No arbitrary Git command interface. No git add . No deletion/rename staging.
No amend. No tag. No push. No network.
"""

from .broker import GitMutationBroker, GitMutationBrokerError
from .models import (
    GitCommitPlan,
    GitMutationResult,
    GitStagePlan,
    IndexEntry,
    KIND_COMMIT,
    KIND_STAGE,
    STATUS_APPLIED,
    STATUS_APPROVED,
    STATUS_APPLYING,
    STATUS_CONFLICT,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from .policy import GitMutationPolicyError

__all__ = [
    "GitMutationBroker",
    "GitMutationBrokerError",
    "GitMutationPolicyError",
    "GitStagePlan",
    "GitCommitPlan",
    "GitMutationResult",
    "IndexEntry",
    "KIND_STAGE",
    "KIND_COMMIT",
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_APPLYING",
    "STATUS_REJECTED",
    "STATUS_APPLIED",
    "STATUS_CONFLICT",
    "STATUS_FAILED",
]
