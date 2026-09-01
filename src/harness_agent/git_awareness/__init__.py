"""Git Awareness subsystem (v0.4.0) -- READ-ONLY.

Agent can understand Git state, diffs, history and branches, but has no
Git mutation authority:

    Repository read   -> YES   (v0.2 tools)
    Git read          -> YES   (git_status / git_diff / git_log / git_branches)
    Controlled run    -> YES   (v0.3, user-approved)
    Source write      -> NO
    Git mutation      -> NO
    Git network       -> NO

Git Awareness != Git Authority.
"""

from __future__ import annotations

from .models import GitBranch, GitCommit, GitStatus
from .parsers import (
    FIELD_SEP,
    SUBJECT_MAX_CHARS,
    parse_branches,
    parse_log,
    parse_status,
)
from .service import (
    ALLOWED_GIT_SUBCOMMANDS,
    GIT_LOG_DEFAULT_LIMIT,
    GIT_LOG_MAX_LIMIT,
    GIT_MAX_BRANCHES,
    GIT_MAX_STATUS_ENTRIES,
    GIT_STDERR_LIMIT,
    GIT_STDOUT_LIMIT,
    GIT_TIMEOUT_SECONDS,
    build_git_environment,
    find_git_executable,
    git_branches,
    git_diff,
    git_log,
    git_status,
    validate_git_pathspec,
)

__all__ = [
    "ALLOWED_GIT_SUBCOMMANDS",
    "FIELD_SEP",
    "GIT_LOG_DEFAULT_LIMIT",
    "GIT_LOG_MAX_LIMIT",
    "GIT_MAX_BRANCHES",
    "GIT_MAX_STATUS_ENTRIES",
    "GIT_STDERR_LIMIT",
    "GIT_STDOUT_LIMIT",
    "GIT_TIMEOUT_SECONDS",
    "GitBranch",
    "GitCommit",
    "GitStatus",
    "SUBJECT_MAX_CHARS",
    "build_git_environment",
    "find_git_executable",
    "git_branches",
    "git_diff",
    "git_log",
    "git_status",
    "parse_branches",
    "parse_log",
    "parse_status",
    "validate_git_pathspec",
]
