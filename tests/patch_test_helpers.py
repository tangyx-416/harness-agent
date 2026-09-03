"""Shared helpers for v0.6.0 patch tests (disposable repo-like roots)."""

from __future__ import annotations

import os
from pathlib import Path


def make_root(tmp_path: Path) -> Path:
    """Return a disposable repository-like root with a pyproject marker."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return root.resolve()


def make_edit_file(root: Path, rel: str = "hello.py", text: str = 'x = "old"\n') -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def replacement(old: str, new: str) -> dict[str, str]:
    return {"old_text": old, "new_text": new}


def try_symlink(target: Path, link: Path) -> bool:
    """Create a symlink; return False when the platform forbids it."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError, TypeError):
        return False
