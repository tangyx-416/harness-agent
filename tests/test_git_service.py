"""Tests for the Git read service: environment hardening, repository
boundary, hardened argv contract, and shared bounded-runner reuse."""

import subprocess
from pathlib import Path

import pytest

import os
import subprocess
from pathlib import Path

import pytest

from harness_agent.git_awareness import (
    ALLOWED_GIT_SUBCOMMANDS,
    build_git_environment,
)
from harness_agent.git_awareness.service import _BASE_GIT_FLAGS, find_git_executable
from harness_agent.tools.git_tools import (
    git_branches_core,
    git_diff_core,
    git_log_core,
    git_status_core,
)

from git_test_utils import make_git_repo


@pytest.fixture()
def git_repo(tmp_path):
    return make_git_repo(tmp_path)


class RecordingPopen:
    """Spy that wraps the real Popen: records argv/env, delegates for real."""

    calls = []

    def install(self, monkeypatch):
        RecordingPopen.calls = []
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            RecordingPopen.calls.append({"argv": argv, "kwargs": kwargs})
            return real_popen(argv, **kwargs)

        monkeypatch.setattr("harness_agent.process.subprocess.Popen", spy)
        return RecordingPopen.calls


@pytest.fixture()
def git_spy(monkeypatch):
    return RecordingPopen().install(monkeypatch)


# ---------------------------------------------------------------------------
# Environment hardening
# ---------------------------------------------------------------------------


def test_inherited_git_vars_all_scrubbed():
    parent = {
        "PATH": "/usr/bin",
        "GIT_DIR": "C:/elsewhere/.git",
        "GIT_WORK_TREE": "C:/elsewhere",
        "GIT_INDEX_FILE": "C:/elsewhere/index",
        "GIT_EXEC_PATH": "C:/fake",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.editor",
        "GIT_CONFIG_VALUE_0": "evil",
        "GIT_EXTERNAL_DIFF": "evil-diff.sh",
        "GIT_SSH_COMMAND": "evil-ssh",
        "GIT_ASKPASS": "evil-askpass",
        "SSH_ASKPASS": "evil-askpass",
        "OPENAI_API_KEY": "fake-secret",
    }
    env = build_git_environment(parent)

    for key in parent:
        if key != "PATH":
            assert key not in env, key
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env


def test_host_sets_safe_git_values():
    env = build_git_environment({"PATH": "x"})
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_ATTR_NOSYSTEM"] == "1"
    assert env["PAGER"] == "cat"


def test_git_env_keeps_base_hardening(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p evil")
    env = build_git_environment()  # from real os.environ
    assert "OPENAI_API_KEY" not in env
    assert "PYTEST_ADDOPTS" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_real_git_child_env_verified(git_repo, git_spy, monkeypatch):
    """Real child: injected GIT_DIR/OPENAI_API_KEY absent from child env."""
    monkeypatch.setenv("GIT_DIR", "C:/injected/.git")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret-value")

    result = git_status_core(root=git_repo)
    assert result["ok"] is True

    env = git_spy[-1]["kwargs"]["env"]
    assert "GIT_DIR" not in env
    assert "OPENAI_API_KEY" not in env
    assert env.get("GIT_PAGER") == "cat"
    assert "PATH" in env


# ---------------------------------------------------------------------------
# Hardened argv contract
# ---------------------------------------------------------------------------


def test_base_argv_hardening_flags(git_repo, git_spy):
    git_status_core(root=git_repo)
    argv = git_spy[-1]["argv"]
    expected_prefix = [find_git_executable(), *_BASE_GIT_FLAGS]
    assert argv[: len(expected_prefix)] == expected_prefix
    assert argv[len(expected_prefix)] == "status"


def test_all_git_operations_use_fixed_subcommands(git_repo, git_spy):
    from git_test_utils import commit_file

    commit_file(git_repo, "f.txt", "x", "c1")

    git_status_core(root=git_repo)
    git_diff_core(root=git_repo)
    git_log_core(root=git_repo)
    git_branches_core(root=git_repo)

    hardened_prefix = [find_git_executable(), *_BASE_GIT_FLAGS]
    for call in git_spy:
        argv = call["argv"]
        if argv[: len(hardened_prefix)] != hardened_prefix:
            continue  # not a harness-agent-initiated Git call (e.g. fixture git)
        subcommand = argv[len(hardened_prefix)]
        assert subcommand in ALLOWED_GIT_SUBCOMMANDS, subcommand
        joined = " ".join(argv)
        for verb in ("fetch", "push", "pull", "clone", "ls-remote"):
            assert f" {verb} " not in joined, verb


def test_model_cannot_control_global_flags(git_repo, git_spy):
    """Subcommand position is fixed; no model data lands before it."""
    git_log_core(limit=1, path="f.txt", root=git_repo)
    argv = git_spy[-1]["argv"]
    base_len = 1 + len(_BASE_GIT_FLAGS)
    assert argv[base_len] == "log"
    assert "--" in argv
    # The path goes last, after the -- separator.
    assert argv[-2] == "--"
    assert argv[-1] == "f.txt"


# ---------------------------------------------------------------------------
# Repository boundary
# ---------------------------------------------------------------------------


def test_git_executable_missing_is_structured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "harness_agent.git_awareness.service.find_git_executable", lambda: None
    )
    result = git_status_core(root=tmp_path)
    assert result == {"ok": False, "error": "Git executable was not found."}


def test_non_git_repo_is_structured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "harness_agent.git_awareness.service.find_git_executable", find_git_executable
    )
    for tool in (git_status_core, git_log_core, git_branches_core):
        result = tool(root=tmp_path)
        assert result["ok"] is False
        assert "not a Git working tree" in result["error"]


def test_git_root_mismatch_refused(tmp_path, monkeypatch):
    """Real git root (tmp_path) != configured root (tmp_path/sub) -> refuse."""
    make_git_repo(tmp_path)
    inner = tmp_path / "sub"
    inner.mkdir()

    result = git_status_core(root=inner)
    assert result["ok"] is False
    assert "differs from the configured repository root" in result["error"]


def test_worktree_git_file_supported(tmp_path, monkeypatch):
    """A linked worktree (.git FILE) is recognized as the git root."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = make_git_repo(repo_dir)
    from git_test_utils import commit_file

    commit_file(repo, "seed.txt", "1", "seed")
    wt = tmp_path / "wt"
    run_result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt-branch", str(wt)],
        capture_output=True, text=True,
    )
    assert run_result.returncode == 0, run_result.stderr

    result = git_status_core(root=wt)
    assert result["ok"] is True, result
    assert result["branch"] == "wt-branch"


# ---------------------------------------------------------------------------
# Shared bounded runner reuse
# ---------------------------------------------------------------------------


def test_git_runs_through_shared_bounded_runner(git_repo, git_spy):
    """Git output goes through harness_agent.process (bounded runner)."""
    import harness_agent.process as proc

    assert hasattr(proc, "run_bounded")
    assert hasattr(proc, "_BoundedStore")
    # The spy proves invocations flow through process.subprocess.Popen,
    # i.e. the same bounded streaming runner as v0.3 execution.
    git_log_core(root=git_repo)
    assert git_spy
    kwargs = git_spy[-1]["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_git_timeout_is_fixed_thirty_seconds(git_repo, monkeypatch):
    """The model can never influence the Git timeout (fixed host value)."""
    from harness_agent import process as proc
    from harness_agent.git_awareness import service as git_service

    captured = {}
    real_run_bounded = proc.run_bounded

    def spy_run_bounded(argv, **kwargs):
        captured.update(kwargs)
        return real_run_bounded(argv, **kwargs)

    monkeypatch.setattr(git_service, "run_bounded", spy_run_bounded)
    git_status_core(root=git_repo)

    assert captured["timeout"] == git_service.GIT_TIMEOUT_SECONDS == 30


def test_git_output_limits_bounded(git_repo, monkeypatch):
    """Git stdout/stderr limits (128 KB / 32 KB) reach the shared runner."""
    from harness_agent import process as proc
    from harness_agent.git_awareness import service as git_service

    captured = {}
    real_run_bounded = proc.run_bounded

    def spy_run_bounded(argv, **kwargs):
        captured.update(kwargs)
        return real_run_bounded(argv, **kwargs)

    monkeypatch.setattr(git_service, "run_bounded", spy_run_bounded)
    git_diff_core(root=git_repo)

    assert captured["stdout_limit"] == git_service.GIT_STDOUT_LIMIT == 128 * 1024
    assert captured["stderr_limit"] == git_service.GIT_STDERR_LIMIT == 32 * 1024
