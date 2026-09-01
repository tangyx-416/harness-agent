"""Read-only Git service (v0.4.0 Git Awareness).

Host-controlled fixed read-only Git introspection. The model NEVER
supplies a Git subcommand, revision, or raw option -- it provides only
semantic parameters (scope, path, limit), and this module composes a
fixed, hardened argv.

Hardening summary:

* ``shell=False`` argv-list invocation only (shared bounded runner).
* Global hardening flags on every invocation::

    --no-pager --no-optional-locks --literal-pathspecs
    -c core.fsmonitor=false -c color.ui=false -c log.showSignature=false

* ``git diff`` additionally: ``--no-ext-diff --no-textconv --no-color
  --ignore-submodules=all`` and a host-generated ``-U<n>``.
* Environment: inherits the sanitized execution environment, then ALL
  inherited ``GIT_*`` variables are removed, then only host-set safe
  values are applied (``GIT_CONFIG_NOSYSTEM``/``GIT_CONFIG_GLOBAL``/
  ``GIT_TERMINAL_PROMPT``/``GIT_PAGER``/``GIT_OPTIONAL_LOCKS``/
  ``GIT_ATTR_NOSYSTEM``/``PAGER=cat``). Repo-local Git config remains
  readable so ordinary Git semantics work.
* Boundary: the agent repository root (path_utils) must EQUAL the real
  Git toplevel (``rev-parse --show-toplevel``); any mismatch is refused
  instead of silently widening the boundary.
* Output bounded via the shared runner (128 KB stdout / 32 KB stderr).
* Fixed subcommands only: rev-parse, status, diff, log, for-each-ref.
  No network operations (fetch/push/pull/clone/ls-remote are never
  composed).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from ..process import ProcessOutcome, run_bounded
from ..tools.path_utils import (
    find_repo_root,
    is_ignored_dir_name,
    is_sensitive_dir_name,
    is_sensitive_file_name,
)
from .parsers import (
    FIELD_SEP,
    SUBJECT_MAX_CHARS,
    parse_branches,
    parse_log,
    parse_status,
)

# ---------------------------------------------------------------------------
# Limits (v0.4 output bounds)
# ---------------------------------------------------------------------------

GIT_STDOUT_LIMIT = 128 * 1024
GIT_STDERR_LIMIT = 32 * 1024
GIT_MAX_STATUS_ENTRIES = 500
GIT_MAX_BRANCHES = 200
GIT_LOG_DEFAULT_LIMIT = 20
GIT_LOG_MAX_LIMIT = 50
GIT_DIFF_DEFAULT_CONTEXT = 3
GIT_DIFF_MAX_CONTEXT = 10
#: Fixed, host-side timeout for every Git invocation (not model-visible).
GIT_TIMEOUT_SECONDS = 30

#: Fixed log format: NUL-terminated fields
#: (hash \0 short \0 author \0 ISO-date \0 subject \0).
#: NUL is the only byte the Git CLI can never place inside a commit
#: subject or author name (execve argument limitation), making this
#: framing immune to delimiter injection from untrusted commit data.
_GIT_LOG_FORMAT = "%H%x00%h%x00%an%x00%aI%x00%s%x00"
#: Fixed for-each-ref format: refname US objectname US HEAD-marker.
_GIT_REF_FORMAT = FIELD_SEP.join(["%(refname)", "%(objectname)", "%(HEAD)"])

_BASE_GIT_FLAGS = [
    "--no-pager",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c", "core.fsmonitor=false",
    "-c", "color.ui=false",
    "-c", "log.showSignature=false",
    "-c", "core.quotepath=false",
]

_DIFF_FLAGS = [
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--ignore-submodules=all",
]

_HOST_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_ATTR_NOSYSTEM": "1",
    "PAGER": "cat",
}

#: Fixed read-only subcommands the service may ever compose.
ALLOWED_GIT_SUBCOMMANDS = frozenset({"rev-parse", "status", "diff", "log", "for-each-ref"})


# ---------------------------------------------------------------------------
# Environment hardening
# ---------------------------------------------------------------------------


def build_git_environment(parent_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Sanitized child environment for Git: base scrub + drop ALL
    inherited ``GIT_*`` (+ ``SSH_ASKPASS``), then host-set safe values.
    """
    from ..execution.service import build_child_environment

    env = build_child_environment(parent_env)
    env = {
        key: value
        for key, value in env.items()
        if not key.upper().startswith("GIT_") and key != "SSH_ASKPASS"
    }
    env.update(_HOST_GIT_ENV)
    return env


# ---------------------------------------------------------------------------
# Invocation helpers
# ---------------------------------------------------------------------------


def find_git_executable() -> Optional[str]:
    """Absolute path of the local Git executable (None when missing)."""
    return shutil.which("git")


def _run_git(git_exe: str, args: list[str], root: Path) -> ProcessOutcome:
    """Compose hardened argv [git, base flags, *args] and run bounded."""
    argv = [git_exe, *_BASE_GIT_FLAGS, *args]
    return run_bounded(
        argv,
        cwd=str(root),
        env=build_git_environment(),
        timeout=GIT_TIMEOUT_SECONDS,
        stdout_limit=GIT_STDOUT_LIMIT,
        stderr_limit=GIT_STDERR_LIMIT,
    )


def _validate_git_root(git_exe: str, root: Path) -> Optional[str]:
    """The real Git toplevel must equal the configured repository root."""
    outcome = _run_git(git_exe, ["rev-parse", "--show-toplevel"], root)
    if outcome.timed_out:
        return "Git command timed out."
    if outcome.exit_code != 0:
        return "The repository is not a Git working tree."
    try:
        actual = Path(outcome.stdout.strip()).resolve()
    except (OSError, ValueError):
        return "Could not determine the Git repository root."
    if os.path.normcase(str(actual)) != os.path.normcase(str(Path(root).resolve())):
        return "Git repository root differs from the configured repository root."
    return None


def _error(error: str, **extra) -> dict:
    payload = {"ok": False, "error": error}
    payload.update(extra)
    return payload


def _prepare(root: Path) -> "tuple[tuple[str, Path], dict] | dict":
    """Common precondition: git exists + boundary matches. Returns either
    ``(git_exe, resolved_root)`` or an error payload."""
    resolved_root = (root if root is not None else find_repo_root()).resolve()
    git_exe = find_git_executable()
    if not git_exe:
        return _error("Git executable was not found.")
    error = _validate_git_root(git_exe, resolved_root)
    if error is not None:
        return _error(error)
    return (git_exe, resolved_root)


# ---------------------------------------------------------------------------
# Pathspec validation (literal, repo-relative; deleted paths allowed)
# ---------------------------------------------------------------------------


def validate_git_pathspec(path: str, root: Path) -> Optional[str]:
    """Return an error string when *path* is not an acceptable literal
    repository-relative Git pathspec.

    Git paths may reference already-deleted tracked files, so existence
    is deliberately NOT required (unlike ``read_file``). Safety comes
    from lexical confinement plus Git-side ``--literal-pathspecs``.
    """
    raw = (path or "").strip()
    if not raw:
        return "Path must not be empty."
    if "\x00" in raw:
        return "Path must not contain NUL characters."
    if raw.startswith("/") or raw.startswith("\\"):
        return f"Absolute paths are not allowed (refused: {path!r})."

    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return f"Absolute paths are not allowed (refused: {path!r})."

    parts = candidate.parts
    if any(part == ".." for part in parts):
        return f"'..' components are not allowed (refused: {path!r})."

    try:
        resolved = (root / candidate).resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return f"Path escapes the repository root (refused: {path!r})."

    # Component policy consistent with the read tools (ignored/sensitive).
    for part in parts:
        if is_ignored_dir_name(part) or is_sensitive_dir_name(part):
            return f"Path points to an ignored or sensitive location (refused: {path!r})."
    last = parts[-1]
    if is_sensitive_file_name(last):
        return f"Path matches the sensitive file policy (refused: {path!r})."
    return None


# ---------------------------------------------------------------------------
# Tool 1 - git_status
# ---------------------------------------------------------------------------


def git_status(root: Optional[Path] = None) -> dict:
    """Structured Git working-tree status (porcelain v2, read-only)."""
    prepared = _prepare(root)
    if isinstance(prepared, dict):
        return prepared
    git_exe, resolved_root = prepared

    outcome = _run_git(
        git_exe, ["status", "--porcelain=v2", "--branch", "-z"], resolved_root
    )
    if outcome.timed_out:
        return _error("Git command timed out.")
    if outcome.exit_code != 0:
        return _error(f"Git status failed: {_brief(outcome.stderr)}")

    status = parse_status(outcome.stdout, GIT_MAX_STATUS_ENTRIES)
    payload = status.to_dict()
    payload.update(
        {
            "ok": True,
            "truncated": status.truncated or outcome.stdout_truncated,
        }
    )
    return payload


# ---------------------------------------------------------------------------
# Tool 2 - git_diff
# ---------------------------------------------------------------------------

#: Fixed scope enum -> extra argv. The model cannot pass revisions/options.
_DIFF_SCOPES = {
    "working": [],
    "staged": ["--cached"],
    "head": ["HEAD"],
}


def git_diff(
    scope: str = "working",
    path: Optional[str] = None,
    context_lines: int = GIT_DIFF_DEFAULT_CONTEXT,
    root: Optional[Path] = None,
) -> dict:
    """Structured read-only diff for a fixed scope enum."""
    prepared = _prepare(root)
    if isinstance(prepared, dict):
        return prepared
    git_exe, resolved_root = prepared

    if scope not in _DIFF_SCOPES:
        return _error("scope must be one of: working, staged, head.", denied=True)

    try:
        ctx = int(context_lines)
    except (TypeError, ValueError):
        ctx = GIT_DIFF_DEFAULT_CONTEXT
    ctx = max(0, min(GIT_DIFF_MAX_CONTEXT, ctx))

    args = ["diff", f"-U{ctx}", *_DIFF_FLAGS, *_DIFF_SCOPES[scope]]
    if path is not None:
        error = validate_git_pathspec(path, resolved_root)
        if error is not None:
            return _error(error, denied=True)
        args += ["--", path]

    outcome = _run_git(git_exe, args, resolved_root)
    if outcome.timed_out:
        return _error("Git command timed out.")
    if outcome.exit_code != 0:
        return _error(f"Git diff failed: {_brief(outcome.stderr)}")

    diff_text = outcome.stdout
    return {
        "ok": True,
        "scope": scope,
        "path": path,
        "context_lines": ctx,
        "empty": len(diff_text.strip()) == 0,
        "diff": diff_text,
        "output_chars": len(diff_text),
        "truncated": outcome.stdout_truncated,
    }


# ---------------------------------------------------------------------------
# Tool 3 - git_log
# ---------------------------------------------------------------------------


def git_log(
    limit: int = GIT_LOG_DEFAULT_LIMIT,
    path: Optional[str] = None,
    root: Optional[Path] = None,
) -> dict:
    """Structured commit history of the current HEAD (read-only).

    HEAD-only by design: the model cannot pass revisions (``HEAD~999``,
    ``--all``, ``branch..branch``, remote refs).
    """
    prepared = _prepare(root)
    if isinstance(prepared, dict):
        return prepared
    git_exe, resolved_root = prepared

    try:
        effective_limit = int(limit)
    except (TypeError, ValueError):
        effective_limit = GIT_LOG_DEFAULT_LIMIT
    effective_limit = max(1, min(GIT_LOG_MAX_LIMIT, effective_limit))

    args = [
        "log",
        f"-n{effective_limit}",
        f"--format={_GIT_LOG_FORMAT}",
    ]
    if path is not None:
        error = validate_git_pathspec(path, resolved_root)
        if error is not None:
            return _error(error, denied=True)
        args += ["--", path]

    outcome = _run_git(git_exe, args, resolved_root)
    if outcome.timed_out:
        return _error("Git command timed out.")
    if outcome.exit_code != 0:
        if "does not have any commits yet" in outcome.stderr:
            return {
                "ok": True,
                "limit": effective_limit,
                "path": path,
                "commits": [],
                "note": "Repository has no commits yet.",
                "truncated": False,
            }
        return _error(f"Git log failed: {_brief(outcome.stderr)}")

    commits = parse_log(outcome.stdout, SUBJECT_MAX_CHARS)
    return {
        "ok": True,
        "limit": effective_limit,
        "path": path,
        "commits": [commit.to_dict() for commit in commits],
        "truncated": outcome.stdout_truncated,
    }


# ---------------------------------------------------------------------------
# Tool 4 - git_branches
# ---------------------------------------------------------------------------


def git_branches(include_remote: bool = False, root: Optional[Path] = None) -> dict:
    """Local branches (plus locally stored remote-tracking refs on request).

    ``include_remote`` reads ONLY local ``refs/remotes/`` metadata -- no
    network request is ever composed.
    """
    prepared = _prepare(root)
    if isinstance(prepared, dict):
        return prepared
    git_exe, resolved_root = prepared

    args = ["for-each-ref", f"--format={_GIT_REF_FORMAT}", f"--count={GIT_MAX_BRANCHES}"]
    if include_remote:
        args += ["refs/heads/", "refs/remotes/"]
    else:
        args += ["refs/heads/"]

    outcome = _run_git(git_exe, args, resolved_root)
    if outcome.timed_out:
        return _error("Git command timed out.")
    if outcome.exit_code != 0:
        return _error(f"Git branch listing failed: {_brief(outcome.stderr)}")

    branches = [branch.to_dict() for branch in parse_branches(outcome.stdout)]
    branches = branches[:GIT_MAX_BRANCHES]  # belt-and-braces on top of --count

    current: Optional[str] = None
    detached = False
    unborn = False
    head_outcome = _run_git(git_exe, ["rev-parse", "--abbrev-ref", "HEAD"], resolved_root)
    if head_outcome.exit_code == 0 and not head_outcome.timed_out:
        name = head_outcome.stdout.strip()
        if name == "HEAD":
            detached = True
        else:
            current = name
    else:
        # Unborn branch (no commits yet): rev-parse HEAD fails.
        unborn = True

    truncated = (
        outcome.stdout_truncated
        or len(branches) >= GIT_MAX_BRANCHES
    )
    return {
        "ok": True,
        "current": current,
        "detached": detached,
        "unborn": unborn,
        "branches": branches,
        "truncated": truncated,
    }


def _brief(stderr: str, cap: int = 200) -> str:
    """One-line bounded stderr excerpt for structured errors."""
    text = " ".join(stderr.split())
    return text[:cap] if text else "unknown git error"
