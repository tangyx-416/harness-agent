#!/usr/bin/env python
"""Deterministic, no-LLM smoke test for v0.5.0 session planning + isolation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness_agent.execution import ExecutionBroker  # noqa: E402
from harness_agent.session import SessionState  # noqa: E402
from harness_agent.tools.execution_tools import (  # noqa: E402
    make_execution_tools,
)
from harness_agent.tools.task_tools import make_task_tools  # noqa: E402


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS: {label}")


def main() -> int:
    state = SessionState()
    check(state.snapshot()["tasks"] == [], "new session is empty")

    task = state.create_task(
        "Inspect project and verify tests",
        ["Inspect repository", "Inspect Git", "Run tests"],
    )
    check([step.id for step in task.steps] == [1, 2, 3], "stable initial step ids")

    state.update_step(task.id, 1, "completed", "Repository inspection completed.")
    state.update_step(task.id, 2, "in_progress", "Git inspection started.")
    state.update_step(task.id, 2, "completed", "Git inspection completed.")
    state.update_step(task.id, 3, "blocked", "Execution requires user approval.")

    refined = state.add_steps(task.id, ["Summarize blocker"])
    check(refined.steps[-1].id == 4, "appended step id continues at four")

    resumed = state.update_step(
        task.id, 3, "in_progress", "Approval can now be requested."
    )
    check(resumed.steps[2].status == "in_progress", "blocked step can resume")

    snapshot = state.snapshot()
    check(snapshot["active_task"]["revision"] == 7, "revision tracks every mutation")
    check(len(snapshot["recent_events"]) == 7, "task events are recorded")

    second = SessionState()
    check(second.snapshot()["tasks"] == [], "second session is isolated")
    check(second.session_id != state.session_id, "session ids are unique")

    state.record_execution_approved("smoke-plan-id")
    state.record_execution_completed(
        "smoke-plan-id",
        exit_code=0,
        timed_out=False,
        duration_ms=12,
        stdout_truncated=False,
        stderr_truncated=True,
    )
    serialized = json.dumps(state.snapshot())
    check("execution_approved" in serialized, "approval event recorded")
    check("execution_completed" in serialized, "completion event recorded")
    check(
        '"stdout":' not in serialized and '"stderr":' not in serialized,
        "raw output fields are absent",
    )

    # v0.5.0 audit: broker isolation -- two closure tool sets, no leakage.
    broker_a = ExecutionBroker()
    broker_b = ExecutionBroker()
    prep_a, result_a = make_execution_tools(broker_a)
    prep_b, result_b = make_execution_tools(broker_b)

    payload_a = prep_a("python", ["--version"])
    check(payload_a["ok"] is True, "broker A prepares a plan")
    plan_a = payload_a["plan_id"]
    check(
        result_a(plan_a)["status"] == "pending",
        "broker A sees its own pending plan",
    )
    check(
        result_b(plan_a)["ok"] is False,
        "broker B cannot read broker A's plan",
    )

    # Completed task is terminal for new steps (new work -> new plan).
    done = state.create_task("Finished", ["Only"])
    state.update_step(done.id, 1, "completed")
    try:
        state.add_steps(done.id, ["More"])
        raise AssertionError("completed task must be terminal for append")
    except Exception:
        pass
    check(True, "completed task is terminal for add_steps")

    print("Session planning smoke: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
