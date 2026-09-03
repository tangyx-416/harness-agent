"""Tests for the bounded, thread-safe ephemeral SessionState."""

from __future__ import annotations

import dataclasses
import threading

import pytest

from harness_agent.session import (
    MAX_EVENTS,
    MAX_GOAL_CHARS,
    MAX_NOTE_CHARS,
    MAX_STEPS_PER_TASK,
    MAX_STEP_CHARS,
    MAX_TASKS,
    STEP_BLOCKED,
    STEP_COMPLETED,
    STEP_IN_PROGRESS,
    STEP_PENDING,
    STEP_SKIPPED,
    SessionState,
    SessionStateError,
)


def make_task(state: SessionState, step_count: int = 1):
    steps = [f"Step {i}" for i in range(1, step_count + 1)]
    return state.create_task("Verify the implementation", steps)


def test_new_session_is_empty():
    state = SessionState()
    snapshot = state.snapshot()
    assert snapshot["session"]["task_count"] == 0
    assert snapshot["session"]["active_task_id"] is None
    assert snapshot["active_task"] is None
    assert snapshot["tasks"] == []
    assert snapshot["recent_events"] == []
    assert state.tasks == ()
    assert state.recent_events == ()


def test_session_ids_are_unique_uuid_hex():
    first = SessionState()
    second = SessionState()
    assert first.session_id != second.session_id
    assert len(first.session_id) == 32
    int(first.session_id, 16)
    assert first.created_at.endswith("Z")


def test_sessions_are_isolated_and_newest_task_is_active():
    first = SessionState()
    second = SessionState()
    old = first.create_task("First", ["One"])
    newest = first.create_task("Second", ["Two"])

    assert first.active_task_id == newest.id
    assert first.get_task(old.id) == old
    assert second.snapshot()["tasks"] == []
    assert second.get_task(old.id) is None


def test_multiple_tasks_remain_queryable():
    state = SessionState()
    tasks = [state.create_task(f"Goal {index}", ["Step"]) for index in range(3)]
    assert [item["id"] for item in state.snapshot()["tasks"]] == [task.id for task in tasks]
    assert state.snapshot(tasks[0].id)["task"]["goal"] == "Goal 0"


def test_task_limit_rejects_without_silent_eviction():
    state = SessionState()
    ids = [state.create_task(f"Goal {i}", ["Step"]).id for i in range(MAX_TASKS)]
    with pytest.raises(SessionStateError, match="Session task limit reached"):
        state.create_task("One too many", ["Step"])
    assert [item["id"] for item in state.snapshot()["tasks"]] == ids


def test_event_limit_is_bounded_and_sequence_is_monotonic():
    state = SessionState()
    for index in range(MAX_EVENTS + 7):
        state.record_execution_rejected(f"plan-{index}")

    snapshot = state.snapshot()
    assert snapshot["session"]["retained_event_count"] == MAX_EVENTS
    assert snapshot["total_events"] == MAX_EVENTS + 7
    assert snapshot["dropped_event_count"] == 7
    assert snapshot["events_truncated"] is True
    assert [event["seq"] for event in snapshot["recent_events"]] == list(
        range(MAX_EVENTS - 12, MAX_EVENTS + 8)
    )


def test_recent_event_output_is_capped_at_twenty():
    state = SessionState()
    for index in range(30):
        state.record_execution_rejected(f"plan-{index}")
    assert len(state.snapshot(recent_event_limit=999)["recent_events"]) == 20
    assert state.snapshot(recent_event_limit=0)["recent_events"] == []


def test_snapshot_cannot_mutate_internal_state():
    state = SessionState()
    task = make_task(state)
    snapshot = state.snapshot()
    snapshot["active_task"]["goal"] = "tampered"
    snapshot["active_task"]["steps"][0]["status"] = STEP_COMPLETED
    snapshot["tasks"].clear()
    snapshot["recent_events"][0]["summary"] = "tampered"

    fresh = state.snapshot()
    assert fresh["active_task"]["goal"] == task.goal
    assert fresh["active_task"]["steps"][0]["status"] == STEP_PENDING
    assert len(fresh["tasks"]) == 1
    assert fresh["recent_events"][0]["summary"] != "tampered"


def test_models_are_frozen_and_steps_are_immutable_tuple():
    state = SessionState()
    task = make_task(state)
    assert isinstance(task.steps, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.status = "completed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.steps[0].status = "completed"  # type: ignore[misc]


def test_unicode_and_newline_normalization():
    state = SessionState()
    task = state.create_task("  检查当前代码并运行测试\r\n  ", ["  阅读源码  "])
    updated = state.update_step(task.id, 1, STEP_COMPLETED, "  已检查：正常。  ")
    assert updated.goal == "检查当前代码并运行测试"
    assert updated.steps[0].description == "阅读源码"
    assert updated.steps[0].note == "已检查：正常。"


@pytest.mark.parametrize(
    ("field", "operation"),
    [
        ("goal", lambda state: state.create_task("bad\x00goal", ["step"])),
        ("step", lambda state: state.create_task("goal", ["bad\x00step"])),
        (
            "note",
            lambda state: state.update_step(
                make_task(state).id, 1, STEP_COMPLETED, "bad\x00note"
            ),
        ),
    ],
)
def test_nul_is_rejected(field, operation):
    with pytest.raises(SessionStateError, match=field):
        operation(SessionState())


@pytest.mark.parametrize(
    "operation",
    [
        lambda state: state.create_task("x" * (MAX_GOAL_CHARS + 1), ["step"]),
        lambda state: state.create_task("goal", ["x" * (MAX_STEP_CHARS + 1)]),
        lambda state: state.update_step(
            make_task(state).id,
            1,
            STEP_COMPLETED,
            "x" * (MAX_NOTE_CHARS + 1),
        ),
    ],
)
def test_text_length_limits(operation):
    with pytest.raises(SessionStateError, match="limit"):
        operation(SessionState())


@pytest.mark.parametrize("goal", ["", "   ", "\r\n"])
def test_blank_goal_rejected(goal):
    with pytest.raises(SessionStateError, match="goal must not be blank"):
        SessionState().create_task(goal, ["step"])


def test_create_one_step_and_max_steps():
    state = SessionState()
    one = state.create_task("One", ["Only"])
    maximum = state.create_task(
        "Maximum", [f"Step {index}" for index in range(MAX_STEPS_PER_TASK)]
    )
    assert len(one.steps) == 1
    assert len(maximum.steps) == MAX_STEPS_PER_TASK


@pytest.mark.parametrize("steps", [[], (), iter(())])
def test_zero_steps_rejected(steps):
    with pytest.raises(SessionStateError, match="at least one"):
        SessionState().create_task("Goal", steps)


def test_too_many_steps_rejected():
    with pytest.raises(SessionStateError, match="step limit"):
        SessionState().create_task(
            "Goal", ["Step"] * (MAX_STEPS_PER_TASK + 1)
        )


@pytest.mark.parametrize("step", ["", "  ", "\r\n"])
def test_blank_step_rejected(step):
    with pytest.raises(SessionStateError, match="step 1 must not be blank"):
        SessionState().create_task("Goal", [step])


def test_task_creation_has_stable_ids_revision_and_pending_steps():
    task = make_task(SessionState(), 3)
    assert len(task.id) == 32
    int(task.id, 16)
    assert task.revision == 1
    assert task.status == "active"
    assert task.created_at.endswith("Z")
    assert [step.id for step in task.steps] == [1, 2, 3]
    assert {step.status for step in task.steps} == {STEP_PENDING}


def test_public_task_planning_method_names_map_to_state_operations():
    state = SessionState()
    task = state.create_task_plan("Goal", ["One"])
    updated = state.update_task_step(task.id, 1, STEP_COMPLETED, "Done")
    assert updated.status == "completed"
    # A blocked task may still take new steps.
    task_b = state.create_task_plan("Goal B", ["One"])
    state.update_task_step(task_b.id, 1, STEP_BLOCKED, "Blocked")
    refined = state.add_task_steps(task_b.id, ["Two"])
    assert refined.steps[-1].id == 2
    assert state.get_task_state(task_b.id)["task"]["revision"] == 3


@pytest.mark.parametrize(
    "target", [STEP_IN_PROGRESS, STEP_COMPLETED, STEP_BLOCKED, STEP_SKIPPED]
)
def test_pending_transitions(target):
    state = SessionState()
    task = make_task(state)
    updated = state.update_step(task.id, 1, target, f"Observed {target}")
    assert updated.steps[0].status == target
    assert updated.revision == 2


@pytest.mark.parametrize(
    "target", [STEP_PENDING, STEP_COMPLETED, STEP_BLOCKED, STEP_SKIPPED]
)
def test_in_progress_transitions(target):
    state = SessionState()
    task = make_task(state)
    state.update_step(task.id, 1, STEP_IN_PROGRESS)
    updated = state.update_step(task.id, 1, target)
    assert updated.steps[0].status == target
    assert updated.revision == 3


@pytest.mark.parametrize("target", [STEP_PENDING, STEP_IN_PROGRESS, STEP_SKIPPED])
def test_blocked_transitions(target):
    state = SessionState()
    task = make_task(state)
    state.update_step(task.id, 1, STEP_BLOCKED)
    updated = state.update_step(task.id, 1, target)
    assert updated.steps[0].status == target


@pytest.mark.parametrize("terminal", [STEP_COMPLETED, STEP_SKIPPED])
@pytest.mark.parametrize(
    "target", [STEP_PENDING, STEP_IN_PROGRESS, STEP_COMPLETED, STEP_BLOCKED, STEP_SKIPPED]
)
def test_terminal_steps_cannot_reopen(terminal, target):
    state = SessionState()
    task = make_task(state)
    state.update_step(task.id, 1, terminal)
    with pytest.raises(SessionStateError, match="already|Invalid step transition"):
        state.update_step(task.id, 1, target)


def test_invalid_status_task_and_step_ids_are_structured_errors():
    state = SessionState()
    task = make_task(state)
    with pytest.raises(SessionStateError, match="Invalid step status"):
        state.update_step(task.id, 1, "almost_done")
    with pytest.raises(SessionStateError, match="Unknown task id"):
        state.update_step("f" * 32, 1, STEP_COMPLETED)
    with pytest.raises(SessionStateError, match="Unknown step id"):
        state.update_step(task.id, 99, STEP_COMPLETED)


def test_task_status_is_automatically_completed():
    state = SessionState()
    task = make_task(state, 2)
    first = state.update_step(task.id, 1, STEP_COMPLETED)
    assert first.status == "active"
    final = state.update_step(task.id, 2, STEP_SKIPPED)
    assert final.status == "completed"


def test_task_status_is_blocked_when_all_unfinished_steps_are_blocked():
    state = SessionState()
    task = make_task(state, 3)
    state.update_step(task.id, 1, STEP_COMPLETED)
    state.update_step(task.id, 2, STEP_BLOCKED)
    final = state.update_step(task.id, 3, STEP_BLOCKED)
    assert final.status == "blocked"


def test_append_one_and_multiple_steps_preserves_history_and_ids():
    state = SessionState()
    task = make_task(state, 2)
    original = task.steps
    once = state.add_steps(task.id, ["Third"])
    twice = state.add_steps(task.id, ["Fourth", "Fifth"])
    assert once.steps[:2] == original
    assert [step.id for step in twice.steps] == [1, 2, 3, 4, 5]
    assert [step.status for step in twice.steps[2:]] == [STEP_PENDING] * 3
    assert once.revision == 2
    assert twice.revision == 3


def test_append_respects_total_step_limit():
    state = SessionState()
    task = make_task(state, MAX_STEPS_PER_TASK)
    with pytest.raises(SessionStateError, match="step limit"):
        state.add_steps(task.id, ["Overflow"])
    assert state.get_task(task.id) == task


def test_append_unknown_task_is_rejected():
    with pytest.raises(SessionStateError, match="Unknown task id"):
        SessionState().add_steps("f" * 32, ["Step"])


def test_concurrent_updates_have_atomic_revisions_and_unique_events():
    state = SessionState()
    task = make_task(state, MAX_STEPS_PER_TASK)
    barrier = threading.Barrier(MAX_STEPS_PER_TASK)
    errors = []

    def worker(step_id):
        try:
            barrier.wait()
            state.update_step(task.id, step_id, STEP_COMPLETED, "Observed")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(step_id,))
        for step_id in range(1, MAX_STEPS_PER_TASK + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    final = state.get_task(task.id)
    assert final is not None
    assert final.revision == 1 + MAX_STEPS_PER_TASK
    assert final.status == "completed"
    events = state.snapshot()["recent_events"]
    assert [event["seq"] for event in events] == list(
        range(2, MAX_STEPS_PER_TASK + 2)
    )


def test_concurrent_sessions_never_leak():
    states = [SessionState() for _ in range(12)]
    threads = [
        threading.Thread(target=state.create_task, args=(f"Goal {index}", ["Step"]))
        for index, state in enumerate(states)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(state.snapshot()["session"]["task_count"] == 1 for state in states)
    assert len({state.snapshot()["active_task"]["id"] for state in states}) == len(states)


# ---------------------------------------------------------------------------
# v0.5.0 audit: completed-terminal append, active-task, status derivation,
# revision atomicity, concurrent competing transitions, event bounds
# ---------------------------------------------------------------------------


def test_completed_task_is_terminal_for_add_steps():
    """append to a completed plan is rejected (new work -> new plan)."""
    state = SessionState()
    task = state.create_task("Done work", ["One"])
    state.update_step(task.id, 1, STEP_COMPLETED, "Done")
    assert state.get_task(task.id).status == "completed"

    with pytest.raises(SessionStateError, match="completed and terminal"):
        state.add_steps(task.id, ["More"])
    # The completed task is unchanged (revision not bumped).
    assert state.get_task(task.id).revision == 2


def test_blocked_task_may_accept_new_steps():
    state = SessionState()
    task = state.create_task("Blocked work", ["One"])
    state.update_step(task.id, 1, STEP_BLOCKED, "Blocked")
    refined = state.add_steps(task.id, ["Investigate blocker"])
    assert [step.id for step in refined.steps] == [1, 2]
    assert refined.steps[1].status == STEP_PENDING
    assert refined.status == "active"


def test_completed_task_terminal_only_after_fully_done():
    """A task is terminal only when its derived status is completed."""
    state = SessionState()
    task = state.create_task("Mixed", ["One", "Two"])
    state.update_step(task.id, 1, STEP_COMPLETED)
    # step 2 still pending -> task is active, append allowed.
    assert state.get_task(task.id).status == "active"
    extended = state.add_steps(task.id, ["Three"])
    assert len(extended.steps) == 3


def test_skipped_and_blocked_combined_derivation():
    from harness_agent.session import TASK_ACTIVE, TASK_BLOCKED, TASK_COMPLETED

    # completed + skipped only -> completed
    s = SessionState()
    t = s.create_task("G", ["a", "b"])
    s.update_step(t.id, 1, STEP_COMPLETED)
    s.update_step(t.id, 2, STEP_SKIPPED)
    assert s.get_task(t.id).status == TASK_COMPLETED

    # completed + blocked -> blocked
    s = SessionState()
    t = s.create_task("G", ["a", "b"])
    s.update_step(t.id, 1, STEP_COMPLETED)
    s.update_step(t.id, 2, STEP_BLOCKED)
    assert s.get_task(t.id).status == TASK_BLOCKED

    # skipped + blocked -> blocked
    s = SessionState()
    t = s.create_task("G", ["a", "b"])
    s.update_step(t.id, 1, STEP_SKIPPED)
    s.update_step(t.id, 2, STEP_BLOCKED)
    assert s.get_task(t.id).status == TASK_BLOCKED

    # blocked + pending -> active
    s = SessionState()
    t = s.create_task("G", ["a", "b"])
    s.update_step(t.id, 1, STEP_BLOCKED)
    assert s.get_task(t.id).status == TASK_ACTIVE

    # blocked + in_progress -> active
    s = SessionState()
    t = s.create_task("G", ["a", "b"])
    s.update_step(t.id, 1, STEP_BLOCKED)
    s.update_step(t.id, 2, STEP_IN_PROGRESS)
    assert s.get_task(t.id).status == TASK_ACTIVE

    # pending only -> active
    s = SessionState()
    t = s.create_task("G", ["a"])
    assert s.get_task(t.id).status == TASK_ACTIVE

    # in_progress only -> active
    s = SessionState()
    t = s.create_task("G", ["a"])
    s.update_step(t.id, 1, STEP_IN_PROGRESS)
    assert s.get_task(t.id).status == TASK_ACTIVE


def test_active_task_id_persists_after_completion():
    """active_task_id keeps pointing at the last selected task even if terminal."""
    from harness_agent.session import TASK_COMPLETED

    state = SessionState()
    task = state.create_task("Only task", ["a"])
    state.update_step(task.id, 1, STEP_COMPLETED)
    assert task.id == state.active_task_id
    snap = state.snapshot()
    assert snap["active_task"]["status"] == TASK_COMPLETED


def test_failed_mutations_do_not_bump_revision():
    # Unknown step id.
    st = SessionState()
    task = make_task(st)
    rev = st.get_task(task.id).revision
    with pytest.raises(SessionStateError):
        st.update_step(task.id, 99, STEP_COMPLETED)
    assert st.get_task(task.id).revision == rev

    # Invalid status.
    st = SessionState()
    task = make_task(st)
    rev = st.get_task(task.id).revision
    with pytest.raises(SessionStateError):
        st.update_step(task.id, 1, "bogus")
    assert st.get_task(task.id).revision == rev

    # Too many steps.
    st = SessionState()
    task = make_task(st, MAX_STEPS_PER_TASK)
    rev = st.get_task(task.id).revision
    with pytest.raises(SessionStateError):
        st.add_steps(task.id, ["Overflow"])
    assert st.get_task(task.id).revision == rev

    # Blank step.
    st = SessionState()
    task = make_task(st)
    rev = st.get_task(task.id).revision
    with pytest.raises(SessionStateError):
        st.add_steps(task.id, [""])
    assert st.get_task(task.id).revision == rev


def test_successful_mutations_bump_revision_exactly_once():
    st = SessionState()
    task = make_task(st, 2)
    assert st.update_step(task.id, 1, STEP_IN_PROGRESS).revision == 2
    assert st.update_step(task.id, 2, STEP_BLOCKED).revision == 3
    assert st.add_steps(task.id, ["Three"]).revision == 4


def test_competing_same_step_transitions_only_one_wins():
    """Two threads race pending->completed vs pending->blocked; one must win."""
    st = SessionState()
    task = make_task(st)
    statuses = []
    barrier = threading.Barrier(2)

    def to_completed():
        barrier.wait()
        try:
            st.update_step(task.id, 1, STEP_COMPLETED, "completed wins")
        except SessionStateError:
            statuses.append("completed-lost")

    def to_blocked():
        barrier.wait()
        try:
            st.update_step(task.id, 1, STEP_BLOCKED, "blocked wins")
        except SessionStateError:
            statuses.append("blocked-lost")

    threads = [
        threading.Thread(target=to_completed),
        threading.Thread(target=to_blocked),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = st.get_task(task.id)
    assert final.steps[0].status in {STEP_COMPLETED, STEP_BLOCKED}
    # only one legal transition succeeded
    assert len(statuses) == 1
    assert final.revision == 2  # exactly one mutation


def test_event_sequence_is_monotonic_beyond_cap():
    """>250 events: retained<=200, dropped>0, seq strictly increasing."""
    st = SessionState()
    for index in range(250):
        st.record_execution_rejected(f"plan-{index}")

    snap = st.snapshot(recent_event_limit=MAX_EVENTS)
    assert snap["session"]["retained_event_count"] == MAX_EVENTS
    assert snap["dropped_event_count"] == 250 - MAX_EVENTS
    seqs = [e["seq"] for e in snap["recent_events"]]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # unique
    assert seqs[-1] == 250  # latest seq == 250 > 200 (monotonic counter, not len)
    # recent_event_limit is capped at MAX_RECENT_EVENTS output, but the true
    # retained set (all 200) has no duplicate seq values.
    internal = list(st.recent_events)
    assert len(internal) == MAX_EVENTS
    assert [e.seq for e in internal] == sorted(e.seq for e in internal)
    assert len({e.seq for e in internal}) == MAX_EVENTS


def test_host_execution_events_keep_minimal_metadata():
    """Approval/completion events hold plan_id reference and flags, not output."""
    state = SessionState()
    task = state.create_task("Run tests", ["Run focused tests"])
    state.update_step(task.id, 1, "in_progress", "awaiting approval")
    plan_ref = "plan-abc-123"
    state.record_execution_approved(plan_ref)
    state.record_execution_completed(
        plan_ref,
        exit_code=0,
        timed_out=False,
        duration_ms=1200,
        stdout_truncated=True,
        stderr_truncated=False,
    )
    events = state.snapshot(recent_event_limit=10)["recent_events"]
    exec_events = [e for e in events if e["type"].startswith("execution_")]
    types = [e["type"] for e in events]
    assert "execution_approved" in types
    assert "execution_completed" in types
    # The execution events reference the plan_id without copying the raw
    # command, its stdout, or its stderr. Only structured flags are retained.
    for event in exec_events:
        assert event["reference"] == plan_ref
        assert "secret" not in event.get("summary", "")
    completed = [e for e in exec_events if e["type"] == "execution_completed"][0]
    assert completed["summary"] == (
        "Execution completed with exit_code=0, timed_out=False, "
        "duration_ms=1200, stdout_truncated=True, stderr_truncated=False."
    )
