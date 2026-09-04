#!/usr/bin/env python
"""Host-level smoke tests for v0.7.0 User-Approved Source Editing.

Runs WITHOUT any LLM API access. It exercises the exact production path:
policy validates -> pending immutable PatchPlan -> COMPLETE diff -> host asks
the user -> user approves -> host applies -> result. It uses a disposable
temporary repo so real file writes never touch the harness repository.

Checks:
  A. edit prepare      -> pending, COMPLETE diff, NO write, file bytes unchanged
  B. create prepare    -> no file created until host apply
  C. approve "yes"     -> host applies exactly once, file updated
  D. reject            -> ZERO write, status rejected
  E. conflict          -> file changed after prepare -> conflict, no write
  F. policy denials    -> unsupported op / overlapping / unsafe path / .env
  G. tool surface      -> 20 tools include prepare_patch + get_patch_result
"""

import io
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "src"))

import run_agent  # noqa: E402

from harness_agent.patch import PatchBroker, apply_approved  # noqa: E402
from harness_agent.session import SessionState  # noqa: E402
from harness_agent.tools.patch_tools import (  # noqa: E402
    get_patch_result_core,
    prepare_patch_core,
)

REPO_ROOT = _SCRIPTS.parent.resolve()

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def make_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="smoke_patch_"))
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return root


def main() -> int:
    print("=" * 64)
    print("Harness Agent v0.6.0 - Source Editing Smoke (host-level, no LLM)")
    print("=" * 64)

    # -- A. edit prepare -------------------------------------------------------
    print("\n[A] edit proposal -> pending, COMPLETE diff, NO write")
    root = make_root()
    target = root / "hello.py"
    ORIG = 'x = "old"\n'
    target.write_bytes(ORIG.encode("utf-8"))
    broker = PatchBroker()
    payload = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="rename value",
    )
    pid = payload["plan_id"]
    check("A1 plan created pending", payload.get("ok") is True
          and payload.get("requires_approval") is True
          and payload.get("status") == "pending")
    check("A2 complete diff present", payload.get("diff", "").startswith("--- ")
          and '"old"' in payload["diff"] and '"new"' in payload["diff"])
    check("A3 no write yet", target.read_bytes() == ORIG.encode("utf-8"))
    check("A4 nothing applied yet", broker.get_result(pid) is None)

    # -- B. create prepare -----------------------------------------------------
    print("\n[B] create proposal -> no file until host apply")
    broker2 = PatchBroker()
    cpayload = prepare_patch_core(
        path="created.txt", operation="create", broker=broker2, root=root,
        content="hello\n", summary="new file",
    )
    cpid = cpayload["plan_id"]
    check("B1 create pending", cpayload.get("ok") is True
          and cpayload.get("status") == "pending")
    check("B2 file NOT created yet", not (root / "created.txt").exists())

    # -- C. approve ------------------------------------------------------------
    print("\n[C] user says yes -> host applies exactly once")
    state = SessionState()
    calls = {"n": 0}

    def counting_apply(b, bid, r):
        calls["n"] += 1
        return apply_approved(b, bid, r)

    with mock.patch("builtins.input", return_value="yes"), mock.patch(
        "run_agent.apply_approved", side_effect=counting_apply
    ), mock.patch("sys.stdout", new_callable=io.StringIO):
        run_agent.process_pending_patches(broker, session_state=state, root=root)
    check("C1 applied once", calls["n"] == 1)
    check("C2 status applied", broker.status(pid) == "applied")
    check("C3 file updated", target.read_text(encoding="utf-8") == 'x = "new"\n')
    check("C4 get_patch_result applied", get_patch_result_core(pid, broker=broker).get("applied") is True)
    check("C5 session records patch events",
          any(ev["type"] in ("patch_approved", "patch_applied")
              for ev in state.snapshot()["recent_events"]))

    # -- D. reject ------------------------------------------------------------
    print("\n[D] user says no -> ZERO write, status rejected")
    root2 = make_root()
    target2 = root2 / "hello.py"
    target2.write_bytes(ORIG.encode("utf-8"))
    broker3 = PatchBroker()
    pid3 = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker3, root=root2,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )["plan_id"]

    def refuse_apply(*_a, **_k):
        raise AssertionError("rejected plan reached apply service")

    with mock.patch("builtins.input", return_value="n"), mock.patch(
        "run_agent.apply_approved", side_effect=refuse_apply
    ), mock.patch("sys.stdout", new_callable=io.StringIO):
        run_agent.process_pending_patches(broker3, session_state=SessionState(), root=root2)
    check("D1 rejected", broker3.status(pid3) == "rejected")
    check("D2 zero write", target2.read_bytes() == ORIG.encode("utf-8"))
    check("D3 no result", broker3.get_result(pid3) is None)

    # -- E. conflict -----------------------------------------------------------
    print("\n[E] file changed after prepare -> conflict, no write")
    root3 = make_root()
    target3 = root3 / "hello.py"
    target3.write_bytes(ORIG.encode("utf-8"))
    broker4 = PatchBroker()
    pid4 = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker4, root=root3,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )["plan_id"]
    target3.write_bytes('x = "CHANGED"\n'.encode("utf-8"))
    broker4.approve(pid4)
    result = apply_approved(broker4, pid4, root3)
    check("E1 conflict detected", result.status == "conflict")
    check("E2 no write", target3.read_text(encoding="utf-8") == 'x = "CHANGED"\n')

    # -- F. policy denials -----------------------------------------------------
    print("\n[F] policy denials")
    broker5 = PatchBroker()
    d1 = prepare_patch_core(path="hello.py", operation="delete", broker=broker5, root=root,
                            replacements=[{"old_text": "a", "new_text": "b"}], summary="c")
    check("F1 delete denied", d1.get("denied") is True)
    d2 = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker5, root=root,
        replacements=[
            {"old_text": "abcdef", "new_text": "AXYZF"},
            {"old_text": "bcd", "new_text": "B"},
        ], summary="c",
    )
    check("F2 overlapping denied", d2.get("denied") is True)
    d3 = prepare_patch_core(path="../escape.py", operation="create", broker=broker5, root=root,
                            content="x\n", summary="c")
    check("F3 path escape denied", d3.get("denied") is True)
    d4 = prepare_patch_core(path=".env", operation="create", broker=broker5, root=root,
                            content="SECRET=1\n", summary="c")
    check("F4 .env denied", d4.get("denied") is True)
    check("F5 no pending after denials", broker5.pending() == [])

    # -- G. tool surface -------------------------------------------------------
    print("\n[G] agent tool surface includes patch tools")
    from harness_agent.config import AgentConfig
    from unittest import mock as _m

    cfg = AgentConfig(api_key="smoke-key", model_id="gpt-4", base_url=None)
    with _m.patch("harness_agent.agent.OpenAIModel"), _m.patch(
        "harness_agent.agent.Agent"
    ) as mac:
        mac.return_value = _m.Mock()
        from harness_agent.agent import create_agent
        create_agent(cfg, session_state=SessionState())
    names = [
        getattr(t, "tool_name", getattr(t, "__name__", ""))
        for t in mac.call_args.kwargs["tools"]
    ]
    check("G1 22 tools", len(names) == 22)
    check("G2 patch tools present",
          "prepare_patch" in names and "get_patch_result" in names)
    check("G3 host ops hidden",
          not ({"apply_patch", "approve_patch", "approve", "execute"} & set(names)))

    # -- cleanup ---------------------------------------------------------------
    for r in (root, root2, root3):
        shutil.rmtree(r, ignore_errors=True)

    print("\n" + "=" * 64)
    print(f"Passed: {len(PASSED)}   Failed: {len(FAILED)}")
    if FAILED:
        print("Failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("Source editing smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
