"""Immutable models for ephemeral structured task planning (v0.5.0)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskStep:
    """One concise, user-visible unit of work in a task plan."""

    id: int
    description: str
    status: str
    note: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskPlan:
    """An immutable snapshot of a task and its ordered steps."""

    id: str
    goal: str
    steps: tuple[TaskStep, ...]
    status: str
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SessionEvent:
    """Short metadata describing an observable session-state event."""

    seq: int
    type: str
    timestamp: str
    summary: str
    task_id: str | None = None
    step_id: int | None = None
    reference: str | None = None
