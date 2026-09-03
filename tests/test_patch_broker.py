"""PatchBroker lifecycle tests (指令8 §63)."""

from __future__ import annotations

import pytest

from harness_agent.patch import PatchBroker, PatchBrokerError, prepare_patch

from patch_test_helpers import make_edit_file, make_root


def make_plan(root, name="hello.py"):
    return prepare_patch(
        path=name, operation="edit", root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}],
        summaries=["c"],
    )


def test_63_01_pending_created(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    assert broker.status(plan.id) == "pending"
    assert [p.id for p in broker.pending()] == [plan.id]


def test_63_02_pending_to_approved(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    assert broker.approve(plan.id).id == plan.id
    assert broker.status(plan.id) == "approved"
    assert broker.pending() == []


def test_63_03_pending_to_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.reject(plan.id)
    assert broker.status(plan.id) == "rejected"
    assert broker.pending() == []


def test_63_04_approved_to_applied(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    res = broker.record_application(plan.id, status="applied", sha256="abc", message="ok")
    assert broker.status(plan.id) == "applied"
    assert broker.get_result(plan.id) is res
    assert res.status == "applied"
    assert res.sha256 == "abc"


def test_63_05_approved_to_conflict(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    res = broker.record_application(plan.id, status="conflict", sha256=None, message="c")
    assert broker.status(plan.id) == "conflict"
    assert res.status == "conflict"


def test_63_06_approved_to_failed(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    res = broker.record_application(plan.id, status="failed", sha256=None, message="f")
    assert broker.status(plan.id) == "failed"
    assert res.status == "failed"


def test_63_07_rejected_terminal(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.reject(plan.id)
    with pytest.raises(PatchBrokerError):
        broker.approve(plan.id)
    with pytest.raises(PatchBrokerError):
        broker.take_for_apply(plan.id)


def test_63_08_applied_terminal(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    broker.record_application(plan.id, status="applied", sha256="x", message="ok")
    with pytest.raises(PatchBrokerError):
        broker.take_for_apply(plan.id)


def test_63_09_conflict_terminal(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    broker.record_application(plan.id, status="conflict", sha256=None, message="c")
    with pytest.raises(PatchBrokerError):
        broker.take_for_apply(plan.id)


def test_63_10_failed_terminal(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    broker.record_application(plan.id, status="failed", sha256=None, message="f")
    with pytest.raises(PatchBrokerError):
        broker.take_for_apply(plan.id)


def test_63_11_unknown_plan(tmp_path):
    broker = PatchBroker()
    assert broker.get_plan("nope") is None
    assert broker.status("nope") is None
    assert broker.get_result("nope") is None
    with pytest.raises(PatchBrokerError):
        broker.approve("nope")
    with pytest.raises(PatchBrokerError):
        broker.reject("nope")
    with pytest.raises(PatchBrokerError):
        broker.take_for_apply("nope")


def test_63_12_double_approval_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    with pytest.raises(PatchBrokerError):
        broker.approve(plan.id)


def test_63_13_double_apply_rejected(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    with pytest.raises(PatchBrokerError):
        broker.take_for_apply(plan.id)


def test_63_14_plan_immutable(tmp_path):
    from dataclasses import FrozenInstanceError

    root = make_root(tmp_path)
    make_edit_file(root)
    plan = make_plan(root)
    with pytest.raises(FrozenInstanceError):
        plan.summaries = ("changed",)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.proposed_content = "changed"  # type: ignore[misc]

    # The registered plan object is the same immutable instance.
    broker = PatchBroker()
    broker.register(plan)
    assert broker.get_plan(plan.id) is plan


def test_63_15_plan_limit(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker(max_plans=2)
    broker.register(make_plan(root))
    broker.register(make_plan(root))
    with pytest.raises(PatchBrokerError, match="limit"):
        broker.register(make_plan(root))
    assert len(broker.pending()) == 2


def test_63_16_result_structured(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.approve(plan.id)
    broker.take_for_apply(plan.id)
    res = broker.record_application(plan.id, status="applied", sha256="sha", message="ok")
    d = res.to_dict()
    assert d["plan_id"] == plan.id
    assert d["status"] == "applied"
    assert d["repo_path"] == "hello.py"
    assert d["operation"] == "edit"
    assert d["sha256"] == "sha"
