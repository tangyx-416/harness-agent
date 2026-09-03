"""PatchService apply tests (指令8 §64 edit + create cases)."""

from __future__ import annotations

import os
import stat as stat_module

import pytest

from harness_agent.patch import (
    OP_CREATE,
    OP_EDIT,
    PatchBroker,
    PatchBrokerError,
    apply_approved,
    apply_plan,
    prepare_patch,
)

from patch_test_helpers import make_edit_file, make_root, try_symlink


def make_plan(root, rel="hello.py", op=OP_EDIT, content=None, **kw):
    return prepare_patch(
        path=rel, operation=op, root=root, content=content,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}]
        if op == OP_EDIT
        else None,
        summaries=["c"],
    )


def approved_broker(root, rel="hello.py", op=OP_EDIT, content=None):
    broker = PatchBroker()
    plan = make_plan(root, rel=rel, op=op, content=content)
    broker.register(plan)
    broker.approve(plan.id)
    return broker, plan


# ---------------------------------------------------------------------------
# §64 edit
# ---------------------------------------------------------------------------


def test_64_01_approved_edit_applies(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "new"\n'
    assert broker.status(plan.id) == "applied"


def test_64_02_pending_cannot_apply(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    with pytest.raises(PatchBrokerError, match="not been approved"):
        apply_approved(broker, plan.id, root)


def test_64_03_rejected_cannot_apply(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root)
    broker = PatchBroker()
    plan = make_plan(root)
    broker.register(plan)
    broker.reject(plan.id)
    with pytest.raises(PatchBrokerError):
        apply_approved(broker, plan.id, root)


def test_64_04_content_hash_conflict(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    (root / "hello.py").write_text('x = "old"\ny = 2\n', encoding="utf-8")
    res = apply_approved(broker, plan.id, root)
    assert res.status == "conflict"
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "old"\ny = 2\n'


def test_64_05_path_revalidation_before_write(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, rel="real.py", text='x = "old"\n')
    broker, plan = approved_broker(root, rel="real.py")
    (root / "real.py").unlink()  # external change
    (root / "real.py").write_text("different\n", encoding="utf-8")
    res = apply_approved(broker, plan.id, root)
    assert res.status == "conflict"


def test_64_06_symlink_introduced_after_prepare_no_write(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, rel="real.py", text='x = "old"\n')
    broker, plan = approved_broker(root, rel="real.py")
    # swap real.py for a symlink after prepare
    link = root / "real.py"
    link.unlink()
    if not try_symlink(root / "hello.py", link):
        pytest.skip("symlinks not permitted on this platform")
    res = apply_approved(broker, plan.id, root)
    assert res.status == "conflict"


def test_64_07_original_bytes_unchanged_on_conflict(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    (root / "hello.py").write_bytes(b"external bytes\n")
    res = apply_approved(broker, plan.id, root)
    assert res.status == "conflict"
    assert (root / "hello.py").read_bytes() == b"external bytes\n"


def test_64_08_atomic_replace(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "new"\n'


def test_64_09_replace_failure_original_unchanged(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    original = (root / "hello.py").read_bytes()

    def boom(*_a, **_k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("harness_agent.patch.service.os.replace", boom)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "failed"
    assert (root / "hello.py").read_bytes() == original


def test_64_10_temp_cleaned(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    apply_approved(broker, plan.id, root)
    leftovers = [p.name for p in root.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_64_11_mode_preserved_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("mode preservation is POSIX-specific")
    root = make_root(tmp_path)
    target = root / "hello.py"
    target.write_text('x = "old"\n', encoding="utf-8")
    os.chmod(target, 0o755)
    before_mode = stat_module.S_IMODE(target.stat().st_mode)
    broker, plan = approved_broker(root)
    apply_approved(broker, plan.id, root)
    after_mode = stat_module.S_IMODE(target.stat().st_mode)
    assert before_mode == after_mode


def test_64_12_proposed_sha_matches_written_bytes(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker, plan = approved_broker(root)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    written = (root / "hello.py").read_bytes()
    import hashlib

    assert hashlib.sha256(written).hexdigest() == plan.proposed_sha256
    assert res.sha256 == plan.proposed_sha256


def test_64_13_bom_preserved(tmp_path):
    root = make_root(tmp_path)
    (root / "hello.py").write_bytes(b"\xef\xbb\xbf" + b'x = "old"\n')
    broker, plan = approved_broker(root)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    assert (root / "hello.py").read_bytes() == b"\xef\xbb\xbf" + b'x = "new"\n'


def test_64_14_crlf_preserved(tmp_path):
    root = make_root(tmp_path)
    (root / "hello.py").write_bytes(b'x = "old"\r\n')
    broker, plan = approved_broker(root)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    assert (root / "hello.py").read_bytes() == b'x = "new"\r\n'


# ---------------------------------------------------------------------------
# §64 create
# ---------------------------------------------------------------------------


def test_64_15_approved_create(tmp_path):
    root = make_root(tmp_path)
    broker, plan = approved_broker(root, op=OP_CREATE, content="hello\nworld\n")
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    assert (root / "hello.py").read_text(encoding="utf-8") == "hello\nworld\n"


def test_64_16_target_appears_after_proposal_conflict(tmp_path):
    root = make_root(tmp_path)
    broker, plan = approved_broker(root, op=OP_CREATE, content="hello\n")
    (root / "hello.py").write_text("external\n", encoding="utf-8")
    res = apply_approved(broker, plan.id, root)
    assert res.status == "conflict"
    assert (root / "hello.py").read_text(encoding="utf-8") == "external\n"


def test_64_17_never_overwrite_existing_target(tmp_path):
    root = make_root(tmp_path)
    broker, plan = approved_broker(root, op=OP_CREATE, content="new\n")
    (root / "hello.py").write_text("keep me\n", encoding="utf-8")
    res = apply_approved(broker, plan.id, root)
    assert res.status in ("conflict", "failed")
    assert (root / "hello.py").read_text(encoding="utf-8") == "keep me\n"


def test_64_18_failed_create_leaves_no_partial_target(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    broker, plan = approved_broker(root, op=OP_CREATE, content="hello\n")

    def boom(*_a, **_k):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr("harness_agent.patch.service.os.fsync", boom)
    res = apply_approved(broker, plan.id, root)
    assert res.status == "failed"
    assert not (root / "hello.py").exists()


def test_64_19_content_hash_correct(tmp_path):
    root = make_root(tmp_path)
    broker, plan = approved_broker(root, op=OP_CREATE, content="line one\n")
    import hashlib

    expected = hashlib.sha256(b"line one\n").hexdigest()
    res = apply_approved(broker, plan.id, root)
    assert res.status == "applied"
    assert plan.proposed_sha256 == expected
    assert res.sha256 == expected
