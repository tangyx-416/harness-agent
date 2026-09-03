"""Agent-visible patch tools, no-write guarantee, and static authority audits.

Covers 指令8 §71 (static write authority is confined to service.py) and §72
(no file deletion / rename API) plus the tool-layer behaviors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.patch import PatchBroker, apply_approved
from harness_agent.tools.patch_tools import (
    get_patch_result_core,
    make_patch_tools,
    prepare_patch_core,
)

from patch_test_helpers import make_edit_file, make_root

REPO_ROOT = Path(__file__).parent.parent.resolve()


# ---------------------------------------------------------------------------
# Tool behavior (explicit broker/root core)
# ---------------------------------------------------------------------------


def test_prepare_patch_registers_pending_and_returns_complete_diff(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker = PatchBroker()
    payload = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}],
        summary="rename value",
    )
    assert payload["ok"] is True
    assert payload["requires_approval"] is True
    assert payload["status"] == "pending"
    plan_id = payload["plan_id"]
    assert payload["path"] == "hello.py"
    assert payload["operation"] == "edit"
    assert payload["summaries"] == ["rename value"]
    assert payload["diff"].startswith("--- ")
    assert '"old"' in payload["diff"] and '"new"' in payload["diff"]
    assert broker.status(plan_id) == "pending"


def test_prepare_patch_does_not_write(tmp_path):
    root = make_root(tmp_path)
    path = make_edit_file(root, text='x = "old"\n')
    before = path.read_bytes()
    broker = PatchBroker()
    prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )
    assert path.read_bytes() == before


def test_prepare_create_does_not_create_file(tmp_path):
    root = make_root(tmp_path)
    broker = PatchBroker()
    payload = prepare_patch_core(
        path="created.txt", operation="create", broker=broker, root=root,
        content="hello\n", summary="c",
    )
    assert payload["ok"] is True
    assert not (root / "created.txt").exists()


def test_get_patch_result_pending(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker = PatchBroker()
    plan_id = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": "old", "new_text": "new"}], summary="c",
    )["plan_id"]
    result = get_patch_result_core(plan_id, broker=broker)
    assert result["ok"] is True
    assert result["applied"] is False
    assert result["status"] == "pending"


def test_get_patch_result_applied(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "old"\n')
    broker = PatchBroker()
    plan_id = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[{"old_text": '"old"', "new_text": '"new"'}], summary="c",
    )["plan_id"]
    broker.approve(plan_id)
    apply_approved(broker, plan_id, root)
    result = get_patch_result_core(plan_id, broker=broker)
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["status"] == "applied"


def test_get_patch_result_unknown(tmp_path):
    broker = PatchBroker()
    result = get_patch_result_core("nope", broker=broker)
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()


def test_tool_rejects_invalid_operation(tmp_path):
    root = make_root(tmp_path)
    broker = PatchBroker()
    payload = prepare_patch_core(
        path="hello.py", operation="delete", broker=broker, root=root,
        replacements=[{"old_text": "a", "new_text": "b"}], summary="c",
    )
    assert payload["ok"] is False
    assert payload["denied"] is True
    assert "Unsupported operation" in payload["error"]
    assert broker.pending() == []


def test_tool_rejects_overlapping_and_returns_denied(tmp_path):
    root = make_root(tmp_path)
    make_edit_file(root, text='x = "abcdef"\n')
    broker = PatchBroker()
    payload = prepare_patch_core(
        path="hello.py", operation="edit", broker=broker, root=root,
        replacements=[
            {"old_text": "abcdef", "new_text": "AXYZF"},
            {"old_text": "bcd", "new_text": "B"},
        ],
        summary="c",
    )
    assert payload["ok"] is False
    assert payload["denied"] is True
    assert broker.pending() == []


def test_make_patch_tools_rejects_non_broker():
    with pytest.raises(TypeError):
        make_patch_tools(object())  # type: ignore[arg-type]


def test_closure_tools_prepare_without_writing_real_repo():
    """The model-facing closure tools can prepare (never apply) code edits."""
    broker = PatchBroker()
    prep, result = make_patch_tools(broker)
    # Prepare an edit on a real tracked file but never approve/apply it; the
    # prepare operation must not modify anything.
    before = (REPO_ROOT / "src/harness_agent/__init__.py").read_bytes()
    payload = prep(
        path="src/harness_agent/__init__.py",
        operation="edit",
        replacements=[{"old_text": "__version__", "new_text": "__version__x0"}],
        summary="probe",
    )
    assert payload["ok"] is True
    after = (REPO_ROOT / "src/harness_agent/__init__.py").read_bytes()
    assert before == after  # no write
    # Cross-broker isolation through the get tool.
    other = PatchBroker()
    assert get_patch_result_core(payload["plan_id"], broker=other)["ok"] is False


# ---------------------------------------------------------------------------
# §71 Static write-authority audit
# ---------------------------------------------------------------------------


def _code_write_violations(path: Path) -> list[str]:
    """Return actual (non-docstring, non-comment) file-write call sites."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    risky_file_attrs = {"write_text", "write_bytes", "unlink"}
    found: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            # open(path, "w"...) style writes
            if isinstance(fn, ast.Name) and fn.id == "open":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if "w" in arg.value:
                            found.append(f"open(mode={arg.value!r})")
            # <obj>.write_text / .write_bytes / .unlink
            if isinstance(fn, ast.Attribute) and fn.attr in risky_file_attrs:
                found.append(fn.attr)
            # os.replace(...)
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "replace"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "os"
            ):
                found.append("os.replace")
    return found


def test_patch_tools_contains_no_write_primitives():
    violations = _code_write_violations(
        REPO_ROOT / "src/harness_agent/tools/patch_tools.py"
    )
    assert violations == [], f"patch_tools.py must not write files: {violations}"


def test_write_primitives_confined_to_service():
    tools_violations = _code_write_violations(
        REPO_ROOT / "src/harness_agent/tools/patch_tools.py"
    )
    assert tools_violations == []
    service_src = (REPO_ROOT / "src/harness_agent/patch/service.py").read_text(
        encoding="utf-8"
    )
    assert "os.replace" in service_src
    assert "unlink" in service_src
    assert "mkstemp" in service_src


# ---------------------------------------------------------------------------
# §72 No delete / rename audit
# ---------------------------------------------------------------------------


def test_no_file_deletion_or_rename_api():
    pkg_dir = REPO_ROOT / "src/harness_agent/patch"
    files = [pkg_dir / "models.py", pkg_dir / "policy.py", pkg_dir / "__init__.py"]
    text = "\n".join(f.read_text(encoding="utf-8") for f in files if f.exists())
    for forbidden_tool in (
        "delete_file",
        "remove_file",
        "rename_file",
        "move_file",
        "unlink_file",
    ):
        assert forbidden_tool not in text
    # The reject path for an unsupported operation, including "delete", exists.
    from harness_agent.patch import PatchPolicyError, prepare_patch

    with pytest.raises(PatchPolicyError):
        prepare_patch(
            path="a.py", operation="delete", root=REPO_ROOT,
            replacements=[{"old_text": "a", "new_text": "b"}], summaries=["c"],
        )
