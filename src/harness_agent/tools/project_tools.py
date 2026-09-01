"""Project inspection tool for analyzing the current repository.

v0.3.0 security update: ``inspect_project`` now enforces the same
repository-root confinement policy as every other repository tool via
``path_utils``. Requests that resolve outside the repository root
(``..``, absolute external paths, ``/etc``, ``C:\\Users`` ...) are
refused with a structured ``denied`` error instead of being inspected.
"""

from pathlib import Path
from typing import Any

from .path_utils import (
    find_repo_root,
    path_is_dangerous,
    resolve_user_path,
)


def inspect_project(directory: str = ".", root: Path | None = None) -> dict[str, Any]:
    """Inspect a directory inside the repository and return basic information.

    This is a read-only tool that analyzes the project directory structure,
    checks for common project files, and provides an overview of the folder.

    Args:
        directory: Directory to inspect, relative to the repository root
            (default: repository root).
        root: Repository root used for confinement. Auto-discovered when
            omitted (tests may pass an explicit root).

    Returns:
        Dictionary containing project information:
        - working_directory: Absolute path to the inspected directory
        - exists: Whether the directory exists
        - denied: True when the request was refused by the path policy
        - is_git_repo: Whether .git directory exists
        - has_readme: Whether README file exists
        - has_pyproject: Whether pyproject.toml exists
        - has_requirements: Whether requirements.txt exists
        - top_level_items: List of files and directories in the root
        - main_directories: List of directories that might contain source code

    Safety:
        - Read-only operation
        - Confined to the repository root (path traversal is refused)
        - Does not read file contents
        - Filters ignored directories (.venv, .git internals...) and
          sensitive names (.env, credentials, keys...) from listings
        - Does not execute any commands
        - Does not modify any files
    """
    boundary = root if root is not None else find_repo_root()
    target, error = resolve_user_path(directory, boundary)
    if error is not None:
        return {
            "working_directory": str(directory),
            "exists": False,
            "denied": True,
            "error": error,
        }

    if path_is_dangerous(target, boundary, target_is_dir=True):
        return {
            "working_directory": str(directory),
            "exists": False,
            "denied": True,
            "error": (
                "Access denied: this path points to an ignored or sensitive "
                "location and cannot be inspected."
            ),
        }

    # Check if directory exists
    if not target.exists():
        return {
            "working_directory": str(target),
            "exists": False,
            "error": "Directory does not exist",
        }

    # Check for git repository
    is_git_repo = (target / ".git").exists()

    # Check for common project files
    has_readme = any(
        (target / name).exists()
        for name in ["README.md", "README.rst", "README.txt", "README"]
    )
    has_pyproject = (target / "pyproject.toml").exists()
    has_requirements = (target / "requirements.txt").exists()

    # Get top-level items (excluding hidden files and sensitive items)
    top_level_items = []
    sensitive_patterns = {".env", "secret", "credential", "key", "token", ".ssh"}

    try:
        for item in sorted(target.iterdir()):
            # Skip hidden files (except .git, .gitignore which are safe to mention)
            if item.name.startswith(".") and item.name not in {".git", ".gitignore"}:
                continue

            # Skip sensitive items
            if any(pattern in item.name.lower() for pattern in sensitive_patterns):
                continue

            # Add type indicator
            if item.is_dir():
                top_level_items.append(f"{item.name}/")
            else:
                top_level_items.append(item.name)

    except PermissionError:
        top_level_items = ["<permission denied>"]

    # Identify main directories (common source code directories)
    main_directories = []
    common_src_dirs = {"src", "lib", "app", "pkg", "core", "modules", "packages"}

    for item in target.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            if item.name in common_src_dirs or item.name.endswith("_agent"):
                main_directories.append(item.name)

    return {
        "working_directory": str(target),
        "exists": True,
        "is_git_repo": is_git_repo,
        "has_readme": has_readme,
        "has_pyproject": has_pyproject,
        "has_requirements": has_requirements,
        "top_level_items": top_level_items,
        "main_directories": main_directories,
    }
