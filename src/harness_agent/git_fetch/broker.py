"""Broker for remote Git fetch operations.

Manages fetch plan lifecycle and ensures exactly-once application.
"""

import threading
from typing import Optional

from .models import FetchState, GitFetchPlan, GitFetchResult


class GitFetchBroker:
    """Broker for remote Git fetch plans.

    Manages the lifecycle of fetch proposals from pending through approval
    to application. Ensures exactly-once execution through atomic state
    transitions.
    """

    def __init__(self):
        """Initialize an empty broker."""
        self._lock = threading.RLock()
        self._plans: dict[str, GitFetchPlan] = {}
        self._states: dict[str, FetchState] = {}
        self._results: dict[str, GitFetchResult] = {}

    def register(self, plan: GitFetchPlan) -> None:
        """Register a new fetch plan in pending state.

        Args:
            plan: The fetch plan to register.

        Raises:
            ValueError: If a plan with this ID already exists.
        """
        with self._lock:
            if plan.plan_id in self._plans:
                raise ValueError(f"Plan {plan.plan_id} already exists")
            self._plans[plan.plan_id] = plan
            self._states[plan.plan_id] = FetchState.PENDING

    def get_plan(self, plan_id: str) -> Optional[GitFetchPlan]:
        """Retrieve a plan by ID.

        Args:
            plan_id: The plan identifier.

        Returns:
            The plan if found, None otherwise.
        """
        with self._lock:
            return self._plans.get(plan_id)

    def get_state(self, plan_id: str) -> Optional[FetchState]:
        """Get the current state of a plan.

        Args:
            plan_id: The plan identifier.

        Returns:
            The current state if the plan exists, None otherwise.
        """
        with self._lock:
            return self._states.get(plan_id)

    def approve(self, plan_id: str) -> None:
        """Approve a pending plan.

        Args:
            plan_id: The plan identifier.

        Raises:
            ValueError: If the plan doesn't exist or is not pending.
        """
        with self._lock:
            if plan_id not in self._states:
                raise ValueError(f"Unknown plan {plan_id}")
            if self._states[plan_id] != FetchState.PENDING:
                raise ValueError(
                    f"Plan {plan_id} is not pending (state: {self._states[plan_id]})"
                )
            self._states[plan_id] = FetchState.APPROVED

    def reject(self, plan_id: str) -> None:
        """Reject a pending plan.

        Args:
            plan_id: The plan identifier.

        Raises:
            ValueError: If the plan doesn't exist or is not pending.
        """
        with self._lock:
            if plan_id not in self._states:
                raise ValueError(f"Unknown plan {plan_id}")
            if self._states[plan_id] != FetchState.PENDING:
                raise ValueError(
                    f"Plan {plan_id} is not pending (state: {self._states[plan_id]})"
                )
            self._states[plan_id] = FetchState.REJECTED

    def take_for_apply(self, plan_id: str) -> bool:
        """Atomically claim a plan for application.

        Only one caller can successfully transition an approved plan to
        applying state. This ensures exactly-once execution.

        Args:
            plan_id: The plan identifier.

        Returns:
            True if the plan was successfully claimed, False otherwise.
        """
        with self._lock:
            if plan_id not in self._states:
                return False
            if self._states[plan_id] != FetchState.APPROVED:
                return False
            self._states[plan_id] = FetchState.APPLYING
            return True

    def record_result(self, result: GitFetchResult) -> None:
        """Record the result of a fetch operation.

        Args:
            result: The fetch result to record.

        Raises:
            ValueError: If the plan is not in applying state.
        """
        with self._lock:
            plan_id = result.plan_id
            if plan_id not in self._states:
                raise ValueError(f"Unknown plan {plan_id}")
            if self._states[plan_id] != FetchState.APPLYING:
                raise ValueError(
                    f"Git fetch plan {plan_id} is not awaiting a result "
                    f"(status {self._states[plan_id].value!r})"
                )
            self._states[plan_id] = result.state
            self._results[plan_id] = result

    def get_result(self, plan_id: str) -> Optional[GitFetchResult]:
        """Retrieve the result of a fetch operation.

        Args:
            plan_id: The plan identifier.

        Returns:
            The result if available, None otherwise.
        """
        with self._lock:
            return self._results.get(plan_id)

    def pending_plans(self) -> list[GitFetchPlan]:
        """Get all pending fetch plans.

        Returns:
            List of plans in pending state.
        """
        with self._lock:
            return [
                plan
                for plan_id, plan in self._plans.items()
                if self._states[plan_id] == FetchState.PENDING
            ]
