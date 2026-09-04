"""Pending Git mutation broker (v0.7.0).

Holds :class:`GitStagePlan` and :class:`GitCommitPlan` objects in process memory
only (no database) and enforces the Git mutation lifecycle:

    pending -> approved -> applying -> applied | conflict | failed   (single use)
    pending -> rejected                                              (user said no)
    unknown id -> error

The broker is the boundary between the agent world and the host world: tools
may only *register* pending plans; approve/reject/apply transitions are
reserved for the trusted host layer (CLI).

The broker temporarily retains an approved proposal (including its complete diff)
in process memory for the host to apply. It never writes it into
:class:`~harness_agent.session.SessionState`, which keeps metadata only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Union

from .models import (
    STATUS_APPLIED,
    STATUS_APPLYING,
    STATUS_APPROVED,
    STATUS_CONFLICT,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    GitCommitPlan,
    GitMutationResult,
    GitStagePlan,
    KIND_COMMIT,
    KIND_STAGE,
)

#: Terminal Git mutation states that can never be approved or applied again.
TERMINAL_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_CONFLICT, STATUS_FAILED, STATUS_REJECTED}
)

#: Maximum number of plans one broker retains (bounded memory, per agent).
MAX_GIT_MUTATION_PLANS = 200


class GitMutationBrokerError(Exception):
    """Raised for unknown plan ids or invalid lifecycle transitions."""


@dataclass
class _GitMutationEntry:
    plan: Union[GitStagePlan, GitCommitPlan]
    status: str = STATUS_PENDING
    result: Optional[GitMutationResult] = None


class GitMutationBroker:
    """In-memory registry of Git mutation proposals with single-use semantics.

    Each agent factory call owns a private GitMutationBroker; two agents in one
    process share no Git mutation plans, results, or pending queues - zero
    cross-session visibility.
    """

    def __init__(self, max_plans: int = MAX_GIT_MUTATION_PLANS) -> None:
        self._entries: dict[str, _GitMutationEntry] = {}
        self._lock = threading.Lock()
        self._max_plans = max_plans

    # -- registration (agent-visible side) --------------------------------

    def register(self, plan: Union[GitStagePlan, GitCommitPlan]) -> None:
        """Store a newly prepared, immutable plan in ``pending`` state."""
        with self._lock:
            if len(self._entries) >= self._max_plans:
                raise GitMutationBrokerError(
                    f"Git mutation plan limit reached ({self._max_plans})."
                )
            self._entries[plan.id] = _GitMutationEntry(plan=plan)

    # -- queries -----------------------------------------------------------

    def get_plan(self, plan_id: str) -> Optional[Union[GitStagePlan, GitCommitPlan]]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.plan if entry else None

    def status(self, plan_id: str) -> Optional[str]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.status if entry else None

    def get_result(self, plan_id: str) -> Optional[GitMutationResult]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.result if entry else None

    def pending(self) -> list[Union[GitStagePlan, GitCommitPlan]]:
        """All plans awaiting user approval, in creation order."""
        with self._lock:
            return [
                entry.plan
                for entry in self._entries.values()
                if entry.status == STATUS_PENDING
            ]

    def pending_stage_plans(self) -> list[GitStagePlan]:
        """All stage plans awaiting user approval."""
        with self._lock:
            return [
                entry.plan
                for entry in self._entries.values()
                if entry.status == STATUS_PENDING and entry.plan.kind == KIND_STAGE
            ]

    def pending_commit_plans(self) -> list[GitCommitPlan]:
        """All commit plans awaiting user approval."""
        with self._lock:
            return [
                entry.plan
                for entry in self._entries.values()
                if entry.status == STATUS_PENDING and entry.plan.kind == KIND_COMMIT
            ]

    # -- lifecycle transitions (host-only side) ---------------------------

    def _require(self, plan_id: str) -> _GitMutationEntry:
        entry = self._entries.get(plan_id)
        if entry is None:
            raise GitMutationBrokerError(f"Unknown Git mutation plan id: {plan_id}")
        return entry

    def approve(self, plan_id: str) -> Union[GitStagePlan, GitCommitPlan]:
        """User approved the plan; it may now be applied exactly once."""
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_PENDING:
                raise GitMutationBrokerError(
                    f"Git mutation plan {plan_id} cannot be approved from status "
                    f"'{entry.status}'."
                )
            entry.status = STATUS_APPROVED
            return entry.plan

    def reject(self, plan_id: str) -> Union[GitStagePlan, GitCommitPlan]:
        """User declined the plan; it can never be applied.

        Only ``pending -> rejected`` is legal: a rejected plan can never be
        approved.
        """
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_PENDING:
                raise GitMutationBrokerError(
                    f"Git mutation plan {plan_id} cannot be rejected from status "
                    f"'{entry.status}'; only pending plans can be rejected."
                )
            entry.status = STATUS_REJECTED
            return entry.plan

    def take_for_apply(self, plan_id: str) -> Union[GitStagePlan, GitCommitPlan]:
        """Claim an approved plan for a single apply attempt (exactly-once).

        Under a single lock this verifies the plan is ``approved`` and atomically
        moves it to ``applying``. A second caller (even a concurrent one) is
        rejected, so one approved GitStagePlan or GitCommitPlan can never be
        applied twice.

        Raises:
            GitMutationBrokerError: Unknown id, plan not approved, or already used.
        """
        with self._lock:
            entry = self._require(plan_id)
            if entry.status == STATUS_PENDING:
                raise GitMutationBrokerError(
                    f"Git mutation plan {plan_id} has not been approved by the user yet."
                )
            if entry.status != STATUS_APPROVED:
                raise GitMutationBrokerError(
                    f"Git mutation plan {plan_id} was already claimed (status "
                    f"'{entry.status}'); proposals are single use."
                )
            entry.status = STATUS_APPLYING
            return entry.plan

    def record_application(
        self,
        plan_id: str,
        *,
        status: str,
        path: Optional[str],
        commit_oid: Optional[str],
        message: str,
    ) -> GitMutationResult:
        """Attach the outcome of an apply attempt to an approved plan.

        *status* is one of ``applied``, ``conflict`` or ``failed``. The plan
        must already be in the ``applying`` (claimed) state from a preceding
        :meth:`take_for_apply`; it is then finalised to the given terminal
        state. Because only one caller can ever reach ``applying``, an apply
        outcome (and any corresponding host session event) is produced at most
        once per plan.
        """
        if status not in (STATUS_APPLIED, STATUS_CONFLICT, STATUS_FAILED):
            raise GitMutationBrokerError(
                f"Invalid application outcome status: {status!r}."
            )
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_APPLYING:
                raise GitMutationBrokerError(
                    f"Git mutation plan {plan_id} is not awaiting a result (status "
                    f"'{entry.status}')."
                )
            result = GitMutationResult(
                plan_id=plan_id,
                kind=entry.plan.kind,
                status=status,
                path=path,
                commit_oid=commit_oid,
                message=message,
            )
            entry.status = status
            entry.result = result
            return result

    def record_result(self, result: GitMutationResult) -> GitMutationResult:
        """Convenience method to record a GitMutationResult object.

        This is a wrapper around record_application that takes a GitMutationResult
        directly instead of individual parameters.
        """
        return self.record_application(
            plan_id=result.plan_id,
            status=result.status,
            path=result.path,
            commit_oid=result.commit_oid,
            message=result.message,
        )
