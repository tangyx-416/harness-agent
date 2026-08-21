"""Tests for project inspection tools."""

import tempfile
from pathlib import Path

import pytest

from harness_agent.tools.project_tools import inspect_project


def test_inspect_project_nonexistent_directory():
    """Test inspection of a non-existent directory."""
    result = inspect_project("/nonexistent/path/12345")

    assert result["exists"] is False
    assert "error" in result
    assert result["error"] == "Directory does not exist"


def test_inspect_project_empty_directory():
    """Test inspection of an empty directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = inspect_project(tmpdir)

        assert result["exists"] is True
        assert result["working_directory"] == str(Path(tmpdir).resolve())
        assert result["is_git_repo"] is False
        assert result["has_readme"] is False
        assert result["has_pyproject"] is False
        assert result["has_requirements"] is False
        assert result["top_level_items"] == []
        assert result["main_directories"] == []


def test_inspect_project_with_common_files():
    """Test inspection of a directory with common project files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create common files
        (tmppath / "README.md").touch()
        (tmppath / "pyproject.toml").touch()
        (tmppath / "requirements.txt").touch()
        (tmppath / ".git").mkdir()
        (tmppath / ".gitignore").touch()

        # Create some directories
        (tmppath / "src").mkdir()
        (tmppath / "tests").mkdir()
        (tmppath / "docs").mkdir()

        result = inspect_project(tmpdir)

        assert result["exists"] is True
        assert result["is_git_repo"] is True
        assert result["has_readme"] is True
        assert result["has_pyproject"] is True
        assert result["has_requirements"] is True

        # Check top-level items (hidden files except .git and .gitignore should be filtered)
        items = result["top_level_items"]
        assert ".git/" in items
        assert ".gitignore" in items
        assert "README.md" in items
        assert "pyproject.toml" in items
        assert "requirements.txt" in items
        assert "src/" in items
        assert "tests/" in items
        assert "docs/" in items


def test_inspect_project_identifies_main_directories():
    """Test that the tool identifies common source directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create directories that should be identified as main
        (tmppath / "src").mkdir()
        (tmppath / "lib").mkdir()
        (tmppath / "app").mkdir()
        (tmppath / "my_agent").mkdir()

        # Create directories that should not be identified
        (tmppath / "tests").mkdir()
        (tmppath / "docs").mkdir()
        (tmppath / ".hidden").mkdir()

        result = inspect_project(tmpdir)

        main_dirs = result["main_directories"]
        assert "src" in main_dirs
        assert "lib" in main_dirs
        assert "app" in main_dirs
        assert "my_agent" in main_dirs
        assert "tests" not in main_dirs
        assert "docs" not in main_dirs


def test_inspect_project_filters_sensitive_files():
    """Test that the tool filters out sensitive files and directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create sensitive files/dirs that should be filtered
        (tmppath / ".env").touch()
        (tmppath / "secret.txt").touch()
        (tmppath / "credentials.json").touch()
        (tmppath / "api_key.txt").touch()
        (tmppath / ".ssh").mkdir()

        # Create normal files
        (tmppath / "README.md").touch()
        (tmppath / "config.yaml").touch()

        result = inspect_project(tmpdir)

        items = result["top_level_items"]

        # Sensitive items should be filtered out
        assert not any(".env" in item for item in items)
        assert not any("secret" in item.lower() for item in items)
        assert not any("credential" in item.lower() for item in items)
        assert not any("key" in item.lower() for item in items)
        assert not any(".ssh" in item for item in items)

        # Normal files should be present
        assert "README.md" in items
        assert "config.yaml" in items


def test_inspect_project_default_current_directory():
    """Test that the tool uses current directory by default."""
    # This test just verifies the function can be called with no arguments
    result = inspect_project()

    assert result["exists"] is True
    assert "working_directory" in result
    assert Path(result["working_directory"]).exists()


def test_inspect_project_various_readme_formats():
    """Test that the tool recognizes different README formats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Test README.md
        (tmppath / "README.md").touch()
        result = inspect_project(tmpdir)
        assert result["has_readme"] is True

        # Clean up and test README.rst
        (tmppath / "README.md").unlink()
        (tmppath / "README.rst").touch()
        result = inspect_project(tmpdir)
        assert result["has_readme"] is True

        # Clean up and test plain README
        (tmppath / "README.rst").unlink()
        (tmppath / "README").touch()
        result = inspect_project(tmpdir)
        assert result["has_readme"] is True
