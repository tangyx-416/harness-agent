"""Structured Git Awareness data models (v0.4.0).

All models are immutable frozen dataclasses with ``to_dict()`` so tool
payloads are stable, JSON-friendly dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class GitStatus:
    """Parsed result of ``git status --porcelain=v2 --branch -z``."""

    branch: Optional[str]
    head: Optional[str]
    upstream: Optional[str]
    ahead: Optional[int]
    behind: Optional[int]
    clean: bool
    detached: bool
    unborn: bool
    staged: list[dict[str, str]] = field(default_factory=list)
    unstaged: list[dict[str, str]] = field(default_factory=list)
    untracked: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "head": self.head,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "clean": self.clean,
            "detached": self.detached,
            "unborn": self.unborn,
            "staged": list(self.staged),
            "unstaged": list(self.unstaged),
            "untracked": list(self.untracked),
            "conflicts": list(self.conflicts),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class GitCommit:
    """One commit entry from ``git log``."""

    hash: str
    short_hash: str
    author: str
    date: str
    subject: str

    def to_dict(self) -> dict[str, str]:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "author": self.author,
            "date": self.date,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class GitBranch:
    """One ref entry from ``git for-each-ref``."""

    name: str
    commit: str
    kind: str  # "local" | "remote"
    is_current: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "commit": self.commit,
            "kind": self.kind,
            "is_current": self.is_current,
        }
