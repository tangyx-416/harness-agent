"""CLI host-side patch approval tests (指令8 §66)."""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import run_agent  # noqa: E402

from harness_agent.patch import PatchBroker  # noqa: E402
from harness_agent.session import SessionState  # noqa: E402
from harness_agent.tools.patch_tools import prepare_patch_core  # noqa: E402

from patch_test_helpers import make_edit_file, make_root

APPROVE_WORDS = ["y", "yes", "Y", "YES"]
REJECT_WORDS = ["", " ", "n", "no", "garbage", "maybe", "nope"]


def prepared_patch(root, broker, text='x = "old"\n'):
    make_edit_file(root, text=text)
    payload = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )
    return payload["plan_id"]


def test_patch_accept_header_and_flow(tmp_path):
    root = make_root(tmp_path)
    broker = PatchBroker()
    state = SessionState()
    plan_id = prepared_patch(root, broker)
    with patch("builtins.input", return_value="yes"), patch(
        "sys.stdout", new_callable=StringIO
    ):
        run_agent.process_pending_patches(broker, session_state=state, root=root)
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "new"\n'
    assert broker.status(plan_id) == "applied"


def test_patch_approve_words_apply_exactly_once(tmp_path):
    root = make_root(tmp_path)
    for word in APPROVE_WORDS:
        broker = PatchBroker()
        state = SessionState()
        plan_id = prepared_patch(root, broker)
        calls = {"n": 0}

        def fake_apply(b, pid, r):
            calls["n"] += 1
            from harness_agent.patch.service import apply_approved as real

            return real(b, pid, r)

        with patch("builtins.input", return_value=word), patch(
            "run_agent.apply_approved", side_effect=fake_apply
        ), patch("sys.stdout", new_callable=StringIO):
            run_agent.process_pending_patches(broker, session_state=state, root=root)
        assert calls["n"] == 1, f"{word!r} must apply exactly once"
        assert broker.status(plan_id) == "applied"
        (root / "hello.py").write_text('x = "old"\n', encoding="utf-8")


def test_patch_reject_words_zero_write(tmp_path):
    root = make_root(tmp_path)
    for word in REJECT_WORDS:
        broker = PatchBroker()
        state = SessionState()
        plan_id = prepared_patch(root, broker)

        def refuse_apply(*_a, **_k):
            raise AssertionError("rejected patch reached apply service")

        with patch("builtins.input", return_value=word), patch(
            "run_agent.apply_approved", side_effect=refuse_apply
        ), patch("sys.stdout", new_callable=StringIO):
            run_agent.process_pending_patches(broker, session_state=state, root=root)
        assert broker.status(plan_id) == "rejected"
        assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "old"\n'


def test_patch_eof_rejects_zero_write(tmp_path):
    root = make_root(tmp_path)
    broker = PatchBroker()
    state = SessionState()
    plan_id = prepared_patch(root, broker)

    def eof(*_a, **_k):
        raise EOFError

    with patch("builtins.input", side_effect=eof), patch(
        "run_agent.apply_approved", side_effect=lambda *a: (_ for _ in ()).throw(
            AssertionError("EOF must not apply")
        )
    ), patch("sys.stdout", new_callable=StringIO):
        run_agent.process_pending_patches(broker, session_state=state, root=root)
    assert broker.status(plan_id) == "rejected"
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "old"\n'


def test_patch_ctrl_c_rejects_zero_write(tmp_path):
    root = make_root(tmp_path)
    broker = PatchBroker()
    state = SessionState()
    plan_id = prepared_patch(root, broker)

    def ctrl_c(*_a, **_k):
        raise KeyboardInterrupt

    with patch("builtins.input", side_effect=ctrl_c), patch(
        "run_agent.apply_approved", side_effect=lambda *a: (_ for _ in ()).throw(
            AssertionError("Ctrl+C must not apply")
        )
    ), patch("sys.stdout", new_callable=StringIO):
        run_agent.process_pending_patches(broker, session_state=state, root=root)
    assert broker.status(plan_id) == "rejected"
    assert (root / "hello.py").read_text(encoding="utf-8") == 'x = "old"\n'


def test_patch_show_displays_complete_diff(tmp_path):
    root = make_root(tmp_path)
    broker = PatchBroker()
    prepared_patch(root, broker)
    output = StringIO()
    with patch("builtins.input", return_value="n"), patch(
        "sys.stdout", new_callable=lambda: output
    ):
        run_agent.process_pending_patches(broker, root=root)
    text = output.getvalue()
    assert "Complete diff:" in text
    assert "hello.py" in text
    assert '"old"' in text and '"new"' in text
