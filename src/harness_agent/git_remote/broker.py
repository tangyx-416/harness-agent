"""Broker for remote Git push operations.

Manages push plan lifecycle and ensures exactly-once application.
"""

import threading
from datetime import datetime
from typing import Optional

from .models import GitPushPlan, GitPushResult, PushState


class GitRemoteBroker:
    """Broker for remote Git push plans.

    Manages the lifecycle of push proposals from pending through approval
    to application. Ensures exactly-once execution through atomic state
    transitions.
    """

    def __init__(self):
        """Initialize an empty broker."""
        self._lock = threading.RLock()
        self._plans: dict[str, GitPushPlan] = {}
        self._states: dict[str, PushState] = {}
        self._results: dict[str, GitPushResult] = {}

    def register(self, plan: GitPushPlan) -> None:
        """Register a new push plan in pending state.

        Args:
            plan: The push plan to register.

        Raises:
            ValueError: If a plan with this ID already exists.
        """
        with self._lock:
            if plan.plan_id in self._plans:
                raise ValueError(f"Plan {plan.plan_id} already exists")
            self._plans[plan.plan_id] = plan
            self._states[plan.plan_id] = PushState.PENDING

    def get_plan(self, plan_id: str) -> Optional[GitPushPlan]:
        """Retrieve a plan by ID.

        Args:
            plan_id: The plan identifier.

        Returns:
            The plan if found, None otherwise.
        """
        with self._lock:
            return self._plans.get(plan_id)

    def get_state(self, plan_id: str) -> Optional[PushState]:
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
            if self._states[plan_id] != PushState.PENDING:
                raise ValueError(
                    f"Plan {plan_id} is not pending (state: {self._states[plan_id]})"
                )
            self._states[plan_id] = PushState.APPROVED

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
            if self._states[plan_id] != PushState.PENDING:
                raise ValueError(
                    f"Plan {plan_id} is not pending (state: {self._states[plan_id]})"
                )
            self._states[plan_id] = PushState.REJECTED

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
            if self._states[plan_id] != PushState.APPROVED:
                return False
            self._states[plan_id] = PushState.APPLYING
            return True

    def record_result(self, result: GitPushResult) -> None:
        """Record the result of a push operation.

        Args:
            result: The push result to record.

        Raises:
            ValueError: If the plan is not in applying state.
        """
        with self._lock:
            plan_id = result.plan_id
            if plan_id not in self._states:
                raise ValueError(f"Unknown plan {plan_id}")
            if self._states[plan_id] != PushState.APPLYING:
                raise ValueError(
                    f"Git push plan {plan_id} is not awaiting a result "
                    f"(status {self._states[plan_id].value!r})"
                )
            self._states[plan_id] = result.state
            self._results[plan_id] = result

    def get_result(self, plan_id: str) -> Optional[GitPushResult]:
        """Retrieve the result of a push operation.

        Args:
            plan_id: The plan identifier.

        Returns:
            The result if available, None otherwise.
        """
        with self._lock:
            return self._results.get(plan_id)

    def pending_plans(self) -> list[GitPushPlan]:
        """Get all pending push plans.

        Returns:
            List of plans in pending state.
        """
        with self._lock:
            return [
                plan
                for plan_id, plan in self._plans.items()
                if self._states[plan_id] == PushState.PENDING
            ]
