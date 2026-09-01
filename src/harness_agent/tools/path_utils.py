"""Shared path-safety utilities for the Repository Read Layer.

This module centralizes the security model used by every repository tool:

1. Repository root discovery
2. User-path resolution confined to the repository root (anti path-traversal)
3. Sensitive-path policy (explicit names/extensions, no naive substring match)
4. Ignored-directory policy (build artifacts, caches, VCS internals)
5. Binary-vs-text classification helpers

All repository tools MUST go through these helpers so that a resolved path
can never escape the repository root, regardless of what the model requests.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Output / safety limits (single source of truth)
# ---------------------------------------------------------------------------

#: Maximum recursion depth for ``list_directory``.
LIST_MAX_DEPTH = 3
#: Maximum number of entries returned by one ``list_directory`` call.
LIST_MAX_ENTRIES = 200
#: Maximum file size accepted by ``read_file`` (256 KB).
READ_MAX_FILE_SIZE_BYTES = 256 * 1024
#: Maximum number of lines returned by one ``read_file`` call.
READ_MAX_LINES_PER_CALL = 400
#: Default number of matches returned by ``search_code``.
SEARCH_DEFAULT_RESULTS = 50
#: Hard upper bound for ``search_code.max_results``.
SEARCH_MAX_RESULTS = 100
#: Files larger than this are skipped entirely by ``search_code``.
SEARCH_MAX_FILE_SIZE_BYTES = 512 * 1024
#: Maximum characters shown per search result snippet.
SNIPPET_MAX_CHARS = 200

# ---------------------------------------------------------------------------
# Repository root discovery
# ---------------------------------------------------------------------------

#: Markers used to identify the repository root.
REPO_ROOT_MARKERS = (".git", "pyproject.toml")


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upward from *start* until a repository marker is found.

    Args:
        start: Directory to start the search from (default: current working
            directory).

    Returns:
        Absolute path to the discovered repository root. Falls back to the
        starting directory itself when no marker exists above it, so callers
        always receive a usable confinement boundary.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in REPO_ROOT_MARKERS):
            return candidate
    return current


# ---------------------------------------------------------------------------
# Ignored directories (never browsed, searched, or read)
# ---------------------------------------------------------------------------

IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "node_modules",
        "dist",
        "build",
    }
)

_IGNORED_DIR_SUFFIXES = (".egg-info",)


def is_ignored_dir_name(name: str) -> bool:
    """Return True when *name* refers to a build/cache/VCS directory."""
    lowered = name.lower()
    return lowered in IGNORED_DIR_NAMES or lowered.endswith(_IGNORED_DIR_SUFFIXES)


# ---------------------------------------------------------------------------
# Sensitive-path policy
#
# Rules are based on explicit file names, directory names, or file
# extensions -- deliberately NOT raw substring matching, so that normal
# source files such as ``secretary.py`` or ``tokenizer.py`` are never
# misclassified.
# ---------------------------------------------------------------------------

#: Directory names that may never be traversed into.
SENSITIVE_DIR_NAMES = frozenset({".ssh", ".aws", ".gnupg", "secrets", "credentials"})
#: File names blocked outright (case-insensitive).
SENSITIVE_FILE_NAMES = frozenset({".env", "id_rsa", "id_ed25519"})
#: Allowed exception: public configuration template.
ALLOWED_SENSITIVE_EXCEPTIONS = frozenset({".env.example"})
#: Extensions treated as private key / certificate material.
SENSITIVE_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})
#: Stems (filename without extension) indicating credential bundles.
SENSITIVE_STEMS = frozenset({"credentials", "credential", "secrets", "secret"})
#: Prefixes for SSH key pairs (covers id_rsa.pub etc.).
SENSITIVE_NAME_PREFIXES = ("id_rsa.", "id_ed25519.")
#: Token artifacts blocked by explicit name/stem so ordinary source files
#: such as ``tokenizer.py`` are never affected.
SENSITIVE_TOKEN_NAMES = frozenset({"token"})
_SENSITIVE_TOKEN_PREFIXES = ("token.",)


def is_sensitive_file_name(name: str) -> bool:
    """Check a file base name against the sensitive-path policy.

    Based on explicit names/extensions only -- deliberately no raw substring
    matching, so normal source files like ``secretary.py`` pass through.
    """
    lowered = name.lower()

    if lowered in ALLOWED_SENSITIVE_EXCEPTIONS:
        return False

    if lowered == ".env" or lowered.startswith(".env."):
        return True

    if lowered in SENSITIVE_FILE_NAMES or lowered.startswith(SENSITIVE_NAME_PREFIXES):
        return True

    if lowered in SENSITIVE_TOKEN_NAMES or lowered.startswith(_SENSITIVE_TOKEN_PREFIXES):
        return True

    lowered_ext = Path(lowered).suffix
    if lowered_ext in SENSITIVE_EXTENSIONS:
        return True

    # Stem comparison: ``secrets.json`` -> ``secrets``.
    stem = lowered.rsplit(".", 1)[0] if "." in lowered[1:] else lowered
    return stem in SENSITIVE_STEMS


def is_sensitive_dir_name(name: str) -> bool:
    """Check a directory component against the sensitive-path policy."""
    return name.lower() in SENSITIVE_DIR_NAMES


def path_is_dangerous(path: Path, root: Path, *, target_is_dir: bool) -> bool:
    """Return True when any component of *path* (relative to *root*) is
    ignored/sensitive -- checked BEFORE touching the filesystem.
    """
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True

    if not rel_parts:
        # The path IS the repository root itself -- always safe.
        return False

    if target_is_dir:
        for part in rel_parts:
            if is_ignored_dir_name(part) or is_sensitive_dir_name(part):
                return True
        return False

    for part in rel_parts[:-1]:
        if is_ignored_dir_name(part) or is_sensitive_dir_name(part):
            return True
    return is_ignored_dir_name(rel_parts[-1]) or is_sensitive_file_name(rel_parts[-1])


# ---------------------------------------------------------------------------
# Text vs binary classification
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyw",
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".toml",
        ".cfg",
        ".ini",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".vue",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".hh",
        ".cs",
        ".swift",
        ".kt",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".psm1",
        ".bat",
        ".cmd",
        ".sql",
        ".graphql",
        ".dockerfile",
        ".csv",
        ".tsv",
        ".log",
    }
)

BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".ico",
        ".icns",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".a",
        ".lib",
        ".obj",
        ".o",
        ".pyc",
        ".pyo",
        ".class",
        ".jar",
        ".war",
        ".whl",
        ".egg",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        ".mp3",
        ".wav",
        ".flac",
        ".mp4",
        ".avi",
        ".mkv",
        ".mov",
        ".webm",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".bin",
        ".dat",
        ".iso",
        ".dmg",
    }
)

_SOURCE_LIKE_EXTRA_NAMES = frozenset({"makefile", "dockerfile", "license", "notice"})


def is_text_extension(suffix: str) -> bool:
    """Return True when *suffix* (e.g. ``.py``) is a known text/code type."""
    return suffix.lower() in TEXT_EXTENSIONS


def is_known_binary_extension(suffix: str) -> bool:
    """Return True when *suffix* maps to a known binary asset type."""
    return suffix.lower() in BINARY_EXTENSIONS


def looks_like_sourceless_text(name: str) -> bool:
    """Heuristic for extension-less files such as ``Makefile``/``LICENSE``."""
    return name.lower() in _SOURCE_LIKE_EXTRA_NAMES


def sniff_binary_content(data: bytes) -> bool:
    """Cheap heuristic: NUL byte presence indicates binary payload."""
    return b"\x00" in data[:8192]


# ---------------------------------------------------------------------------
# Path resolution (repository-root confinement)
# ---------------------------------------------------------------------------


def resolve_user_path(user_path: str, root: Path | None = None) -> tuple[Path | None, str | None]:
    """Resolve *user_path* against *root*, enforcing repository confinement.

    Absolute paths, ``..`` segments, drive letters, and UNC paths are all
    tolerated as *input* but always validated afterwards: if the resolved
    location falls outside the repository root, the request is refused even
    when the target file physically exists.

    Args:
        user_path: User/model supplied path, absolute or relative.
        root: Repository root to confine resolution to (default: auto-discovered).

    Returns:
        ``(resolved_path, None)`` on success, or ``(None, error_reason)``
        when the request must be refused.
    """
    if root is None:
        root = find_repo_root()
    root = root.resolve()

    raw = (user_path or "").strip()
    if raw in ("", "."):
        return root, None

    if "\x00" in raw:
        return None, "invalid path"

    try:
        resolved = (root / raw).resolve(strict=False)
    except (OSError, ValueError):
        return None, "invalid path"

    try:
        resolved.relative_to(root)
    except ValueError:
        return None, "path escapes repository root"

    return resolved, None


def relative_display(path: Path, root: Path) -> str:
    """Render *path* relative to *root* using POSIX separators."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
