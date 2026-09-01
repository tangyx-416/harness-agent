"""Parsers for machine-readable Git output (v0.4.0).

Only stable machine formats are parsed -- never human-readable output:

* ``git status --porcelain=v2 --branch -z`` (NUL-separated records)
* ``git log --format=%H%x00%h%x00%an%x00%aI%x00%s%x00`` (NUL-terminated
  fields; NUL is the only byte a commit subject/author can never contain
  via the Git CLI, so untrusted commit data cannot collide with the
  framing)
* ``git for-each-ref --format=<US-delimited fields>`` -- safe because
  ``git check-ref-format`` forbids control characters (0x00-0x1F) and
  spaces inside refnames, so ``\\x1f`` can never occur in a legal
  refname and ``%(HEAD)`` is exactly ``*`` or ``space``

Paths are received as literal UTF-8 (``-z`` disables quoting and
``core.quotepath=false`` unquotes diff/log displays), so Unicode names
and spaces are handled exactly; byte sequences that are not valid
UTF-8 become replacement characters (see the shared runner's
``errors="replace"``) instead of crashing or corrupting the result.
"""

from __future__ import annotations

import re

from .models import GitBranch, GitCommit, GitStatus

#: Field delimiter used by the for-each-ref format. Safe for refnames:
#: git-check-ref-format rejects ASCII control characters and spaces.
FIELD_SEP = "\x1f"

#: Number of NUL-terminated fields per parsed log record.
_LOG_FIELDS_PER_RECORD = 5

#: subject cap: an abnormal commit message must not flood agent context.
SUBJECT_MAX_CHARS = 500

_BRANCH_AB_RE = re.compile(r"^# branch\.ab \+(\d+) -(\d+)$")
_FULL_HASH_RE = re.compile(r"^[0-9a-f]{40}$")

# porcelain v2 record prefixes: "1 " changed, "2 " renamed/copied,
# "u " unmerged, "? " untracked, "# " header, "!" ignored (not requested).


def _append_entry(bucket: list[dict[str, str]], entry: dict[str, str], state: dict) -> None:
    """Append respecting the global entry cap; flag truncation on overflow."""
    if state["count"] >= state["max_entries"]:
        state["truncated"] = True
        return
    bucket.append(entry)
    state["count"] += 1


def _classify_xy(xy: str, path: str, old_path: str | None, buckets: dict, state: dict) -> None:
    """Split an XY status into staged / unstaged entries."""
    x, y = xy[0], xy[1]
    if x != ".":
        entry = {"path": path, "status": x}
        if old_path:
            entry["old_path"] = old_path
        _append_entry(buckets["staged"], entry, state)
    if y != ".":
        _append_entry(buckets["unstaged"], {"path": path, "status": y}, state)


def parse_status(raw: str, max_entries: int = 500) -> GitStatus:
    """Parse ``git status --porcelain=v2 --branch -z`` output."""
    branch_oid: str | None = None
    branch_head: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None

    buckets: dict[str, list[dict[str, str]]] = {
        "staged": [],
        "unstaged": [],
        "conflicts": [],
    }
    untracked: list[dict[str, str]] = []
    state = {"count": 0, "max_entries": max_entries, "truncated": False}

    tokens = raw.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue

        if token.startswith("# branch.oid "):
            branch_oid = token[len("# branch.oid "):]
        elif token.startswith("# branch.head "):
            branch_head = token[len("# branch.head "):]
        elif token.startswith("# branch.upstream "):
            upstream = token[len("# branch.upstream "):]
        elif token.startswith("# branch.ab "):
            match = _BRANCH_AB_RE.match(token)
            if match:
                ahead = int(match.group(1))
                behind = int(match.group(2))
        elif token.startswith("1 "):
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            parts = token.split(" ", 8)
            if len(parts) == 9:
                _classify_xy(parts[1], parts[8], None, buckets, state)
        elif token.startswith("2 "):
            # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>\0<origPath>
            parts = token.split(" ", 9)
            if len(parts) == 10:
                orig_path = tokens[index] if index < len(tokens) else ""
                index += 1  # rename entries consume the extra NUL slot
                _classify_xy(parts[1], parts[9], orig_path, buckets, state)
        elif token.startswith("u "):
            # u <XY> <sub> <m1> <m2> <m3> <h1> <h2> <h3> <path>
            parts = token.split(" ", 9)
            if len(parts) == 10:
                _append_entry(
                    buckets["conflicts"],
                    {"path": parts[9], "status": parts[1]},
                    state,
                )
        elif token.startswith("? "):
            _append_entry(untracked, {"path": token[2:], "status": "?"}, state)
        # "! ..." ignored entries are only produced with --ignored; skip.

    detached = branch_head == "(detached)"
    unborn = branch_oid == "(initial)"
    branch = None if detached else branch_head

    staged = buckets["staged"]
    unstaged = buckets["unstaged"]
    conflicts = buckets["conflicts"]
    clean = not (staged or unstaged or conflicts or untracked)

    return GitStatus(
        branch=branch,
        head=branch_oid,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        clean=clean,
        detached=detached,
        unborn=unborn,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        conflicts=conflicts,
        truncated=state["truncated"],
    )


def parse_log(raw: str, subject_limit: int = SUBJECT_MAX_CHARS) -> list[GitCommit]:
    """Parse our NUL-framed ``git log --format=...`` output (newest first).

    Framing: each commit contributes exactly five NUL-terminated fields
    (hash, short hash, author, ISO date, subject). Because the Git CLI
    cannot place a NUL byte inside a commit subject or author name, the
    delimiter can never collide with untrusted repository data.
    Malformed input degrades gracefully: incomplete trailing fields are
    dropped and records whose hash is not a 40-char hex string are
    skipped instead of raising.
    """
    commits: list[GitCommit] = []
    tokens = raw.split("\0")

    for start in range(0, len(tokens) - _LOG_FIELDS_PER_RECORD + 1,
                       _LOG_FIELDS_PER_RECORD):
        group = tokens[start:start + _LOG_FIELDS_PER_RECORD]
        commit_hash = group[0].lstrip("\n").strip()
        if not _FULL_HASH_RE.match(commit_hash):
            continue  # malformed/garbled record: skip safely
        subject = group[4]
        if len(subject) > subject_limit:
            subject = subject[:subject_limit]
        commits.append(
            GitCommit(
                hash=commit_hash,
                short_hash=group[1].strip(),
                author=group[2],
                date=group[3].strip(),
                subject=subject,
            )
        )
    return commits


def parse_branches(raw: str) -> list[GitBranch]:
    """Parse ``git for-each-ref --format=...`` output."""
    branches: list[GitBranch] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(FIELD_SEP)
        if len(parts) < 3:
            continue
        refname, objectname, head_mark = parts[0], parts[1], parts[2].strip()
        if refname.startswith("refs/heads/"):
            name = refname[len("refs/heads/"):]
            kind = "local"
        elif refname.startswith("refs/remotes/"):
            name = refname[len("refs/remotes/"):]
            kind = "remote"
        else:
            name = refname
            kind = "other"
        branches.append(
            GitBranch(
                name=name,
                commit=objectname,
                kind=kind,
                is_current=head_mark == "*",
            )
        )
    return branches
