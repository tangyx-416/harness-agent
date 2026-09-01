"""Tests for git_diff: scope semantics, literal pathspec safety, limits."""

import subprocess

import pytest

from harness_agent.tools.git_tools import git_diff_core

from git_test_utils import commit_file, make_git_repo, run_git, write_file


@pytest.fixture()
def git_repo(tmp_path):
    repo = make_git_repo(tmp_path)
    commit_file(repo, "README.md", "line1\nline2\nline3\n", "initial")
    return repo


def _subprocess_spy(monkeypatch):
    calls = []
    real_popen = subprocess.Popen

    def spy(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("harness_agent.process.subprocess.Popen", spy)
    return calls


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


def test_working_diff(git_repo):
    write_file(git_repo, "README.md", "line1 changed\nline2\nline3\n")
    result = git_diff_core(root=git_repo)

    assert result["ok"] is True
    assert result["scope"] == "working"
    assert result["empty"] is False
    assert "+line1 changed" in result["diff"]
    assert result["context_lines"] == 3


def test_staged_diff(git_repo):
    write_file(git_repo, "README.md", "staged change\n")
    run_git(git_repo, "add", "--", "README.md")
    result = git_diff_core(scope="staged", root=git_repo)

    assert result["ok"] is True
    assert "+staged change" in result["diff"]


def test_head_diff_covers_staged_and_unstaged(git_repo):
    write_file(git_repo, "README.md", "final content\n")
    run_git(git_repo, "add", "--", "README.md")
    result = git_diff_core(scope="head", root=git_repo)

    assert result["ok"] is True
    assert "+final content" in result["diff"]


def test_path_filtered_diff(git_repo):
    commit_file(git_repo, "other.txt", "x\n", "second")
    write_file(git_repo, "README.md", "modified\n")
    write_file(git_repo, "other.txt", "modified too\n")
    result = git_diff_core(path="other.txt", root=git_repo)

    assert "other.txt" in result["diff"]
    assert "README.md" not in result["diff"]


def test_deleted_file_diff(git_repo):
    (git_repo / "README.md").unlink()
    result = git_diff_core(root=git_repo)

    assert result["ok"] is True
    assert "deleted file" in result["diff"]
    assert "README.md" in result["diff"]


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------


def test_context_lines_zero(git_repo):
    body = "\n".join(f"line{i}" for i in range(30)) + "\n"
    commit_file(git_repo, "big.txt", body, "big file")
    write_file(git_repo, "big.txt", body.replace("line15", "CHANGED"))
    result = git_diff_core(context_lines=0, path="big.txt", root=git_repo)

    assert result["context_lines"] == 0
    assert "+CHANGED" in result["diff"]
    assert "-line15" in result["diff"]
    # -U0: no unchanged context lines (context lines start with a space).
    context_lines_present = [
        ln for ln in result["diff"].splitlines()
        if ln.startswith(" ")
    ]
    assert context_lines_present == []


def test_context_lines_clamped(git_repo):
    write_file(git_repo, "README.md", "changed\n")

    result = git_diff_core(context_lines=99, root=git_repo)
    assert result["context_lines"] == 10

    result = git_diff_core(context_lines=-5, root=git_repo)
    assert result["context_lines"] == 0

    result = git_diff_core(context_lines="bogus", root=git_repo)
    assert result["context_lines"] == 3


def test_context_lines_in_argv(git_repo, monkeypatch):
    calls = _subprocess_spy(monkeypatch)
    write_file(git_repo, "README.md", "changed\n")
    git_diff_core(context_lines=7, root=git_repo)
    diff_call = [c for c in calls if "diff" in c["argv"]][-1]
    assert "-U7" in diff_call["argv"]


# ---------------------------------------------------------------------------
# Output limits
# ---------------------------------------------------------------------------


def test_large_diff_truncated(git_repo):
    big_old = "\n".join(f"old {i}" for i in range(40000)) + "\n"
    commit_file(git_repo, "large.txt", big_old, "big base")
    big_new = "\n".join(f"new {i}" for i in range(40000)) + "\n"
    write_file(git_repo, "large.txt", big_new)

    result = git_diff_core(path="large.txt", root=git_repo)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["diff"]) <= 128 * 1024


def test_two_mb_diff_parent_memory_bounded(git_repo):
    """~2 MB diff through the shared runner: parent retention stays capped."""
    huge_old = "\n".join(f"old {i}" for i in range(120000)) + "\n"
    commit_file(git_repo, "huge.txt", huge_old, "huge base")
    huge_new = "\n".join(f"new {i}" for i in range(120000)) + "\n"
    write_file(git_repo, "huge.txt", huge_new)

    result = git_diff_core(path="huge.txt", root=git_repo)

    assert result["ok"] is True
    assert result["truncated"] is True
    # Parent memory holds only the bounded prefix (result is not just
    # post-hoc trimmed -- the runner never buffered the whole diff).
    assert result["output_chars"] <= 128 * 1024
    assert len(result["diff"]) == result["output_chars"]


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------


def test_unicode_path(git_repo):
    commit_file(git_repo, "配置/模块.txt", "内容\n", "unicode commit")
    write_file(git_repo, "配置/模块.txt", "新内容\n")
    result = git_diff_core(path="配置/模块.txt", root=git_repo)

    assert result["ok"] is True
    assert "模块.txt" in result["diff"]


def test_path_with_spaces(git_repo):
    commit_file(git_repo, "docs/my note.txt", "x\n", "spaces commit")
    write_file(git_repo, "docs/my note.txt", "y\n")
    result = git_diff_core(path="docs/my note.txt", root=git_repo)

    assert result["ok"] is True
    assert "my note.txt" in result["diff"]


@pytest.mark.parametrize(
    "bad",
    ["../outside.txt", "../../etc/passwd", "C:\\Windows\\system.ini", "/etc/passwd"],
)
def test_escape_paths_denied(git_repo, bad):
    result = git_diff_core(path=bad, root=git_repo)
    assert result["ok"] is False
    assert result.get("denied") is True


def test_deleted_path_still_diffable(git_repo):
    """Deleted tracked files need no existence requirement."""
    commit_file(git_repo, "gone.txt", "x\n", "will be deleted")
    run_git(git_repo, "rm", "--", "gone.txt")
    result = git_diff_core(path="gone.txt", scope="staged", root=git_repo)

    assert result["ok"] is True
    assert "gone.txt" in result["diff"]


def test_literal_pathspec_magic_disabled(git_repo):
    """Pathspec magic like ':(glob)**' is treated literally (matches nothing)."""
    write_file(git_repo, "README.md", "changed\n")
    result = git_diff_core(path=":(glob)**", root=git_repo)

    assert result["ok"] is True
    assert result["empty"] is True


def test_untracked_not_in_diff(git_repo):
    write_file(git_repo, "untracked.txt", "brand new\n")
    result = git_diff_core(root=git_repo)

    assert result["empty"] is True


def test_binary_change_no_blob(git_repo):
    (git_repo / "logo.png").write_bytes(b"PNG\x00\x01\x02" + b"\x00" * 200)
    run_git(git_repo, "add", "--", "logo.png")
    (git_repo / "logo.png").write_bytes(b"PNG\x00\xff\xfe" + b"\x00" * 200)
    result = git_diff_core(path="logo.png", root=git_repo)

    assert result["ok"] is True
    assert "Binary files" in result["diff"]
    assert "\x00" not in result["diff"]


# ---------------------------------------------------------------------------
# External-process protection (argv contract)
# ---------------------------------------------------------------------------


def test_diff_argv_external_protection(git_repo, monkeypatch):
    calls = _subprocess_spy(monkeypatch)
    write_file(git_repo, "README.md", "changed\n")
    git_diff_core(scope="head", path="README.md", root=git_repo)

    diff_call = [c for c in calls if "diff" in c["argv"]][-1]
    argv = diff_call["argv"]
    assert "--no-ext-diff" in argv
    assert "--no-textconv" in argv
    assert "--no-color" in argv
    assert "--ignore-submodules=all" in argv
    assert "--literal-pathspecs" in argv
    # literal pathspec goes after the -- separator
    assert argv[-2] == "--"
    assert argv[-1] == "README.md"


def test_invalid_scope_denied(git_repo):
    result = git_diff_core(scope="HEAD~3", root=git_repo)
    assert result["ok"] is False
    assert "denied" in result
