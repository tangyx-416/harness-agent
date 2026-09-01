"""Shared helpers for Git Awareness tests.

Git mutation commands (init/add/commit/branch/...) are used ONLY inside
disposable ``tmp_path`` repositories to build fixtures -- they are test
infrastructure, never Agent capability. No global git config is touched
(identity is configured locally per repo) and no network is used.
"""

import subprocess

TEST_USER = "Test User"
TEST_EMAIL = "test@example.invalid"


def run_git(root, *args, check=True):
    """Run git against a disposable test repository."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def make_git_repo(tmp_path, branch="main"):
    """Create an empty repository with local-only identity config."""
    run_git(tmp_path, "init", "-b", branch)
    run_git(tmp_path, "config", "user.name", TEST_USER)
    run_git(tmp_path, "config", "user.email", TEST_EMAIL)
    run_git(tmp_path, "config", "commit.gpgsign", "false")
    run_git(tmp_path, "config", "core.autocrlf", "false")
    return tmp_path


def write_file(root, relpath, content):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def commit_file(root, relpath, content, message):
    """Write, stage and commit one file (disposable repo only)."""
    write_file(root, relpath, content)
    run_git(root, "add", "--", relpath)
    run_git(root, "commit", "-m", message)
    return root / relpath
