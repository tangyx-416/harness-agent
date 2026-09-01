"""Data models for the User-Approved Safe Execution subsystem (v0.3.0).

All models are immutable ``frozen`` dataclasses:

* :class:`PolicyDecision` -- outcome of validating an execution request.
* :class:`ExecutionPlan`  -- the exact, policy-validated command that a user
  may approve. Once created it can never be mutated, so what the user sees
  and approves is byte-for-byte what the host executes.
* :class:`ExecutionResult` -- what actually happened after host execution.

Sequence numbers such as ``args`` are stored as tuples so plans stay
hashable and truly immutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy import DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class PolicyDecision:
    """Result of evaluating a command request against the execution policy."""

    allowed: bool
    reason: str
    argv: tuple[str, ...] = ()
    cwd: str = ""
    display_command: str = ""
    risk_level: str = ""
    risk_reason: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ExecutionPlan:
    """A validated, pending command awaiting user approval.

    Attributes:
        id: Unique plan identifier (the single handle for approve/execute).
        program: Absolute interpreter path (always ``sys.executable``).
        args: Validated argument tokens (immutable tuple).
        cwd: Validated absolute working directory inside the repository.
        display_command: Human-readable command shown for approval.
        risk_level: Informational risk label (LOW/MEDIUM/HIGH).
        risk_reason: Why the risk level was assigned.
        timeout_seconds: Clamped execution timeout (1-60s).
        created_at: Creation timestamp (``time.time()``).
    """

    id: str
    program: str
    args: tuple[str, ...]
    cwd: str
    display_command: str
    risk_level: str
    risk_reason: str
    timeout_seconds: int
    created_at: float


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one host-side execution of an :class:`ExecutionPlan`."""

    plan_id: str
    command: str
    cwd: str
    risk_level: str
    approved: bool
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict (used by tools and the CLI)."""
        return {
            "plan_id": self.plan_id,
            "command": self.command,
            "cwd": self.cwd,
            "risk_level": self.risk_level,
            "approved": self.approved,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
        }
