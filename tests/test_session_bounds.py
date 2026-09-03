"""v0.5.0 audit: context-bound stress, snapshot deep immutability, ownership.

Section 15: get_task_state() default must stay bounded (summaries, not full
steps for every task). Section 16: snapshot returns deep copies that cannot
mutate internal state. Section 5: explicit two-parameter ownership is kept
(no RuntimeContext abstraction).
"""

from __future__ import annotations

import json

from harness_agent.session import (
    MAX_EVENTS,
    MAX_RECENT_EVENTS,
    MAX_STEPS_PER_TASK,
    MAX_TASKS,
    SessionState,
)
from harness_agent.tools.task_tools import get_task_state_core


def test_default_get_task_state_is_bounded_at_scale():
    """20 tasks x 20 steps x 200 events keeps the default result small."""
    state = SessionState()
    tasks = []
    for t in range(MAX_TASKS):
        task = state.create_task(
            f"Goal {t}",
            [f"Step {t}-{s}" for s in range(1, MAX_STEPS_PER_TASK + 1)],
        )
        # make a few steps non-pending
        state.update_step(task.id, 1, "in_progress")
        state.update_step(task.id, 2, "completed")
        tasks.append(task)

    # bump events toward the cap
    for i in range(MAX_EVENTS - len(tasks)):
        state.record_execution_rejected(f"plan-{i}")

    payload = get_task_state_core(state)
    assert payload["ok"] is True
    data = payload

    # Default view: active task + task SUMMARIES (no full 20-step bodies for
    # all 19 other tasks), and at most MAX_RECENT_EVENTS events.
    assert data["active_task"]["id"] == tasks[-1].id
    assert data["active_task"]["status"] == "active"
    assert len(data["tasks"]) == MAX_TASKS
    assert "steps" not in data["tasks"][0]  # summaries do not carry full steps
    assert len(data["recent_events"]) <= MAX_RECENT_EVENTS
    # seq stays monotonic past the retained window (counter, not positioning).
    tail = [e["seq"] for e in data["recent_events"]]
    assert tail == sorted(tail)
    assert len(set(tail)) == len(tail)

    body = json.dumps(data).encode("utf-8")
    # Reasonable bound for the default (summary) view.
    assert len(body) < 50_000


def test_specific_task_returns_full_steps_only_for_that_task():
    state = SessionState()
    state.create_task("Goal A", ["A1", "A2"])
    target = state.create_task("Goal B", ["B1", "B2", "B3"])

    specific = get_task_state_core(state, target.id)
    assert specific["task"]["id"] == target.id
    assert [s["description"] for s in specific["task"]["steps"]] == [
        "B1",
        "B2",
        "B3",
    ]
    assert "session" not in specific and "tasks" not in specific


def test_snapshot_returns_deep_copies_without_internal_alias():
    """Mutating nested dicts/lists in returned snapshots never affects state."""
    state = SessionState()
    task = state.create_task("Goal", ["One", "Two"])
    state.update_step(task.id, 1, "completed", "done")
    state.record_execution_approved("plan-x")

    snap = state.snapshot()
    # Nested mutation attempts on every container level.
    snap["session"]["active_task_id"] = "hijacked"
    snap["active_task"]["goal"] = "tampered"
    snap["active_task"]["steps"][0]["status"] = "blocked"
    snap["active_task"]["steps"][0]["note"] = "tampered"
    snap["tasks"][0]["goal"] = "tampered"
    snap["tasks"][0]["step_status_counts"]["completed"] = 0
    snap["recent_events"][0]["summary"] = "tampered"

    fresh = state.snapshot()
    assert fresh["session"]["active_task_id"] == task.id
    assert fresh["active_task"]["goal"] == "Goal"
    assert fresh["active_task"]["steps"][0]["status"] == "completed"
    assert fresh["active_task"]["steps"][0]["note"] == "done"
    assert fresh["tasks"][0]["goal"] == "Goal"
    assert fresh["tasks"][0]["step_status_counts"]["completed"] == 1
    assert fresh["recent_events"][0]["summary"] != "tampered"
    # Original model objects are untouched too.
    assert state.get_task(task.id).steps[0].status == "completed"


def test_explicit_process_local_ownership_two_params():
    """create_agent accepts separate session_state and execution_broker params.

    This documents the deliberate decision (Section 5) to keep two explicit,
    host-supplied dependencies instead of introducing a RuntimeContext object.
    """
    import inspect

    from harness_agent.agent import create_agent

    sig = inspect.signature(create_agent)
    params = list(sig.parameters)
    assert "session_state" in params
    assert "execution_broker" in params
    # A RuntimeContext was NOT introduced.
    assert "runtime_context" not in params
    assert "runtime" not in params
