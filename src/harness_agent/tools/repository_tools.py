"""Read-only Repository Tools for Harness Agent (v0.2.0).

Implements four repository understanding tools on top of ``path_utils``:

* ``list_directory``       -- browse directories inside the repository
* ``read_file``            -- safely read text files (with line ranges)
* ``search_code``          -- pure-Python text search across source files
* ``analyze_dependencies`` -- static analysis of dependency manifests

Design rules:

* Every user-supplied path goes through :func:`path_utils.resolve_user_path`
  so the resolved location can never escape the repository root.
* Ignored directories (.git/.venv/node_modules/...) and sensitive files
  (.env, keys, credentials...) are checked *before* touching the filesystem.
* Binary payloads are detected by extension and content sniffing.
* All outputs are bounded and every result carries a ``truncated`` flag when
  output had to be capped.
* No shell, no subprocess, no writes, no network: strictly read-only.

Success responses look like ``{"ok": True, ...}``; recoverable failures are
returned as structured errors ``{"ok": False, "error": "..."}`` instead of
raising exceptions into the agent loop.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from strands import tool

from .path_utils import (
    LIST_MAX_DEPTH,
    LIST_MAX_ENTRIES,
    READ_MAX_FILE_SIZE_BYTES,
    READ_MAX_LINES_PER_CALL,
    SEARCH_DEFAULT_RESULTS,
    SEARCH_MAX_FILE_SIZE_BYTES,
    SEARCH_MAX_RESULTS,
    SNIPPET_MAX_CHARS,
    find_repo_root,
    is_ignored_dir_name,
    is_known_binary_extension,
    is_sensitive_dir_name,
    is_sensitive_file_name,
    is_text_extension,
    looks_like_sourceless_text,
    path_is_dangerous,
    relative_display,
    resolve_user_path,
    sniff_binary_content,
)

_READ_CHUNK = 8192
#: Hard ceiling on how many raw bytes one streaming read window may buffer
#: (protects against pathological single-line files).
_STREAM_WINDOW_BYTE_CAP = 1024 * 1024


def _failure(error: str, **extra: Any) -> dict[str, Any]:
    """Standardized failure payload for every tool."""
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Tool 1 - list_directory
# ---------------------------------------------------------------------------


def _list_directory(
    root: Path,
    path: str = ".",
    depth: int = 1,
) -> dict[str, Any]:
    """Core directory browsing implementation confined to *root*."""
    target, error = resolve_user_path(path, root)
    if error is not None:
        return _failure(error)

    if path_is_dangerous(target, root, target_is_dir=True):
        return _failure(
            "Access denied: this path points to an ignored or sensitive "
            "location and cannot be browsed.",
            requested_path=str(path),
        )

    if not target.exists():
        return _failure(f"Directory not found: {relative_display(target, root)}")
    if not target.is_dir():
        return _failure(f"Not a directory: {relative_display(target, root)}")

    requested_depth = int(depth)
    depth_note = None
    if requested_depth < 1:
        depth_note = f"depth < 1 not allowed, using 1"
        requested_depth = 1
    elif requested_depth > LIST_MAX_DEPTH:
        depth_note = f"depth limited to maximum ({LIST_MAX_DEPTH})"
        requested_depth = LIST_MAX_DEPTH

    entries: list[dict[str, Any]] = []
    truncated = False

    def scan(directory: Path, level: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for child in children:
            if len(entries) >= LIST_MAX_ENTRIES:
                truncated = True
                return
            if child.is_dir():
                if is_ignored_dir_name(child.name) or is_sensitive_dir_name(child.name):
                    continue
                entries.append({"name": child.name, "type": "directory"})
                if level < requested_depth:
                    scan(child, level + 1)
            else:
                if child.is_file() and is_sensitive_file_name(child.name):
                    continue
                entries.append({"name": child.name, "type": "file"})

    scan(target, 1)

    result: dict[str, Any] = {
        "ok": True,
        "path": relative_display(target, root),
        "depth": requested_depth,
        "entry_count": len(entries),
        "entries": entries,
        "truncated": truncated,
    }
    if depth_note:
        result["note"] = depth_note
    return result


@tool
def list_directory(path: str = ".", depth: int = 1) -> dict[str, Any]:
    """Browse the repository directory tree starting at 'path'.

    Lists files and subdirectories recursively up to 'depth' levels. Hidden
    build/cache directories (.git, .venv, __pycache__, node_modules...)
    and sensitive locations are never shown. Output is bounded at 200
    entries with a 'truncated' flag.

    Args:
        path: Directory to browse, relative to the repository root ('.' default).
        depth: Recursion depth between 1 and 3 (default 1).

    Returns:
        Structured dict: {'ok', 'path', 'entries': [{'name','type'}],
        'entry_count', 'truncated'} or {'ok': False, 'error'}.
    """
    return _list_directory(find_repo_root(), path, depth)


# ---------------------------------------------------------------------------
# Tool 2 - read_file
# ---------------------------------------------------------------------------


def _read_file(
    root: Path,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Core safe file reading implementation confined to *root*."""
    target, error = resolve_user_path(path, root)
    if error is not None:
        return _failure(error)

    if path_is_dangerous(target, root, target_is_dir=False):
        return _failure(
            "Access denied: this file matches the sensitive/generated path "
            "policy and cannot be read.",
            requested_path=str(path),
        )

    if not target.exists():
        return _failure(f"File not found: {relative_display(target, root)}")
    if target.is_dir():
        return _failure(f"Path is a directory, not a file: {relative_display(target, root)}")

    if is_known_binary_extension(target.suffix):
        return _failure("File is binary and cannot be read by this tool.")

    try:
        with open(target, "rb") as handle:
            head = handle.read(_READ_CHUNK)
        if sniff_binary_content(head):
            return _failure("File is binary and cannot be read by this tool.")
        size_bytes = target.stat().st_size
    except OSError as exc:
        return _failure(f"Cannot access file: {exc}")

    has_range = start_line is not None or end_line is not None
    if size_bytes > READ_MAX_FILE_SIZE_BYTES and not has_range:
        return _failure(
            "File exceeds safe read limit "
            f"({size_bytes} > {READ_MAX_FILE_SIZE_BYTES} bytes). "
            "Use line-range reading if appropriate."
        )

    try:
        s_line = 1 if start_line is None else int(start_line)
        e_line = None if end_line is None else int(end_line)
    except (TypeError, ValueError):
        return _failure("Invalid line range: start_line/end_line must be integers.")

    if s_line < 1:
        return _failure("Invalid line range: start_line must be >= 1.")
    if e_line is not None and e_line < s_line:
        return _failure(f"Invalid line range: end_line ({e_line}) < start_line ({s_line}).")

    if size_bytes > READ_MAX_FILE_SIZE_BYTES:
        # Oversized text files are only ever read through the streaming
        # line-range path so the whole file is never loaded into memory.
        return _read_large_file_ranged(target, root, size_bytes, s_line, e_line)

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return _failure(f"Cannot read file: {exc}")

    encoding_used = "utf-8"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
            encoding_used = "latin-1"
        except UnicodeDecodeError:
            return _failure("File could not be decoded as text.")

    lines = text.splitlines()
    total_lines = len(lines)
    effective_end = total_lines if e_line is None else e_line

    if s_line > total_lines:
        return _failure(
            f"start_line={s_line} is beyond end of file (total_lines={total_lines})"
        )

    clamped_high = effective_end > total_lines
    stop = min(effective_end, total_lines)

    window = lines[s_line - 1 : stop]
    sliced = False
    if len(window) > READ_MAX_LINES_PER_CALL:
        window = window[: READ_MAX_LINES_PER_CALL]
        sliced = True

    numbered = "\n".join(
        f"{number} | {content}"
        for number, content in enumerate(window, start=s_line)
    )

    result: dict[str, Any] = {
        "ok": True,
        "path": relative_display(target, root),
        "encoding": encoding_used,
        "size_bytes": size_bytes,
        "total_lines": total_lines,
        "start_line": s_line,
        "end_line": s_line + len(window) - 1,
        "requested_end_line": effective_end,
        "max_lines_per_call": READ_MAX_LINES_PER_CALL,
        "truncated": sliced,
        "content_truncated_at": s_line + len(window) - 1 if sliced else None,
        "range_clamped_to_eof": clamped_high,
        "content": numbered,
    }
    return result


def _read_large_file_ranged(
    target: Path,
    root: Path,
    size_bytes: int,
    s_line: int,
    e_line: int | None,
) -> dict[str, Any]:
    """Streaming line-range reader for oversized text files.

    Iterates the file line by line in binary mode, buffering at most
    ``READ_MAX_LINES_PER_CALL`` lines (and at most ``_STREAM_WINDOW_BYTE_CAP``
    bytes) of the requested window. The full file is never held in memory.
    """
    window: list[bytes] = []
    window_bytes = 0
    byte_capped = False
    total_lines = 0

    try:
        with open(target, "rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                total_lines = line_number
                if line_number < s_line:
                    continue
                if e_line is not None and line_number > e_line:
                    continue
                if len(window) >= READ_MAX_LINES_PER_CALL:
                    continue
                if window_bytes >= _STREAM_WINDOW_BYTE_CAP:
                    byte_capped = True
                    continue
                window.append(raw_line)
                window_bytes += len(raw_line)
    except OSError as exc:
        return _failure(f"Cannot read file: {exc}")

    if s_line > total_lines:
        return _failure(
            f"start_line={s_line} is beyond end of file (total_lines={total_lines})"
        )

    encoding_used = "utf-8"
    blob = b"".join(window)
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = blob.decode("latin-1")
            encoding_used = "latin-1"
        except UnicodeDecodeError:
            return _failure("File could not be decoded as text.")

    lines = text.splitlines()
    served_end = s_line + len(lines) - 1
    requested_end = e_line if e_line is not None else total_lines
    effective_end = min(requested_end, total_lines)
    truncated = served_end < effective_end or byte_capped
    clamped_high = e_line is not None and e_line > total_lines

    numbered = "\n".join(
        f"{number} | {content}"
        for number, content in enumerate(lines, start=s_line)
    )

    return {
        "ok": True,
        "path": relative_display(target, root),
        "encoding": encoding_used,
        "size_bytes": size_bytes,
        "total_lines": total_lines,
        "start_line": s_line,
        "end_line": served_end,
        "requested_end_line": requested_end,
        "max_lines_per_call": READ_MAX_LINES_PER_CALL,
        "truncated": truncated,
        "content_truncated_at": served_end if truncated else None,
        "range_clamped_to_eof": clamped_high,
        "content": numbered,
    }


@tool
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Safely read a text file located inside the repository.

    Enforces the repository boundary, blocks sensitive files (.env, private
    keys, credentials) and generated artifacts, refuses binary formats by
    extension and content sniffing, and caps files larger than 256 KB behind
    explicit line ranges. At most 400 lines are returned per call. Content
    lines are prefixed with their numbers, e.g. '42 | def main():'.

    Args:
        path: File path relative to the repository root.
        start_line: First line to read, 1-based (default: 1).
        end_line: Last line to read, inclusive (default: end of file).

    Returns:
        Structured dict with numbered 'content' plus metadata
        ('total_lines', 'start_line', 'end_line', 'truncated') or
        {'ok': False, 'error'}.
    """
    return _read_file(find_repo_root(), path, start_line, end_line)


# ---------------------------------------------------------------------------
# Tool 3 - search_code
# ---------------------------------------------------------------------------

#: Extension-less text files worth searching.
_EXTRA_SOURCE_NAMES = frozenset({"makefile", "dockerfile", "license", "notice"})


def _is_searchable_file(file_path: Path) -> bool:
    """Whitelist decision for searchable text source files."""
    return (
        is_text_extension(file_path.suffix)
        or looks_like_sourceless_text(file_path.name)
    )


def _search_code(
    root: Path,
    query: str,
    path: str = ".",
    file_pattern: str | None = None,
    max_results: int = SEARCH_DEFAULT_RESULTS,
    use_regex: bool = False,
) -> dict[str, Any]:
    """Core pure-Python search implementation confined to *root*."""
    if query is None or not str(query).strip():
        return _failure("Empty search query.")
    needle = str(query)

    if use_regex:
        try:
            matcher = re.compile(needle, re.IGNORECASE)
        except re.error as exc:
            return _failure(f"Invalid regular expression: {exc}")
    else:
        haystack_probe = needle.lower()

    limit = int(max_results) if max_results else SEARCH_DEFAULT_RESULTS
    if limit < 1:
        limit = 1
    elif limit > SEARCH_MAX_RESULTS:
        limit = SEARCH_MAX_RESULTS

    scope, error = resolve_user_path(path, root)
    if error is not None:
        return _failure(error)

    if scope.is_file():
        if path_is_dangerous(scope, root, target_is_dir=False):
            return _failure("Access denied: cannot search this file.")
        if not _is_searchable_file(scope):
            return _failure("File type is not searchable (text sources only).")
        base_files: list[Path] | None = [scope]
        walk_root = scope.parent
    else:
        if path_is_dangerous(scope, root, target_is_dir=True):
            return _failure(
                "Access denied: cannot search inside an ignored or "
                "sensitive location."
            )
        if not scope.is_dir():
            return _failure(f"Search path not found: {relative_display(scope, root)}")
        base_files = None
        walk_root = scope

    pattern = (file_pattern or "*").lower()
    matches: list[dict[str, Any]] = []
    truncated = False
    files_scanned = 0

    candidates: list[Path]
    if base_files is not None:
        candidates = base_files
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(walk_root):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not is_ignored_dir_name(d) and not is_sensitive_dir_name(d)
            )
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if is_sensitive_file_name(fname):
                    continue
                if not _is_searchable_file(fpath):
                    continue
                candidates.append(fpath)

    for candidate in candidates:
        if truncated:
            break
        rel_posix = relative_display(candidate, root)
        if not fnmatch.fnmatch(rel_posix.lower(), pattern) and not fnmatch.fnmatch(
            candidate.name.lower(), pattern
        ):
            continue
        try:
            if candidate.stat().st_size > SEARCH_MAX_FILE_SIZE_BYTES:
                continue
            with open(candidate, "rb") as handle:
                if sniff_binary_content(handle.read(_READ_CHUNK)):
                    continue
            text = candidate.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue

        files_scanned += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            hit = (
                matcher.search(line) is not None
                if use_regex
                else haystack_probe in line.lower()
            )
            if not hit:
                continue
            snippet = line.strip()
            if len(snippet) > SNIPPET_MAX_CHARS:
                snippet = snippet[:SNIPPET_MAX_CHARS]
            matches.append({"path": rel_posix, "line": line_number, "text": snippet})
            if len(matches) >= limit:
                truncated = True
                break

    return {
        "ok": True,
        "query": needle,
        "use_regex": use_regex,
        "file_pattern": file_pattern,
        "files_scanned": files_scanned,
        "matches": matches,
        "match_count": len(matches),
        "max_results": limit,
        "truncated": truncated,
    }


@tool
def search_code(
    query: str,
    path: str = ".",
    file_pattern: str | None = None,
    max_results: int = SEARCH_DEFAULT_RESULTS,
    use_regex: bool = False,
) -> dict[str, Any]:
    """Search a text substring (or optional regex) across repository sources.

    Implemented in pure Python -- no grep, ripgrep, findstr or subprocess.
    Only whitelisted text/code extensions are scanned; binaries, caches
    (.git, .venv, __pycache__, node_modules, dist, build) and sensitive
    files are skipped automatically.

    Args:
        query: Text to find. Case-insensitive substring by default.
        path: Search scope relative to the repository root (default '.').
        file_pattern: Optional filename glob such as '*.py'.
        max_results: Maximum matches returned, clamped to 1-100 (default 50).
        use_regex: Treat 'query' as a regular expression (default False).

    Returns:
        Structured dict: {'ok', 'matches': [{'path','line','text'}],
        'match_count', 'files_scanned', 'truncated'} or {'ok': False,
        'error'}.
    """
    return _search_code(
        find_repo_root(),
        query,
        path=path,
        file_pattern=file_pattern,
        max_results=max_results,
        use_regex=use_regex,
    )


# ---------------------------------------------------------------------------
# Tool 4 - analyze_dependencies
# ---------------------------------------------------------------------------

_PY_REQUIREMENTS_FILES = ("requirements.txt", "requirements-dev.txt")


def _parse_pyproject(data: dict[str, Any]) -> dict[str, Any]:
    """Extract project metadata and dependencies from parsed TOML."""
    project = data.get("project", {}) or {}
    optional = project.get("optional-dependencies", {}) or {}
    dev_group = data.get("dependency-groups", {}) or {}
    entry: dict[str, Any] = {
        "type": "python-pyproject",
        "name": project.get("name"),
        "version": project.get("version"),
        "requires_python": project.get("requires-python"),
        "dependencies": list(project.get("dependencies") or []),
    }
    if isinstance(optional, dict) and optional:
        entry["optional_dependencies"] = {
            group: list(items or []) for group, items in optional.items()
        }
    if isinstance(dev_group, dict) and dev_group:
        entry["dependency_groups"] = {
            group: list(items or []) for group, items in dev_group.items()
        }
    return entry


def _parse_requirements(raw: str) -> list[str]:
    """Parse requirement specifiers from a requirements-style file body."""
    deps: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        before_comment = stripped.split("#", 1)[0].strip()
        if before_comment:
            deps.append(before_comment)
    return deps


def _parse_package_json(data: dict[str, Any]) -> dict[str, Any]:
    """Extract dependency sections from parsed package.json."""
    entry: dict[str, Any] = {
        "type": "node-package-json",
        "name": data.get("name"),
        "version": data.get("version"),
    }
    dependencies = data.get("dependencies")
    dev_dependencies = data.get("devDependencies")
    if isinstance(dependencies, dict):
        entry["dependencies"] = sorted(dependencies.keys())
    if isinstance(dev_dependencies, dict):
        entry["devDependencies"] = sorted(dev_dependencies.keys())
    return entry


def _analyze_dependencies(root: Path) -> dict[str, Any]:
    """Core static manifest analysis confined to *root*."""
    manifests: list[dict[str, Any]] = []

    pyproject_path = root / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            with open(pyproject_path, "rb") as handle:
                parsed = tomllib.load(handle)
            entry = _parse_pyproject(parsed)
        except tomllib.TOMLDecodeError as exc:
            entry = {
                "type": "python-pyproject",
                "parse_error": f"Malformed TOML: {exc}",
            }
        except OSError as exc:
            entry = {
                "type": "python-pyproject",
                "parse_error": f"Cannot read file: {exc}",
            }
        entry["path"] = "pyproject.toml"
        manifests.append(entry)

    for req_name in _PY_REQUIREMENTS_FILES:
        req_path = root / req_name
        if not req_path.is_file():
            continue
        try:
            body = req_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            manifests.append(
                {
                    "type": "python-requirements",
                    "path": req_name,
                    "parse_error": f"Cannot read file: {exc}",
                }
            )
            continue
        manifests.append(
            {
                "type": "python-requirements",
                "path": req_name,
                "dependencies": _parse_requirements(body),
            }
        )

    package_json_path = root / "package.json"
    if package_json_path.is_file():
        try:
            loaded = json.loads(package_json_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("package.json root must be a JSON object")
            entry = _parse_package_json(loaded)
        except json.JSONDecodeError as exc:
            entry = {
                "type": "node-package-json",
                "parse_error": f"Malformed JSON: {exc}",
            }
        except (OSError, ValueError) as exc:
            entry = {
                "type": "node-package-json",
                "parse_error": f"Cannot parse file: {exc}",
            }
        entry["path"] = "package.json"
        manifests.append(entry)

    return {
        "ok": True,
        "manifest_count": len(manifests),
        "manifests": manifests,
    }


@tool
def analyze_dependencies() -> dict[str, Any]:
    """Analyze dependency manifests present in the repository root.

    Recognizes pyproject.toml (parsed natively via Python's tomllib),
    requirements.txt, requirements-dev.txt and package.json. This is static
    analysis only: no installs, no network requests, no package managers.

    Returns:
        Structured dict: {'ok', 'manifests': [...]} where each manifest
        carries its 'type', 'path' and extracted dependency information
        (or a 'parse_error' description).
    """
    return _analyze_dependencies(find_repo_root())


# ---------------------------------------------------------------------------
# Public testable cores (explicit repository root; used by tests and smoke
# scripts. The @tool wrappers above resolve the root at call time.)
# ---------------------------------------------------------------------------


def list_directory_core(
    path: str = ".",
    depth: int = 1,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run :func:`list_directory` against an explicit repository root."""
    return _list_directory(root if root is not None else find_repo_root(), path, depth)


def read_file_core(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run :func:`read_file` against an explicit repository root."""
    return _read_file(
        root if root is not None else find_repo_root(),
        path,
        start_line,
        end_line,
    )


def search_code_core(
    query: str,
    path: str = ".",
    file_pattern: str | None = None,
    max_results: int = SEARCH_DEFAULT_RESULTS,
    use_regex: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run :func:`search_code` against an explicit repository root."""
    return _search_code(
        root if root is not None else find_repo_root(),
        query,
        path=path,
        file_pattern=file_pattern,
        max_results=max_results,
        use_regex=use_regex,
    )


def analyze_dependencies_core(root: Path | None = None) -> dict[str, Any]:
    """Run :func:`analyze_dependencies` against an explicit repository root."""
    return _analyze_dependencies(root if root is not None else find_repo_root())
