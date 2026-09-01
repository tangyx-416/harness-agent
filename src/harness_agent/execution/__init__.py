"""User-Approved Safe Execution subsystem (v0.3.0).

Security model::

    LLM proposes -> Policy validates -> User approves -> Host executes

* :mod:`.policy`  -- strict allowlist; normalizes and validates requests.
* :mod:`.models`  -- immutable ExecutionPlan / ExecutionResult / PolicyDecision.
* :mod:`.broker`  -- in-memory pending plans; single-use lifecycle.
* :mod:`.service` -- the only place a subprocess is ever spawned.

The agent can only *prepare* execution plans; approval and execution
live in the trusted host layer (CLI).
"""

from __future__ import annotations

from .broker import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    BrokerError,
    ExecutionBroker,
)
from .models import ExecutionPlan, ExecutionResult, PolicyDecision
from .policy import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_SECONDS,
    clamp_timeout,
    evaluate_command,
)
from .service import (
    SENSITIVE_ENV_KEYS_EXACT,
    SENSITIVE_ENV_KEY_SUFFIXES,
    build_child_environment,
    execute_approved,
    execute_plan,
)

__all__ = [
    "BrokerError",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExecutionBroker",
    "ExecutionPlan",
    "ExecutionResult",
    "MAX_OUTPUT_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "PolicyDecision",
    "SENSITIVE_ENV_KEYS_EXACT",
    "SENSITIVE_ENV_KEY_SUFFIXES",
    "STATUS_APPROVED",
    "STATUS_EXECUTED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "build_child_environment",
    "clamp_timeout",
    "evaluate_command",
    "execute_approved",
    "execute_plan",
    "get_default_broker",
]

_default_broker: ExecutionBroker | None = None


def get_default_broker() -> ExecutionBroker:
    """Process-wide broker shared by agent tools and the CLI host."""
    global _default_broker
    if _default_broker is None:
        _default_broker = ExecutionBroker()
    return _default_broker
