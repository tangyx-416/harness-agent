"""Agent-visible execution request tools (v0.3.0).

Exposes exactly two tools to the model:

* ``prepare_command``      -- normalize + validate + register a pending
  ExecutionPlan. NEVER runs anything.
* ``get_execution_result`` -- read-only lookup of a stored result (or the
  plan's lifecycle status). NEVER runs anything.

The model can REQUEST execution; it can never EXECUTE it. Approval and
subprocess spawning live in the trusted host layer (see
``harness_agent.execution`` and the CLI).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from strands import tool

from ..execution import ExecutionBroker, evaluate_command, get_default_broker
from .path_utils import find_repo_root


def _prepare_command(
    broker: ExecutionBroker,
    program: str,
    args: list[str] | tuple[str, ...] | None,
    cwd: str,
    timeout_seconds: int | None,
    root: Path,
) -> dict[str, Any]:
    """Core prepare logic (testable with an explicit broker/root)."""
    decision = evaluate_command(
        program,
        args=args,
        cwd=cwd,
        root=root,
        timeout_seconds=timeout_seconds,
    )

    if not decision.allowed:
        return {
            "ok": False,
            "denied": True,
            "error": decision.reason,
            "hint": (
                "Only commands in the v0.3.0 allowlist can be prepared: "
                "'python --version', 'python -m pytest [flags] [paths]' "
                "and 'python -m ruff check [paths]'."
            ),
        }

    plan_id = uuid.uuid4().hex
    from ..execution import ExecutionPlan

    plan = ExecutionPlan(
        id=plan_id,
        program=decision.argv[0],
        args=tuple(decision.argv[1:]),
        cwd=decision.cwd,
        display_command=decision.display_command,
        risk_level=decision.risk_level,
        risk_reason=decision.risk_reason,
        timeout_seconds=decision.timeout_seconds,
        created_at=time.time(),
    )
    broker.register(plan)

    return {
        "ok": True,
        "requires_approval": True,
        "status": "prepared",
        "plan_id": plan.id,
        "command": plan.display_command,
        "cwd": plan.cwd,
        "risk": plan.risk_level,
        "risk_reason": plan.risk_reason,
        "timeout_seconds": plan.timeout_seconds,
        "message": (
            "Execution plan prepared. The command has NOT run. The host will "
            "ask the user to approve it; only after approval will it execute. "
            "Never claim a command ran or tests passed unless you later "
            "retrieve an ExecutionResult for this plan_id."
        ),
    }


@tool(name="prepare_command")
def prepare_command(
    program: str,
    args: list[str] | None = None,
    cwd: str = ".",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Request approval to run a whitelisted development command.

    Prepares a pending execution plan for the host to show to the user.
    This tool NEVER executes anything itself. Allowed programs are
    strictly limited: 'python' (with '--version', '-m pytest ...' or
    '-m ruff check ...'), or the aliases 'pytest' / 'ruff'. Shell
    syntax, package installation, git and arbitrary scripts are denied.

    Args:
        program: Program name, e.g. 'python', 'pytest' or 'ruff'.
        args: Argument tokens, e.g. ['-m', 'pytest', '-q', 'tests'].
        cwd: Working directory relative to the repository root (default '.').
        timeout_seconds: Optional timeout, clamped to 1-60 seconds.

    Returns:
        {'ok': True, 'requires_approval': True, 'plan_id', 'command',
        'cwd', 'risk', 'risk_reason', ...} when prepared, or
        {'ok': False, 'denied': True, 'error'} when the policy refuses.
    """
    return _prepare_command(
        get_default_broker(),
        program,
        args,
        cwd,
        timeout_seconds,
        find_repo_root(),
    )


def _get_execution_result(broker: ExecutionBroker, plan_id: str) -> dict[str, Any]:
    """Core result lookup (read-only; never executes)."""
    plan = broker.get_plan(plan_id)
    if plan is None:
        return {"ok": False, "error": f"Unknown plan id: {plan_id}"}

    result = broker.get_result(plan_id)
    if result is None:
        status = broker.status(plan_id)
        return {
            "ok": True,
            "executed": False,
            "plan_id": plan_id,
            "status": status,
            "command": plan.display_command,
            "message": (
                f"Plan status: {status}. No execution result exists yet -- "
                "do not claim the command ran."
            ),
        }

    payload = result.to_dict()
    payload.update({"ok": True, "executed": True, "status": "executed"})
    return payload


@tool(name="get_execution_result")
def get_execution_result(plan_id: str) -> dict[str, Any]:
    """Read the stored result of a previously prepared execution plan.

    Purely read-only: this tool can never execute a command. Use it to
    fetch the exit code and output of a plan after the user approved and
    the host executed it, or to check whether a plan is still pending.

    Args:
        plan_id: Identifier returned by prepare_command.

    Returns:
        {'ok': True, 'executed': True, 'exit_code', 'stdout', ...} when a
        result exists, {'ok': True, 'executed': False, 'status', ...} for
        plans without results, or {'ok': False, 'error'} for unknown ids.
    """
    return _get_execution_result(get_default_broker(), plan_id)


def prepare_command_core(
    program: str,
    args: list[str] | tuple[str, ...] | None = None,
    cwd: str = ".",
    timeout_seconds: int | None = None,
    broker: ExecutionBroker | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run :func:`prepare_command` against an explicit broker/root (tests)."""
    from ..execution import get_default_broker as _gdb
    from .path_utils import find_repo_root as _frr

    return _prepare_command(
        broker if broker is not None else _gdb(),
        program,
        args,
        cwd,
        timeout_seconds,
        root if root is not None else _frr(),
    )


def get_execution_result_core(
    plan_id: str,
    broker: ExecutionBroker | None = None,
) -> dict[str, Any]:
    """Run :func:`get_execution_result` against an explicit broker (tests)."""
    from ..execution import get_default_broker as _gdb

    return _get_execution_result(
        broker if broker is not None else _gdb(), plan_id
    )
