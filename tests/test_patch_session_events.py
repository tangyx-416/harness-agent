"""Patch session events (指令8 §68) + secret-data boundary.

Host-side patch approvals/apply results must be recorded as METADATA ONLY in
the session. The actual code content (old_text / new_text / content / diff)
lives only in the mutable Plan, never in the durable session events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_agent.patch import PatchBroker, apply_approved
from harness_agent.session import SessionState, SessionStateError
from harness_agent.tools.patch_tools import prepare_patch_core

from patch_test_helpers import make_edit_file, make_root

SECRET_OLD = 'value = "s3cr3t_old"\n'
SECRET_NEW = 'value = "s3cr3t_nex7w"\n'


def make_events(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text=SECRET_OLD)
    broker = PatchBroker()
    plan_id = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": SECRET_OLD.strip(), "new_text": SECRET_NEW.strip()}],
        summary="rotate secret",
    )["plan_id"]
    state = SessionState()
    return root, broker, plan_id, state


@pytest.mark.parametrize(
    "method,event_type,summary_prefix",
    [
        ("record_patch_approved", "patch_approved", "Source edit approved"),
        ("record_patch_rejected", "patch_rejected", "Source edit rejected"),
        ("record_patch_applied", "patch_applied", "Source edit applied"),
        ("record_patch_conflict", "patch_conflict", "Source edit apply conflicted"),
        ("record_patch_failed", "patch_failed", "Source edit apply failed"),
    ],
)
def test_record_patch_events_metadata_only(
    tmp_path, method, event_type, summary_prefix
):
    _root, _broker, plan_id, state = make_events(tmp_path)
    event = getattr(state, method)(plan_id)
    assert event.type == event_type
    assert event.summary.startswith(summary_prefix)
    assert event.reference == plan_id
    assert event.task_id is None and event.step_id is None
    snapshot = state.snapshot()
    for ev in snapshot["recent_events"]:
        assert ev["type"] == event_type
        # Secret-data boundary: no code content enters the durable session.
        blob = json.dumps(snapshot)
        assert SECRET_OLD.strip() not in blob
        assert SECRET_NEW.strip() not in blob
        assert "s3cr3t" not in blob


def test_patch_events_append_sequentially(tmp_path):
    _root, _broker, plan_id, state = make_events(tmp_path)
    e1 = state.record_patch_approved(plan_id)
    e2 = state.record_patch_applied(plan_id)
    assert e2.seq == e1.seq + 1
    types = [ev["type"] for ev in state.snapshot()["recent_events"]]
    assert types == ["patch_approved", "patch_applied"]


def test_patch_event_rejects_invalid_plan_id(tmp_path):
    _root, _broker, _plan_id, state = make_events(tmp_path)
    with pytest.raises(SessionStateError):
        state.record_patch_approved("")

def test_full_lifecycle_session_tracked(
    tmp_path,
):
    root = make_root(tmp_path)
    make_edit_file(root, text=SECRET_OLD)
    broker = PatchBroker()
    plan_id = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": SECRET_OLD.strip(), "new_text": SECRET_NEW.strip()}],
        summary="rotate secret",
    )["plan_id"]
    state = SessionState()
    state.record_patch_approved(plan_id)
    broker.approve(plan_id)
    apply_approved(broker, plan_id, root)
    state.record_patch_applied(plan_id)
    types = [ev["type"] for ev in state.snapshot()["recent_events"]]
    assert types == ["patch_approved", "patch_applied"]
    assert (root / "hello.py").read_text(encoding="utf-8") == SECRET_NEW
    blob = json.dumps(state.snapshot())
    assert SECRET_NEW.strip() not in blob
