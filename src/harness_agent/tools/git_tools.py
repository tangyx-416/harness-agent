"""Agent-visible Git Awareness tools (v0.4.0, READ-ONLY).

Exposes exactly four fixed-introspection tools:

* ``git_status``   -- working-tree status (porcelain v2, structured)
* ``git_diff``     -- working / staged / HEAD diff with literal pathspec
* ``git_log``      -- current HEAD history (bounded, structured)
* ``git_branches`` -- local branches (plus locally stored remote refs)

The model provides semantic parameters only. It can never choose a Git
subcommand, revision, or raw option; every argv is composed here from
fixed read-only templates. No mutation, no network. Git mutation is NOT
supported in v0.4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from strands import tool

from ..git_awareness import git_branches, git_diff, git_log, git_status
from .path_utils import find_repo_root


@tool(name="git_status")
def git_status_tool() -> dict[str, Any]:
    """Inspect the current Git working-tree status (read-only).

    Reports the current branch, HEAD commit, upstream tracking info and
    every staged / unstaged / untracked / conflicted file, based on
    machine-readable 'git status --porcelain=v2'. Use this first for
    questions like 'What changed?', 'Is the repository clean?' or
    'What branch am I on?'.

    Returns:
        Structured dict: {'ok', 'branch', 'head', 'upstream', 'ahead',
        'behind', 'clean', 'detached', 'unborn', 'staged', 'unstaged',
        'untracked', 'conflicts', 'truncated'} or {'ok': False, 'error'}.
    """
    return git_status(find_repo_root())


@tool(name="git_diff")
def git_diff_tool(
    scope: str = "working",
    path: str | None = None,
    context_lines: int = 3,
) -> dict[str, Any]:
    """Read a diff of repository changes (read-only).

    scope is fixed: 'working' (working tree vs index), 'staged' (index
    vs HEAD) or 'head' (all tracked working changes vs HEAD). The path
    filter is a literal repository-relative path (deleted tracked paths
    are allowed). Untracked files never appear in diffs -- use
    git_status to find them and read_file to view their content.

    Args:
        scope: One of 'working', 'staged', 'head' (default 'working').
        path: Optional literal repository-relative path filter.
        context_lines: Context lines per hunk, 0-10 (default 3).

    Returns:
        Structured dict: {'ok', 'scope', 'path', 'context_lines',
        'empty', 'diff', 'output_chars', 'truncated'} or
        {'ok': False, 'error'}.
    """
    return git_diff(scope, path=path, context_lines=context_lines, root=find_repo_root())


@tool(name="git_log")
def git_log_tool(limit: int = 20, path: str | None = None) -> dict[str, Any]:
    """Read the commit history of the current HEAD (read-only).

    Returns the most recent commits (newest first) with hash, author,
    ISO date and subject. Only the current HEAD history is available;
    arbitrary revisions, ranges and remote refs are not supported.

    Args:
        limit: Number of commits, 1-50 (default 20).
        path: Optional literal repository-relative path filter.

    Returns:
        Structured dict: {'ok', 'limit', 'path', 'commits': [{'hash',
        'short_hash', 'author', 'date', 'subject'}], 'truncated'} or
        {'ok': False, 'error'}.
    """
    return git_log(limit, path=path, root=find_repo_root())


@tool(name="git_branches")
def git_branches_tool(include_remote: bool = False) -> dict[str, Any]:
    """List Git branches known locally (read-only, no network).

    Lists local branches; with include_remote=True also the locally
    stored remote-tracking refs (e.g. origin/main as last fetched).
    This never performs any network operation.

    Args:
        include_remote: Include local remote-tracking refs (default False).

    Returns:
        Structured dict: {'ok', 'current', 'detached', 'unborn',
        'branches': [{'name', 'commit', 'kind', 'is_current'}],
        'truncated'} or {'ok': False, 'error'}.
    """
    return git_branches(include_remote=include_remote, root=find_repo_root())


# ---------------------------------------------------------------------------
# Testable cores (explicit repository root)
# ---------------------------------------------------------------------------


def git_status_core(root: Path | None = None) -> dict[str, Any]:
    """Run :func:`git_status_tool` against an explicit repository root."""
    from ..git_awareness import git_status as _status

    return _status(root if root is not None else find_repo_root())


def git_diff_core(
    scope: str = "working",
    path: str | None = None,
    context_lines: int = 3,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run :func:`git_diff_tool` against an explicit repository root."""
    from ..git_awareness import git_diff as _diff

    return _diff(scope, path=path, context_lines=context_lines, root=root)


def git_log_core(
    limit: int = 20,
    path: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run :func:`git_log_tool` against an explicit repository root."""
    from ..git_awareness import git_log as _log

    return _log(limit, path=path, root=root)


def git_branches_core(include_remote: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Run :func:`git_branches_tool` against an explicit repository root."""
    from ..git_awareness import git_branches as _branches

    return _branches(include_remote=include_remote, root=root)
