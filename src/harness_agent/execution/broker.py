"""Pending-execution broker (v0.3.0).

Holds :class:`ExecutionPlan` objects in process memory only (no database)
and enforces the plan lifecycle:

    pending -> approved -> executed      (single use)
    pending -> rejected                  (user said no)
    unknown id -> error

The broker is the boundary between the agent world and the host world:
tools may only *register* plans; approve/reject/execute transitions are
reserved for the trusted host layer (CLI).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .models import ExecutionPlan, ExecutionResult

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_EXECUTED = "executed"
STATUS_REJECTED = "rejected"


class BrokerError(Exception):
    """Raised for unknown plan ids or invalid lifecycle transitions."""


@dataclass
class _PlanEntry:
    plan: ExecutionPlan
    status: str = STATUS_PENDING
    result: Optional[ExecutionResult] = None


class ExecutionBroker:
    """In-memory registry of execution plans with single-use semantics."""

    def __init__(self) -> None:
        self._entries: dict[str, _PlanEntry] = {}
        self._lock = threading.Lock()

    # -- registration (agent-visible side) --------------------------------

    def register(self, plan: ExecutionPlan) -> None:
        """Store a newly prepared plan in ``pending`` state."""
        with self._lock:
            self._entries[plan.id] = _PlanEntry(plan=plan)

    # -- queries -----------------------------------------------------------

    def get_plan(self, plan_id: str) -> Optional[ExecutionPlan]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.plan if entry else None

    def status(self, plan_id: str) -> Optional[str]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.status if entry else None

    def get_result(self, plan_id: str) -> Optional[ExecutionResult]:
        with self._lock:
            entry = self._entries.get(plan_id)
            return entry.result if entry else None

    def pending(self) -> list[ExecutionPlan]:
        """All plans awaiting user approval, in creation order."""
        with self._lock:
            return [
                entry.plan
                for entry in self._entries.values()
                if entry.status == STATUS_PENDING
            ]

    # -- lifecycle transitions (host-only side) ---------------------------

    def _require(self, plan_id: str) -> _PlanEntry:
        entry = self._entries.get(plan_id)
        if entry is None:
            raise BrokerError(f"Unknown plan id: {plan_id}")
        return entry

    def approve(self, plan_id: str) -> ExecutionPlan:
        """User approved the plan; it may now be executed exactly once."""
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_PENDING:
                raise BrokerError(
                    f"Plan {plan_id} cannot be approved from status "
                    f"'{entry.status}'."
                )
            entry.status = STATUS_APPROVED
            return entry.plan

    def reject(self, plan_id: str) -> ExecutionPlan:
        """User declined the plan; it can never be executed.

        Only ``pending -> rejected`` is legal: approval happens strictly
        before execution and a rejected plan can never be approved.
        """
        with self._lock:
            entry = self._require(plan_id)
            if entry.status != STATUS_PENDING:
                raise BrokerError(
                    f"Plan {plan_id} cannot be rejected from status "
                    f"'{entry.status}'; only pending plans can be rejected."
                )
            entry.status = STATUS_REJECTED
            return entry.plan

    def take_for_execution(self, plan_id: str) -> ExecutionPlan:
        """Claim an approved plan for execution (marks it single-use).

        Raises:
            BrokerError: Unknown id, plan not approved, or already used.
        """
        with self._lock:
            entry = self._require(plan_id)
            if entry.status == STATUS_PENDING:
                raise BrokerError(
                    f"Plan {plan_id} has not been approved by the user yet."
                )
            if entry.status != STATUS_APPROVED:
                raise BrokerError(
                    f"Plan {plan_id} was already used (status "
                    f"'{entry.status}'); plans are single-use."
                )
            entry.status = STATUS_EXECUTED
            return entry.plan

    def record_result(self, result: ExecutionResult) -> None:
        """Attach an :class:`ExecutionResult` to its (executed) plan."""
        with self._lock:
            entry = self._require(result.plan_id)
            entry.result = result
