"""Execution policy for v0.3.0 User-Approved Safe Execution.

Strict allowlist -- the ONLY commands the agent may *request* are:

1. ``python --version``            (normalized to ``sys.executable --version``)
2. ``python -m pytest  [...]``     (normalized to ``sys.executable -m pytest ...``)
3. ``python -m ruff check [...]``  (normalized to ``sys.executable -m ruff check ...``)

Bare ``pytest`` / ``ruff`` program names are accepted as convenience
aliases and normalized the same way. Everything else -- pip, git, shell
binaries, arbitrary modules, inline code, script paths -- is denied.

Key properties:

* Policy understands Python arguments; it never trusts ``executable ==
  "python"`` alone. ``-c``, ``-``, script paths and ``-m <any-module>``
  are refused.
* Every argument token is scanned for shell metacharacters
  (``; & | > < ` $`` newlines) even though execution always uses
  ``shell=False``: v0.3 is not a shell-language agent.
* ``pytest`` flags are validated one by one; only a small, safe set is
  allowed. Path-like arguments must resolve inside the repository root
  (reusing :mod:`harness_agent.tools.path_utils`; no second resolver).
* The working directory is confined to the repository root.
* Risk levels (LOW/MEDIUM/HIGH) are informational: every execution still
  requires explicit user approval.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ..tools.path_utils import (
    find_repo_root,
    path_is_dangerous,
    resolve_user_path,
)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 60
MIN_TIMEOUT_SECONDS = 1
#: Per-stream output cap for captured stdout/stderr (~64 KB each).
MAX_OUTPUT_CHARS = 64 * 1024

# ---------------------------------------------------------------------------
# Allowlist definition
# ---------------------------------------------------------------------------

#: Program names normalized to the current interpreter.
_PYTHON_PROGRAM_ALIASES = frozenset({"python", "python3", "py"})
#: Convenience aliases normalized to ``sys.executable -m <tool>``.
_TOOL_PROGRAM_ALIASES = {"pytest": ("-m", "pytest"), "ruff": ("-m", "ruff")}

#: Shell syntax refused in every token (defense in depth on top of shell=False).
_SHELL_METACHARACTERS = (";", "&", "|", ">", "<", "`", "$", "\n", "\r")

#: pytest boolean/short flags allowed verbatim.
_PYTEST_BOOL_FLAGS = frozenset({"-q", "-v", "-x", "--collect-only"})
#: Safe character set for ``-k`` expressions (no quotes/metacharacters).
_K_EXPRESSION_RE = re.compile(r"^[A-Za-z0-9_ .()-]+$")

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

_RISK_LOW_REASON = (
    "Reads the interpreter version only; no repository code is executed."
)
_RISK_MEDIUM_REASON = (
    "Read-only static analysis; inspects repository files without modifying them."
)
_RISK_HIGH_REASON = (
    "Executes repository Python code with the current operating-system "
    "user's privileges."
)

_PYTHON_USAGE_HINT = (
    "Allowed python invocations: 'python --version', "
    "'python -m pytest [flags] [paths]', 'python -m ruff check [paths]'."
)

PolicyOutcome = "tuple[list[str], str, str] | str"  # (tail, risk, reason) or error


def clamp_timeout(value: object) -> int:
    """Clamp a requested timeout into ``[1, 60]`` seconds (default 30)."""
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, parsed))


def _contains_shell_syntax(token: str) -> bool:
    return any(ch in token for ch in _SHELL_METACHARACTERS)


def _deny(reason: str, timeout: int) -> "PolicyDecision":
    from .models import PolicyDecision

    return PolicyDecision(allowed=False, reason=reason, timeout_seconds=timeout)


def _allow(
    tail: list[str],
    risk_level: str,
    risk_reason: str,
    cwd_path: Path,
    timeout: int,
) -> "PolicyDecision":
    from .models import PolicyDecision

    argv = (sys.executable, *tail)
    return PolicyDecision(
        allowed=True,
        reason="Allowed by the v0.3.0 command allowlist.",
        argv=argv,
        cwd=str(cwd_path),
        display_command=" ".join(["python", *tail]),
        risk_level=risk_level,
        risk_reason=risk_reason,
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Path validation (reuses path_utils -- no second resolver)
# ---------------------------------------------------------------------------


def _validate_path_arg(token: str, root: Path) -> str | None:
    """Return an error string when *token* is not a safe repository path."""
    resolved, error = resolve_user_path(token, root)
    if error is not None:
        return f"Path argument refused ({token!r}): {error}."
    if path_is_dangerous(resolved, root, target_is_dir=True) or path_is_dangerous(
        resolved, root, target_is_dir=False
    ):
        return (
            f"Path argument refused ({token!r}): it points to an ignored or "
            "sensitive location."
        )
    return None


def _validate_cwd(cwd: str, root: Path) -> tuple[Path | None, str | None]:
    """Validate the working directory; it must stay inside the repository."""
    resolved, error = resolve_user_path(cwd or ".", root)
    if error is not None:
        return None, f"Working directory refused ({cwd!r}): {error}."
    if path_is_dangerous(resolved, root, target_is_dir=True):
        return None, (
            f"Working directory refused ({cwd!r}): it points to an ignored "
            "or sensitive location."
        )
    if not resolved.is_dir():
        return None, f"Working directory does not exist: {resolved}"
    return resolved, None


# ---------------------------------------------------------------------------
# Per-command argument validators
# ---------------------------------------------------------------------------


def _validate_pytest_tail(
    rest: list[str], root: Path
) -> "tuple[list[str], str, str] | str":
    """Validate pytest arguments; returns (tail, risk, reason) or an error."""
    validated: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]

        if token in _PYTEST_BOOL_FLAGS:
            validated.append(token)
            index += 1
            continue

        if token == "--tb=short":
            validated.append(token)
            index += 1
            continue
        if token.startswith("--tb="):
            return (
                f"Unsupported '--tb' value (refused: {token!r}). "
                "Only '--tb=short' is allowed."
            )

        if token == "--maxfail" or token.startswith("--maxfail="):
            if token == "--maxfail":
                if index + 1 >= len(rest):
                    return "'--maxfail' requires a numeric value >= 1."
                value_token = rest[index + 1]
                index += 2
            else:
                value_token = token.split("=", 1)[1]
                index += 1
            if not value_token.isdigit() or int(value_token) < 1:
                return (
                    f"'--maxfail' requires a numeric value >= 1 "
                    f"(refused: {value_token!r})."
                )
            validated.extend(["--maxfail", value_token])
            continue

        if token == "-k" or token.startswith("-k="):
            if token == "-k":
                if index + 1 >= len(rest):
                    return "'-k' requires an expression."
                expression = rest[index + 1]
                index += 2
            else:
                expression = token.split("=", 1)[1]
                index += 1
            if not _K_EXPRESSION_RE.fullmatch(expression):
                return (
                    f"'-k' expression contains unsupported characters "
                    f"(refused: {expression!r})."
                )
            validated.extend(["-k", expression])
            continue

        if token.startswith("-"):
            return (
                f"Unsupported pytest flag (refused: {token!r}). Allowed: "
                "-q -v -x --collect-only --tb=short --maxfail=N -k <expr> "
                "and repository-relative paths."
            )

        error = _validate_path_arg(token, root)
        if error is not None:
            return error
        validated.append(token)
        index += 1

    return validated, RISK_HIGH, _RISK_HIGH_REASON


def _validate_ruff_tail(
    rest: list[str], root: Path
) -> "tuple[list[str], str, str] | str":
    """Validate ruff arguments; only the read-only 'check' subcommand passes."""
    if not rest:
        return (
            "'-m ruff' requires the 'check' subcommand. "
            "'ruff format' and fix modes are not allowed."
        )
    if rest[0] != "check":
        return (
            f"Only the read-only 'check' subcommand is allowed for ruff "
            f"(refused: {rest[0]!r})."
        )

    validated: list[str] = ["-m", "ruff", "check"]
    for token in rest[1:]:
        if token.startswith("-"):
            return (
                f"ruff options are not allowed in v0.3 (refused: {token!r}). "
                "Use plain 'ruff check <paths>'; '--fix' and "
                "'--unsafe-fixes' are forbidden."
            )
        error = _validate_path_arg(token, root)
        if error is not None:
            return error
        validated.append(token)

    return validated, RISK_MEDIUM, _RISK_MEDIUM_REASON


def _validate_python_tail(
    rest: list[str], root: Path
) -> "tuple[list[str], str, str] | str":
    """Validate arguments following the python executable itself."""
    if not rest:
        return f"python requires explicit arguments. {_PYTHON_USAGE_HINT}"

    head = rest[0]

    if head == "--version":
        if len(rest) > 1:
            return "'--version' accepts no additional arguments."
        return ["--version"], RISK_LOW, _RISK_LOW_REASON

    if head.startswith("-c"):
        return "Inline code execution via '-c' is not allowed."
    if head == "-":
        return "Reading a program from stdin ('-') is not allowed."

    if head == "-m":
        if len(rest) < 2:
            return "'-m' requires a module name."
        module = rest[1]
        if module == "pytest":
            outcome = _validate_pytest_tail(rest[2:], root)
            if isinstance(outcome, str):
                return outcome
            tail, risk, reason = outcome
            return ["-m", "pytest", *tail], risk, reason
        if module == "ruff":
            return _validate_ruff_tail(rest[2:], root)
        return (
            f"Arbitrary module execution is not allowed (refused: "
            f"'-m {module}'). Only '-m pytest' and '-m ruff check' "
            "are permitted."
        )

    return (
        f"Arbitrary python entry points are not allowed (refused: "
        f"{head!r}). {_PYTHON_USAGE_HINT}"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_command(
    program: str,
    args: list[str] | tuple[str, ...] | None = None,
    cwd: str = ".",
    root: Path | None = None,
    timeout_seconds: int | None = None,
) -> "PolicyDecision":
    """Normalize and validate an execution request against the policy.

    Args:
        program: Requested program (``python``, ``pytest``, ``ruff`` ...).
        args: Requested argument tokens.
        cwd: Requested working directory (must stay inside the repository).
        root: Repository root for confinement (auto-discovered when None).
        timeout_seconds: Optional timeout request (clamped to 1-60s).

    Returns:
        A :class:`~harness_agent.execution.models.PolicyDecision`. Check
        ``.allowed`` first; ``.reason`` explains any refusal.
    """
    boundary = (root if root is not None else find_repo_root()).resolve()
    timeout = clamp_timeout(timeout_seconds)
    rest = [str(token) for token in (args or [])]

    raw_program = str(program or "").strip()
    if _contains_shell_syntax(raw_program):
        return _deny(
            "Program contains shell syntax; v0.3 is not a shell-language agent.",
            timeout,
        )

    lowered = raw_program.lower()
    prefix_tail: list[str] = []
    if lowered in _PYTHON_PROGRAM_ALIASES:
        mode = "python"
    elif lowered in _TOOL_PROGRAM_ALIASES:
        mode = lowered
        prefix_tail = list(_TOOL_PROGRAM_ALIASES[lowered])
    else:
        return _deny(
            f"Program {raw_program!r} is not in the v0.3.0 command allowlist. "
            + _PYTHON_USAGE_HINT
            + " Bare 'pytest' and 'ruff' aliases are also accepted.",
            timeout,
        )

    for token in prefix_tail + rest:
        if _contains_shell_syntax(token):
            return _deny(
                f"Argument contains shell syntax (refused: {token!r}); "
                "commands like 'cmd1 && cmd2', pipes and redirects are not "
                "part of the allowlist.",
                timeout,
            )

    if mode == "python":
        outcome = _validate_python_tail(rest, boundary)
    elif mode == "pytest":
        outcome = _validate_pytest_tail(rest, boundary)
        if not isinstance(outcome, str):
            tail, risk, reason = outcome
            outcome = (list(prefix_tail) + tail, risk, reason)
    else:  # ruff
        outcome = _validate_ruff_tail(rest, boundary)

    if isinstance(outcome, str):
        return _deny(outcome, timeout)

    tail, risk_level, risk_reason = outcome
    cwd_path, cwd_error = _validate_cwd(cwd, boundary)
    if cwd_error is not None:
        return _deny(cwd_error, timeout)

    assert cwd_path is not None
    return _allow(tail, risk_level, risk_reason, cwd_path, timeout)
