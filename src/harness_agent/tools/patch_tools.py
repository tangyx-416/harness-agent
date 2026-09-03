"""Agent-visible patch proposal tools (v0.6.0).

Exposes exactly two tools to the model:

* ``prepare_patch``      -- validate a source-edit/create request into an
  immutable PatchPlan and register it as ``pending``. NEVER writes.
* ``get_patch_result``   -- read-only lookup of a patch's lifecycle status or
  its host-side apply result. NEVER writes.

:func:`make_patch_tools` produces closure-bound tool instances that share a
single :class:`PatchBroker`, isolating patch state per agent session. These
tools call only the validating policy and the broker's ``register`` path:
they perform no ``open(...,"w")``, ``write_text``, ``write_bytes``,
``os.replace`` or ``unlink``.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from ..patch import PatchBroker, PatchPolicyError, prepare_patch as _policy_prepare
from ..patch.models import OP_CREATE, OP_EDIT
from .path_utils import find_repo_root
def _prepare_patch_core(
    broker: PatchBroker,
    *,
    path: str,
    operation: str,
    replacements: list[dict[str, str]] | tuple[dict[str, str], ...] | None,
    content: str | None,
    summary: str,
    summaries: list[str] | tuple[str, ...] | None,
    root,
) -> dict[str, Any]:
    """Validate + register one pending PatchPlan through an explicit broker."""
    if operation not in (OP_EDIT, OP_CREATE):
        return {
            "ok": False,
            "denied": True,
            "error": f"Unsupported operation ({operation!r}); only 'edit' and 'create'.",
        }

    if summaries is None:
        summaries = [summary]
    if isinstance(summaries, str):
        summaries = [summaries]

    try:
        plan = _policy_prepare(
            path=path,
            operation=operation,
            root=root,
            replacements=replacements if operation == OP_EDIT else None,
            content=content if operation == OP_CREATE else None,
            summaries=list(summaries),
        )
    except PatchPolicyError as exc:
        return {"ok": False, "denied": True, "error": str(exc)}

    try:
        broker.register(plan)
    except Exception as exc:
        return {"ok": False, "denied": False, "error": str(exc)}

    return {
        "ok": True,
        "requires_approval": True,
        "status": "pending",
        "plan_id": plan.id,
        "path": plan.repo_path,
        "operation": plan.operation,
        "summaries": list(plan.summaries),
        "diff": plan.diff,
        "message": (
            "Patch proposal prepared but NOT applied. The host will show the "
            "complete diff and ask the user to approve it; only after approval "
            "will any file be written. Never claim a file was edited unless you "
            "later retrieve a PatchResult with status 'applied' for this plan_id."
        ),
    }


def _get_patch_result_core(
    broker: PatchBroker, plan_id: str
) -> dict[str, Any]:
    """Read-only lookup (never writes or applies)."""
    plan = broker.get_plan(plan_id)
    if plan is None:
        return {"ok": False, "error": f"Unknown patch plan id: {plan_id}"}

    result = broker.get_result(plan_id)
    if result is None:
        status = broker.status(plan_id)
        return {
            "ok": True,
            "plan_id": plan_id,
            "applied": False,
            "status": status,
            "path": plan.repo_path,
            "operation": plan.operation,
            "message": (
                f"Patch plan status: {status}. No apply result exists yet -- "
                "do not claim the file was edited."
            ),
        }

    payload = result.to_dict()
    payload.update({"ok": True, "applied": result.status == "applied"})
    return payload


def make_patch_tools(broker: PatchBroker) -> tuple[Any, Any]:
    """Create exactly two Strands tools sharing ``broker``."""
    if not isinstance(broker, PatchBroker):
        raise TypeError("broker must be a PatchBroker instance")

    @tool(name="prepare_patch")
    def prepare_patch_tool(
        path: str,
        operation: str,
        replacements: list[dict[str, str]] | None = None,
        content: str | None = None,
        summary: str = "",
        summaries: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Propose a precise, previewable, single-file source edit or creation.

        This tool NEVER writes. It validates your request into an immutable
        patch plan and returns the COMPLETE unified diff for the host to show
        the user. Only after the user approves and the host applies it is any
        file touched.

        For an 'edit', ``replacements`` is a list of {'old_text': ..., 'new_text': ...}
        where every old_text matches the current file exactly once (uniquely).
        Use the smallest necessary patch (prefer a few line edits over a whole
        file rewrite). For a 'create', give the new UTF-8 ``content``; the
        target must not exist yet and its parent directory must already exist.

        Args:
            path: Repository-relative path to the single file (e.g. 'src/x.py').
            operation: 'edit' to modify an existing file, or 'create' for a new one.
            replacements: For 'edit', ordered list of exact {old_text, new_text}.
            content: For 'create', the full UTF-8 content of the new file.
            summary: One concise, user-visible change summary.
            summaries: Optional plural list of concise summaries.

        Returns:
            {'ok': True, 'requires_approval': True, 'plan_id', 'path',
            'operation', 'diff', ...} when prepared, or
            {'ok': False, 'denied': True, 'error'} when the policy refuses.
        """
        return _prepare_patch_core(
            broker,
            path=path,
            operation=operation,
            replacements=replacements,
            content=content,
            summary=summary,
            summaries=summaries,
            root=find_repo_root(),
        )

    @tool(name="get_patch_result")
    def get_patch_result_tool(plan_id: str) -> dict[str, Any]:
        """Read the stored status/result of a previously prepared patch plan.

        Purely read-only: this tool can never write a file. Use it to check
        whether a plan is still pending or to fetch the 'applied'/'conflict'/
        'failed' result after the user approved and the host applied it.

        Args:
            plan_id: Identifier returned by prepare_patch.

        Returns:
            {'ok': True, 'applied': True, 'status': 'applied', ...} when a
            result exists, {'ok': True, 'applied': False, 'status', ...} for
            plans without results, or {'ok': False, 'error'} for unknown ids.
        """
        return _get_patch_result_core(broker, plan_id)

    return (prepare_patch_tool, get_patch_result_tool)


def prepare_patch_core(
    *,
    path: str,
    operation: str,
    broker: PatchBroker | None = None,
    root=None,
    replacements: list[dict[str, str]] | None = None,
    content: str | None = None,
    summary: str = "",
    summaries: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Run the tool's prepare logic against an explicit broker/root (tests)."""
    broker = broker or PatchBroker()
    return _prepare_patch_core(
        broker,
        path=path,
        operation=operation,
        replacements=replacements,
        content=content,
        summary=summary,
        summaries=summaries,
        root=root if root is not None else find_repo_root(),
    )


def get_patch_result_core(
    plan_id: str, broker: PatchBroker | None = None
) -> dict[str, Any]:
    """Run the tool's result lookup against an explicit broker (tests)."""
    broker = broker or PatchBroker()
    return _get_patch_result_core(broker, plan_id)
