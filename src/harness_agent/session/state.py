"""Thread-safe, bounded, in-memory task state for one CLI session.

This module deliberately has no persistence or execution capability. It stores
concise, user-visible metadata only; it is not a transcript, tool-result cache,
authorization system, or place for private chain-of-thought.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import SessionEvent, TaskPlan, TaskStep

MAX_TASKS = 20
MAX_STEPS_PER_TASK = 20
MAX_EVENTS = 200
MAX_RECENT_EVENTS = 20
MAX_GOAL_CHARS = 500
MAX_STEP_CHARS = 300
MAX_NOTE_CHARS = 1000
MAX_EVENT_SUMMARY_CHARS = 500
MAX_REFERENCE_CHARS = 200

STEP_PENDING = "pending"
STEP_IN_PROGRESS = "in_progress"
STEP_COMPLETED = "completed"
STEP_BLOCKED = "blocked"
STEP_SKIPPED = "skipped"

VALID_STEP_STATUSES = frozenset(
    {
        STEP_PENDING,
        STEP_IN_PROGRESS,
        STEP_COMPLETED,
        STEP_BLOCKED,
        STEP_SKIPPED,
    }
)

_ALLOWED_TRANSITIONS = {
    STEP_PENDING: frozenset(
        {STEP_IN_PROGRESS, STEP_COMPLETED, STEP_BLOCKED, STEP_SKIPPED}
    ),
    STEP_IN_PROGRESS: frozenset(
        {STEP_PENDING, STEP_COMPLETED, STEP_BLOCKED, STEP_SKIPPED}
    ),
    STEP_BLOCKED: frozenset({STEP_PENDING, STEP_IN_PROGRESS, STEP_SKIPPED}),
    STEP_COMPLETED: frozenset(),
    STEP_SKIPPED: frozenset(),
}

TASK_ACTIVE = "active"
TASK_COMPLETED = "completed"
TASK_BLOCKED = "blocked"


class SessionStateError(ValueError):
    """Raised when a requested state mutation is invalid."""


def _utc_now() -> str:
    """Return a timezone-aware UTC timestamp in a JSON-friendly form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_text(
    value: str,
    *,
    field_name: str,
    max_chars: int,
    allow_blank: bool = False,
) -> str:
    """Validate bounded Unicode text without changing ordinary content."""
    if not isinstance(value, str):
        raise SessionStateError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise SessionStateError(f"{field_name} must not contain NUL characters.")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized and not allow_blank:
        raise SessionStateError(f"{field_name} must not be blank.")
    if len(normalized) > max_chars:
        raise SessionStateError(
            f"{field_name} exceeds the {max_chars}-character limit."
        )
    return normalized


def _derive_task_status(steps: tuple[TaskStep, ...]) -> str:
    """Derive plan status exclusively from the current step statuses."""
    if all(step.status in {STEP_COMPLETED, STEP_SKIPPED} for step in steps):
        return TASK_COMPLETED

    unfinished = [
        step for step in steps if step.status not in {STEP_COMPLETED, STEP_SKIPPED}
    ]
    if unfinished and all(step.status == STEP_BLOCKED for step in unfinished):
        return TASK_BLOCKED
    return TASK_ACTIVE


def _step_to_dict(step: TaskStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "description": step.description,
        "status": step.status,
        "note": step.note,
        "created_at": step.created_at,
        "updated_at": step.updated_at,
    }


def _task_to_dict(task: TaskPlan) -> dict[str, Any]:
    return {
        "id": task.id,
        "goal": task.goal,
        "status": task.status,
        "revision": task.revision,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "steps": [_step_to_dict(step) for step in task.steps],
    }


def _task_summary(task: TaskPlan) -> dict[str, Any]:
    counts = {status: 0 for status in sorted(VALID_STEP_STATUSES)}
    for step in task.steps:
        counts[step.status] += 1
    return {
        "id": task.id,
        "goal": task.goal,
        "status": task.status,
        "revision": task.revision,
        "step_count": len(task.steps),
        "step_status_counts": counts,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _event_to_dict(event: SessionEvent) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "type": event.type,
        "timestamp": event.timestamp,
        "summary": event.summary,
        "task_id": event.task_id,
        "step_id": event.step_id,
        "reference": event.reference,
    }


class SessionState:
    """Isolated state owned by one Agent/CLI process.

    Mutations are serialized with an ``RLock`` and replace immutable model
    instances atomically. The object never reads or writes files, accesses the
    network, runs commands, or approves execution.
    """

    def __init__(self) -> None:
        self.session_id = uuid.uuid4().hex
        self.created_at = _utc_now()
        self._active_task_id: str | None = None
        self._tasks: dict[str, TaskPlan] = {}
        self._events: deque[SessionEvent] = deque(maxlen=MAX_EVENTS)
        self._next_event_seq = 1
        self._dropped_event_count = 0
        self._lock = threading.RLock()

    @property
    def active_task_id(self) -> str | None:
        """Return the current task id without exposing mutable internals."""
        with self._lock:
            return self._active_task_id

    @property
    def tasks(self) -> tuple[TaskPlan, ...]:
        """Return immutable task snapshots in creation order."""
        with self._lock:
            return tuple(self._tasks.values())

    @property
    def recent_events(self) -> tuple[SessionEvent, ...]:
        """Return the bounded retained event history as an immutable tuple."""
        with self._lock:
            return tuple(self._events)

    @property
    def dropped_event_count(self) -> int:
        with self._lock:
            return self._dropped_event_count

    def create_task(self, goal: str, steps: Iterable[str]) -> TaskPlan:
        """Create a task, make it active, and record ``task_created``."""
        clean_goal = _normalize_text(
            goal, field_name="goal", max_chars=MAX_GOAL_CHARS
        )
        clean_steps = self._normalize_steps(steps)

        with self._lock:
            if len(self._tasks) >= MAX_TASKS:
                raise SessionStateError("Session task limit reached.")

            now = _utc_now()
            task_id = uuid.uuid4().hex
            task_steps = tuple(
                TaskStep(
                    id=index,
                    description=description,
                    status=STEP_PENDING,
                    note=None,
                    created_at=now,
                    updated_at=now,
                )
                for index, description in enumerate(clean_steps, start=1)
            )
            task = TaskPlan(
                id=task_id,
                goal=clean_goal,
                steps=task_steps,
                status=TASK_ACTIVE,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            self._tasks[task_id] = task
            self._active_task_id = task_id
            self._append_event_locked(
                event_type="task_created",
                summary=f"Task created with {len(task_steps)} step(s).",
                task_id=task_id,
            )
            return task

    def create_task_plan(self, goal: str, steps: Iterable[str]) -> TaskPlan:
        """Public task-planning name for :meth:`create_task`."""
        return self.create_task(goal, steps)

    def get_task(self, task_id: str) -> TaskPlan | None:
        """Return an immutable task snapshot, or ``None`` when unknown."""
        clean_task_id = self._normalize_task_id(task_id)
        with self._lock:
            return self._tasks.get(clean_task_id)

    def update_step(
        self,
        task_id: str,
        step_id: int,
        status: str,
        note: str | None = None,
    ) -> TaskPlan:
        """Apply one legal step transition and atomically revise its task."""
        clean_task_id = self._normalize_task_id(task_id)
        clean_step_id = self._normalize_step_id(step_id)
        clean_status = self._normalize_status(status)
        clean_note = self._normalize_note(note)

        with self._lock:
            task = self._require_task_locked(clean_task_id)
            step_index = next(
                (index for index, item in enumerate(task.steps) if item.id == clean_step_id),
                None,
            )
            if step_index is None:
                raise SessionStateError(
                    f"Unknown step id {clean_step_id} for task {clean_task_id}."
                )

            old_step = task.steps[step_index]
            if clean_status == old_step.status:
                raise SessionStateError(
                    f"Step {clean_step_id} is already '{clean_status}'."
                )
            if clean_status not in _ALLOWED_TRANSITIONS[old_step.status]:
                raise SessionStateError(
                    f"Invalid step transition: {old_step.status} -> {clean_status}."
                )

            now = _utc_now()
            new_step = replace(
                old_step,
                status=clean_status,
                note=clean_note,
                updated_at=now,
            )
            new_steps = list(task.steps)
            new_steps[step_index] = new_step
            immutable_steps = tuple(new_steps)
            updated_task = replace(
                task,
                steps=immutable_steps,
                status=_derive_task_status(immutable_steps),
                revision=task.revision + 1,
                updated_at=now,
            )
            self._tasks[clean_task_id] = updated_task
            self._append_event_locked(
                event_type="task_step_updated",
                summary=(
                    f"Step {clean_step_id} changed from {old_step.status} "
                    f"to {clean_status}."
                ),
                task_id=clean_task_id,
                step_id=clean_step_id,
            )
            return updated_task

    def update_task_step(
        self,
        task_id: str,
        step_id: int,
        status: str,
        note: str | None = None,
    ) -> TaskPlan:
        """Public task-planning name for :meth:`update_step`."""
        return self.update_step(task_id, step_id, status, note)

    def add_steps(self, task_id: str, steps: Iterable[str]) -> TaskPlan:
        """Append pending steps without deleting, renumbering, or reordering history.

        A ``completed`` task is terminal: new work belongs in a new task plan,
        so appending to a completed plan is rejected. ``blocked`` tasks may
        still accept steps (e.g. to investigate the blocker).
        """
        clean_task_id = self._normalize_task_id(task_id)
        clean_steps = self._normalize_steps(steps)

        with self._lock:
            task = self._require_task_locked(clean_task_id)
            if task.status == TASK_COMPLETED:
                raise SessionStateError(
                    f"Task {clean_task_id} is completed and terminal; "
                    "its steps cannot be extended. Create a new task plan "
                    "for new work."
                )
            if len(task.steps) + len(clean_steps) > MAX_STEPS_PER_TASK:
                raise SessionStateError(
                    f"Task step limit is {MAX_STEPS_PER_TASK}."
                )

            now = _utc_now()
            next_id = max((step.id for step in task.steps), default=0) + 1
            appended = tuple(
                TaskStep(
                    id=next_id + offset,
                    description=description,
                    status=STEP_PENDING,
                    note=None,
                    created_at=now,
                    updated_at=now,
                )
                for offset, description in enumerate(clean_steps)
            )
            all_steps = task.steps + appended
            updated_task = replace(
                task,
                steps=all_steps,
                status=_derive_task_status(all_steps),
                revision=task.revision + 1,
                updated_at=now,
            )
            self._tasks[clean_task_id] = updated_task
            self._append_event_locked(
                event_type="task_steps_added",
                summary=f"Added {len(appended)} task step(s).",
                task_id=clean_task_id,
            )
            return updated_task

    def add_task_steps(self, task_id: str, steps: Iterable[str]) -> TaskPlan:
        """Public task-planning name for :meth:`add_steps`."""
        return self.add_steps(task_id, steps)

    def snapshot(
        self,
        task_id: str | None = None,
        *,
        recent_event_limit: int = MAX_RECENT_EVENTS,
    ) -> dict[str, Any]:
        """Return fresh dictionaries/lists that cannot mutate internal state."""
        limit = self._normalize_recent_event_limit(recent_event_limit)
        with self._lock:
            if task_id is not None:
                clean_task_id = self._normalize_task_id(task_id)
                task = self._require_task_locked(clean_task_id)
                return {
                    "session_id": self.session_id,
                    "task": _task_to_dict(task),
                }

            active_task = (
                self._tasks.get(self._active_task_id)
                if self._active_task_id is not None
                else None
            )
            recent = list(self._events)[-limit:] if limit else []
            return {
                "session_id": self.session_id,
                "session": {
                    "id": self.session_id,
                    "created_at": self.created_at,
                    "active_task_id": self._active_task_id,
                    "task_count": len(self._tasks),
                    "retained_event_count": len(self._events),
                    "total_events": self._next_event_seq - 1,
                    "dropped_event_count": self._dropped_event_count,
                },
                "active_task": _task_to_dict(active_task) if active_task else None,
                "tasks": [_task_summary(task) for task in self._tasks.values()],
                "recent_events": [_event_to_dict(event) for event in recent],
                "events_truncated": (
                    self._dropped_event_count > 0 or len(self._events) > len(recent)
                ),
                "total_events": self._next_event_seq - 1,
                "dropped_event_count": self._dropped_event_count,
            }

    def get_task_state(self, task_id: str | None = None) -> dict[str, Any]:
        """Public task-planning name for a bounded default snapshot."""
        return self.snapshot(task_id=task_id)

    def record_execution_approved(self, plan_id: str) -> SessionEvent:
        """Record a host-verified approval decision as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="execution_approved",
                summary="Execution approved by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_execution_rejected(self, plan_id: str) -> SessionEvent:
        """Record a host-verified rejection decision as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="execution_rejected",
                summary="Execution rejected by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_execution_completed(
        self,
        plan_id: str,
        *,
        exit_code: int | None,
        timed_out: bool,
        duration_ms: int,
        stdout_truncated: bool,
        stderr_truncated: bool,
    ) -> SessionEvent:
        """Record bounded execution metadata, never stdout/stderr/environment."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise SessionStateError("exit_code must be an integer or None.")
        if not isinstance(timed_out, bool):
            raise SessionStateError("timed_out must be a boolean.")
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise SessionStateError("duration_ms must be a non-negative integer.")
        if not isinstance(stdout_truncated, bool) or not isinstance(stderr_truncated, bool):
            raise SessionStateError("truncation flags must be booleans.")

        summary = (
            f"Execution completed with exit_code={exit_code}, timed_out={timed_out}, "
            f"duration_ms={duration_ms}, stdout_truncated={stdout_truncated}, "
            f"stderr_truncated={stderr_truncated}."
        )
        with self._lock:
            return self._append_event_locked(
                event_type="execution_completed",
                summary=summary,
                reference=clean_plan_id,
            )

    def record_patch_approved(self, plan_id: str) -> SessionEvent:
        """Record a host-verified patch approval as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="patch_approved",
                summary="Source edit approved by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_patch_rejected(self, plan_id: str) -> SessionEvent:
        """Record a host-verified patch rejection as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="patch_rejected",
                summary="Source edit rejected by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_patch_applied(self, plan_id: str) -> SessionEvent:
        """Record a host-verified, successfully applied edit as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="patch_applied",
                summary="Source edit applied by the host.",
                reference=clean_plan_id,
            )

    def record_patch_conflict(self, plan_id: str) -> SessionEvent:
        """Record a host-verified apply conflict (no write) as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="patch_conflict",
                summary="Source edit apply conflicted; no file was written.",
                reference=clean_plan_id,
            )

    def record_patch_failed(self, plan_id: str) -> SessionEvent:
        """Record a host-verified apply failure as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="patch_failed",
                summary="Source edit apply failed; the source was not changed.",
                reference=clean_plan_id,
            )

    # -- Git mutation session events (v0.7.0) --------------------------------

    def record_git_stage_proposal(
        self, plan_id: str, path: str, summary: str
    ) -> SessionEvent:
        """Record a Git stage proposal as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        clean_summary = _normalize_text(summary, field_name="summary", max_chars=MAX_EVENT_SUMMARY_CHARS)
        event_summary = f"Git stage proposed: {path} - {clean_summary}"
        with self._lock:
            return self._append_event_locked(
                event_type="git_stage_proposal",
                summary=event_summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_stage_approved(self, plan_id: str) -> SessionEvent:
        """Record a host-verified Git stage approval as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="git_stage_approved",
                summary="Git stage approved by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_git_stage_rejected(self, plan_id: str) -> SessionEvent:
        """Record a host-verified Git stage rejection as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="git_stage_rejected",
                summary="Git stage rejected by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_git_stage_applied(self, plan_id: str, path: str | None = None) -> SessionEvent:
        """Record a host-verified, successfully applied Git stage as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        summary = f"Git stage applied by the host: {path}" if path else "Git stage applied by the host."
        with self._lock:
            return self._append_event_locked(
                event_type="git_stage_applied",
                summary=summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_stage_conflict(self, plan_id: str, reason: str | None = None) -> SessionEvent:
        """Record a host-verified Git stage conflict (no index mutation) as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        if reason:
            clean_reason = _normalize_text(reason, field_name="reason", max_chars=MAX_EVENT_SUMMARY_CHARS - 50)
            summary = f"Git stage conflicted: {clean_reason}"
        else:
            summary = "Git stage conflicted; no index mutation occurred."
        with self._lock:
            return self._append_event_locked(
                event_type="git_stage_conflict",
                summary=summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_stage_failed(self, plan_id: str, error: str | None = None) -> SessionEvent:
        """Record a host-verified Git stage failure as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        if error:
            clean_error = _normalize_text(error, field_name="error", max_chars=MAX_EVENT_SUMMARY_CHARS - 50)
            summary = f"Git stage failed: {clean_error}"
        else:
            summary = "Git stage failed; the index was not mutated."
        with self._lock:
            return self._append_event_locked(
                event_type="git_stage_failed",
                summary=summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_commit_proposal(
        self, plan_id: str, message: str, staged_file_count: int
    ) -> SessionEvent:
        """Record a Git commit proposal as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        clean_message = _normalize_text(message, field_name="message", max_chars=MAX_EVENT_SUMMARY_CHARS)
        event_summary = f"Git commit proposed: {staged_file_count} file(s) - {clean_message}"
        with self._lock:
            return self._append_event_locked(
                event_type="git_commit_proposal",
                summary=event_summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_commit_approved(self, plan_id: str) -> SessionEvent:
        """Record a host-verified Git commit approval as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="git_commit_approved",
                summary="Git commit approved by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_git_commit_rejected(self, plan_id: str) -> SessionEvent:
        """Record a host-verified Git commit rejection as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        with self._lock:
            return self._append_event_locked(
                event_type="git_commit_rejected",
                summary="Git commit rejected by the host-side user approval flow.",
                reference=clean_plan_id,
            )

    def record_git_commit_applied(self, plan_id: str, commit_oid: str | None = None) -> SessionEvent:
        """Record a host-verified, successfully created Git commit as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        summary = f"Git commit created by the host: {commit_oid if commit_oid else 'unknown'}"
        with self._lock:
            return self._append_event_locked(
                event_type="git_commit_applied",
                summary=summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_commit_conflict(self, plan_id: str, reason: str | None = None) -> SessionEvent:
        """Record a host-verified Git commit conflict (no commit created) as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        if reason:
            clean_reason = _normalize_text(reason, field_name="reason", max_chars=MAX_EVENT_SUMMARY_CHARS - 50)
            summary = f"Git commit conflicted: {clean_reason}"
        else:
            summary = "Git commit conflicted; no local commit was created."
        with self._lock:
            return self._append_event_locked(
                event_type="git_commit_conflict",
                summary=summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def record_git_commit_failed(self, plan_id: str, error: str | None = None) -> SessionEvent:
        """Record a host-verified Git commit failure as metadata only."""
        clean_plan_id = self._normalize_reference(plan_id, "plan_id")
        if error:
            clean_error = _normalize_text(error, field_name="error", max_chars=MAX_EVENT_SUMMARY_CHARS - 50)
            summary = f"Git commit failed: {clean_error}"
        else:
            summary = "Git commit failed; no local commit was created."
        with self._lock:
            return self._append_event_locked(
                event_type="git_commit_failed",
                summary=summary[:MAX_EVENT_SUMMARY_CHARS],
                reference=clean_plan_id,
            )

    def get_git_mutation_events(self) -> list[dict[str, Any]]:
        """Get all Git mutation events from the session.

        Returns a list of event dicts with 'event' (type), 'plan_id', and other fields.
        """
        from datetime import datetime

        with self._lock:
            git_event_types = {
                "git_stage_proposal",
                "git_stage_approved",
                "git_stage_rejected",
                "git_stage_applied",
                "git_stage_conflict",
                "git_stage_failed",
                "git_commit_proposal",
                "git_commit_approved",
                "git_commit_rejected",
                "git_commit_applied",
                "git_commit_conflict",
                "git_commit_failed",
            }

            result = []
            for event in self._events:
                if event.type in git_event_types:
                    # Convert ISO 8601 timestamp to Unix timestamp
                    timestamp_str = event.timestamp.replace('Z', '+00:00')
                    timestamp = datetime.fromisoformat(timestamp_str).timestamp()

                    event_dict = {
                        "event": event.type,
                        "plan_id": event.reference,
                        "timestamp": timestamp,
                        "summary": event.summary,
                    }
                    # Extract path from summary for stage events
                    if event.type == "git_stage_proposal" and "Git stage proposed:" in event.summary:
                        parts = event.summary.split(":", 1)
                        if len(parts) > 1:
                            path_and_summary = parts[1].strip()
                            if " - " in path_and_summary:
                                path = path_and_summary.split(" - ")[0].strip()
                                event_dict["path"] = path
                    elif event.type == "git_stage_applied" and "Git stage applied by the host:" in event.summary:
                        parts = event.summary.split(":", 1)
                        if len(parts) > 1:
                            event_dict["path"] = parts[1].strip()
                    # Extract commit_oid from summary for commit applied events
                    elif event.type == "git_commit_applied" and "Git commit created by the host:" in event.summary:
                        parts = event.summary.split(":", 1)
                        if len(parts) > 1:
                            event_dict["commit_oid"] = parts[1].strip()

                    result.append(event_dict)

            return result

    def _append_event_locked(
        self,
        *,
        event_type: str,
        summary: str,
        task_id: str | None = None,
        step_id: int | None = None,
        reference: str | None = None,
    ) -> SessionEvent:
        """Append one event. Caller must hold ``self._lock``."""
        clean_type = _normalize_text(
            event_type, field_name="event type", max_chars=100
        )
        clean_summary = _normalize_text(
            summary,
            field_name="event summary",
            max_chars=MAX_EVENT_SUMMARY_CHARS,
        )
        if len(self._events) == MAX_EVENTS:
            self._dropped_event_count += 1
        event = SessionEvent(
            seq=self._next_event_seq,
            type=clean_type,
            timestamp=_utc_now(),
            summary=clean_summary,
            task_id=task_id,
            step_id=step_id,
            reference=reference,
        )
        self._events.append(event)
        self._next_event_seq += 1
        return event

    def _require_task_locked(self, task_id: str) -> TaskPlan:
        task = self._tasks.get(task_id)
        if task is None:
            raise SessionStateError(f"Unknown task id: {task_id}.")
        return task

    @staticmethod
    def _normalize_steps(steps: Iterable[str]) -> tuple[str, ...]:
        if isinstance(steps, (str, bytes)) or not isinstance(steps, Iterable):
            raise SessionStateError("steps must be a list of strings.")
        raw_steps = tuple(steps)
        if not raw_steps:
            raise SessionStateError("A task plan requires at least one step.")
        if len(raw_steps) > MAX_STEPS_PER_TASK:
            raise SessionStateError(
                f"Task step limit is {MAX_STEPS_PER_TASK}."
            )
        return tuple(
            _normalize_text(
                item,
                field_name=f"step {index}",
                max_chars=MAX_STEP_CHARS,
            )
            for index, item in enumerate(raw_steps, start=1)
        )

    @staticmethod
    def _normalize_task_id(task_id: str) -> str:
        return _normalize_text(task_id, field_name="task_id", max_chars=64)

    @staticmethod
    def _normalize_step_id(step_id: int) -> int:
        if not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 1:
            raise SessionStateError("step_id must be a positive integer.")
        return step_id

    @staticmethod
    def _normalize_status(status: str) -> str:
        clean_status = _normalize_text(status, field_name="status", max_chars=32)
        if clean_status not in VALID_STEP_STATUSES:
            allowed = ", ".join(sorted(VALID_STEP_STATUSES))
            raise SessionStateError(f"Invalid step status. Allowed values: {allowed}.")
        return clean_status

    @staticmethod
    def _normalize_note(note: str | None) -> str | None:
        if note is None:
            return None
        clean_note = _normalize_text(
            note,
            field_name="note",
            max_chars=MAX_NOTE_CHARS,
            allow_blank=True,
        )
        return clean_note or None

    @staticmethod
    def _normalize_reference(reference: str, field_name: str) -> str:
        return _normalize_text(
            reference,
            field_name=field_name,
            max_chars=MAX_REFERENCE_CHARS,
        )

    @staticmethod
    def _normalize_recent_event_limit(limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise SessionStateError("recent_event_limit must be a non-negative integer.")
        return min(limit, MAX_RECENT_EVENTS)
