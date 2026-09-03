"""Pending-patch broker (v0.6.0).

Holds :class:`PatchPlan` objects in process memory only (no database) and
enforces the patch lifecycle:

    pending -> approved -> applied | conflict | failed   (single use)
    pending -> rejected                                  (user said no)
    unknown id -> error

The broker is the boundary between the agent world and the host world: tools
may only *register* pending plans; approve/reject/apply transitions are
reserved for the trusted host layer (CLI).

The broker temporarily retains an approved proposal (including its diff and
proposed source content) in process memory for the host to apply. It never
writes it into :class:`~harness_agent.session.SessionState`, which keeps
metadata only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .models import (
    STATUS_APPLIED,
    STATUS_APPLYING,
    STATUS_APPROVED,
    STATUS_CONFLICT,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    PatchPlan,
    PatchResult,
)

#: Terminal patch states that can never be approved or applied again.
TERMINAL_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_CONFLICT, STATUS_FAILED, STATUS_REJECTED}
)

#: Maximum number of plans one broker retains (bounded memory, per agent).
MAX_PATCH_PLANS = 200


class PatchBrokerError(Exception):
    """Raised for unknown plan ids or invalid lifecycle transitions."""


@dataclass
class _PatchEntry:
    plan: PatchPlan
    status: str = STATUS_PENDING
    result: Optional[PatchResult] = None


class PatchBroker:
    """In-memory registry of patch proposals with single-use semantics."""

    def __init__(self, max_plans: int = MAX_PATCH_PLANS) -> None:
        self._entries: dict[str, _PatchEntry] = {}
        self._lock = threading.Lock()
        self._max_plans = max_plans

    # -- registration (agent-visible side) --------------------------------

    def register(self, plan: PatchPlan) -> None:
        """Store a newly prepared, immutable plan in ``pending`` state."""
        with self._lock:
            if len(self._entries) >= self._max_plans:
                raise PatchBrokerError(
                    f"Patch plan limit reached ({self._max_plans})."
                )
            self._entries[plan.id] = _PatchEntry(plan=plan)

    # -- queries -----------------------------------------------------------

    def get_plan(self, plan_id: str) -> Optional[PatchPlan]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.plan if entry else None

    def status(self, plan_id: str) -> Optional[str]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.status if entry else None

    def get_result(self, plan_id: str) -> Optional[PatchResult]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.result if entry else None

    def pending(self) -> list[PatchPlan]:
        """All plans awaiting user approval, in creation order."""
        with self._lock:
            return [
                entry.plan
                for entry in self._entries.values()
                if entry.status == STATUS_PENDING
            ]

    # -- lifecycle transitions (host-only side) ---------------------------

    def _require(self, plan_id: str) -> _PatchEntry:
        entry = self._entries.get(plan_id)
        if entry is None:
            raise PatchBrokerError(f"Unknown patch plan id: {plan_id}")
        return entry

    def approve(self, plan_id: str) -> PatchPlan:
        """User approved the plan; it may now be applied exactly once."""
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_PENDING:
                raise PatchBrokerError(
                    f"Patch plan {plan_id} cannot be approved from status "
                    f"'{entry.status}'."
                )
            entry.status = STATUS_APPROVED
            return entry.plan

    def reject(self, plan_id: str) -> PatchPlan:
        """User declined the plan; it can never be applied.

        Only ``pending -> rejected`` is legal: a rejected plan can never be
        approved.
        """
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_PENDING:
                raise PatchBrokerError(
                    f"Patch plan {plan_id} cannot be rejected from status "
                    f"'{entry.status}'; only pending plans can be rejected."
                )
            entry.status = STATUS_REJECTED
            return entry.plan

    def take_for_apply(self, plan_id: str) -> PatchPlan:
        """Claim an approved plan for a single apply attempt (exactly-once).

        Under a single lock this verifies the plan is ``approved`` and atomically
        moves it to ``applying``. A second caller (even a concurrent one) is
        rejected, so one approved PatchPlan can never be applied twice.

        Raises:
            PatchBrokerError: Unknown id, plan not approved, or already used.
        """
        with self._lock:
            entry = self._require(plan_id)
            if entry.status == STATUS_PENDING:
                raise PatchBrokerError(
                    f"Patch plan {plan_id} has not been approved by the user yet."
                )
            if entry.status != STATUS_APPROVED:
                raise PatchBrokerError(
                    f"Patch plan {plan_id} was already claimed (status "
                    f"'{entry.status}'); proposals are single use."
                )
            entry.status = STATUS_APPLYING
            return entry.plan

    def record_application(
        self,
        plan_id: str,
        *,
        status: str,
        sha256: Optional[str],
        message: str,
    ) -> PatchResult:
        """Attach the outcome of an apply attempt to an approved plan.

        *status* is one of ``applied``, ``conflict`` or ``failed``. The plan
        must already be in the ``applying`` (claimed) state from a preceding
        :meth:`take_for_apply`; it is then finalised to the given terminal
        state. Because only one caller can ever reach ``applying``, an apply
        outcome (and any corresponding host session event) is produced at most
        once per plan.
        """
        if status not in (STATUS_APPLIED, STATUS_CONFLICT, STATUS_FAILED):
            raise PatchBrokerError(f"Invalid application outcome status: {status!r}.")
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_APPLYING:
                raise PatchBrokerError(
                    f"Patch plan {plan_id} is not awaiting a result (status "
                    f"'{entry.status}')."
                )
            result = PatchResult(
                plan_id=plan_id,
                repo_path=entry.plan.repo_path,
                operation=entry.plan.operation,
                status=status,
                sha256=sha256,
                message=message,
            )
            entry.status = status
            entry.result = result
            return result
