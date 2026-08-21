"""Project inspection tools for analyzing the current repository."""

import os
from pathlib import Path
from typing import Any


def inspect_project(directory: str = ".") -> dict[str, Any]:
    """Inspect the current project structure and return basic information.

    This is a read-only tool that analyzes the project directory structure,
    checks for common project files, and provides an overview of the repository.

    Args:
        directory: Directory to inspect (default: current directory)

    Returns:
        Dictionary containing project information:
        - working_directory: Absolute path to the inspected directory
        - exists: Whether the directory exists
        - is_git_repo: Whether .git directory exists
        - has_readme: Whether README file exists
        - has_pyproject: Whether pyproject.toml exists
        - has_requirements: Whether requirements.txt exists
        - top_level_items: List of files and directories in the root
        - main_directories: List of directories that might contain source code

    Safety:
        - Read-only operation
        - Does not read file contents
        - Does not access .env or credential files
        - Does not execute any commands
        - Does not modify any files
    """
    target_path = Path(directory).resolve()

    # Check if directory exists
    if not target_path.exists():
        return {
            "working_directory": str(target_path),
            "exists": False,
            "error": "Directory does not exist",
        }

    # Check for git repository
    is_git_repo = (target_path / ".git").exists()

    # Check for common project files
    has_readme = any(
        (target_path / name).exists()
        for name in ["README.md", "README.rst", "README.txt", "README"]
    )
    has_pyproject = (target_path / "pyproject.toml").exists()
    has_requirements = (target_path / "requirements.txt").exists()

    # Get top-level items (excluding hidden files and sensitive items)
    top_level_items = []
    sensitive_patterns = {".env", "secret", "credential", "key", "token", ".ssh"}

    try:
        for item in sorted(target_path.iterdir()):
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

    for item in target_path.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            if item.name in common_src_dirs or item.name.endswith("_agent"):
                main_directories.append(item.name)

    return {
        "working_directory": str(target_path),
        "exists": True,
        "is_git_repo": is_git_repo,
        "has_readme": has_readme,
        "has_pyproject": has_pyproject,
        "has_requirements": has_requirements,
        "top_level_items": top_level_items,
        "main_directories": main_directories,
    }
