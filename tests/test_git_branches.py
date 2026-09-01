"""Tests for git_branches: local refs, remote-tracking refs, no network."""

import subprocess

import pytest

from harness_agent.git_awareness.parsers import parse_branches
from harness_agent.tools.git_tools import git_branches_core

from git_test_utils import commit_file, make_git_repo, run_git


@pytest.fixture()
def git_repo(tmp_path):
    repo = make_git_repo(tmp_path)
    commit_file(repo, "README.md", "x\n", "initial")
    return repo


def test_current_local_branch(git_repo):
    result = git_branches_core(root=git_repo)

    assert result["ok"] is True
    assert result["current"] == "main"
    assert result["detached"] is False
    assert result["unborn"] is False
    current = [b for b in result["branches"] if b["is_current"]]
    assert len(current) == 1
    assert current[0]["name"] == "main"
    assert current[0]["kind"] == "local"


def test_multiple_local_branches(git_repo):
    run_git(git_repo, "branch", "feature/one")
    run_git(git_repo, "branch", "feature/two")
    result = git_branches_core(root=git_repo)

    names = {b["name"] for b in result["branches"]}
    assert {"main", "feature/one", "feature/two"} <= names
    current = [b for b in result["branches"] if b["is_current"]]
    assert len(current) == 1 and current[0]["name"] == "main"


def test_include_remote_false_excludes_remotes(git_repo):
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    result = git_branches_core(root=git_repo)

    assert result["ok"] is True
    assert all(b["kind"] == "local" for b in result["branches"])


def test_include_remote_reads_local_tracking_refs_only(git_repo):
    """refs/remotes are local metadata; no network call happens."""
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    result = git_branches_core(include_remote=True, root=git_repo)

    remote_entries = [b for b in result["branches"] if b["kind"] == "remote"]
    assert len(remote_entries) == 1
    assert remote_entries[0]["name"] == "origin/main"
    assert remote_entries[0]["is_current"] is False


def test_detached_head(git_repo):
    run_git(git_repo, "checkout", "--detach")
    result = git_branches_core(root=git_repo)

    assert result["ok"] is True
    assert result["detached"] is True
    assert result["current"] is None
    assert all(b["is_current"] is False for b in result["branches"])


def test_branch_cap_synthetic():
    lines = [
        f"refs/heads/branch{i}\x1f{'a' * 40}\x1f "
        for i in range(250)
    ]
    branches = parse_branches("\n".join(lines))
    assert len(branches) == 250  # parser itself does not cap


def test_branch_framing_safe_for_legal_refnames():
    """\\x1f cannot occur in legal refnames (git-check-ref-format forbids
    control characters 0x00-0x1F and spaces), so the framing cannot be
    collided with by branch data; malformed lines are skipped."""
    from harness_agent.git_awareness.parsers import FIELD_SEP

    tricky_but_legal = [
        "feature/a-b_c.d",
        "release/v1.0.0-rc1",
        "特性/分支",
        "user/hero+plus",
    ]
    raw = "\n".join(
        f"refs/heads/{name}{FIELD_SEP}{'b' * 40}{FIELD_SEP} "
        for name in tricky_but_legal
    )
    branches = parse_branches(raw)
    assert [b.name for b in branches] == tricky_but_legal

    # Malformed lines (missing separators) are skipped, never raise.
    garbage = "no-separators-here\nrefs/heads/ok" + FIELD_SEP + "b" * 40
    assert parse_branches(garbage) == []


def test_branch_cap_enforced_by_service(git_repo, monkeypatch):
    """The service caps returned branches at 200 and flags truncation."""
    from harness_agent.git_awareness import service as git_service
    from harness_agent.process import ProcessOutcome

    def fake_run_git(exe, args, root):
        subcommand = args[0]
        if subcommand == "for-each-ref":
            lines = [
                f"refs/heads/b{i}\x1f{'a' * 40}\x1f " for i in range(250)
            ]
            return ProcessOutcome(
                exit_code=0, stdout="\n".join(lines), stderr="",
                stdout_truncated=False, stderr_truncated=False, timed_out=False,
            )
        if subcommand == "rev-parse":
            if "--abbrev-ref" in args:
                return ProcessOutcome(
                    exit_code=0, stdout="main\n", stderr="",
                    stdout_truncated=False, stderr_truncated=False, timed_out=False,
                )
            # --show-toplevel: the boundary check must see the real root.
            return ProcessOutcome(
                exit_code=0, stdout=f"{root}\n", stderr="",
                stdout_truncated=False, stderr_truncated=False, timed_out=False,
            )
        raise AssertionError(f"unexpected subcommand: {subcommand}")

    monkeypatch.setattr(git_service, "_run_git", fake_run_git)

    result = git_branches_core(root=git_repo)
    assert result["ok"] is True
    assert len(result["branches"]) == 200
    assert result["truncated"] is True


def test_unicode_branch_name(git_repo):
    run_git(git_repo, "branch", "特性/分支")
    result = git_branches_core(root=git_repo)
    assert "特性/分支" in {b["name"] for b in result["branches"]}


def test_no_network_invocation(git_repo, monkeypatch):
    """The composed argv never contains network subcommands."""
    calls = []
    real_popen = subprocess.Popen

    def spy(argv, **kwargs):
        calls.append(argv)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("harness_agent.process.subprocess.Popen", spy)
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git_branches_core(include_remote=True, root=git_repo)

    for argv in calls:
        joined = " ".join(argv)
        for verb in ("fetch", "pull", "push", "clone", "ls-remote"):
            assert f" {verb} " not in joined, verb


def test_unborn_repository_branches(tmp_path):
    repo = make_git_repo(tmp_path)
    result = git_branches_core(root=repo)

    assert result["ok"] is True
    assert result["unborn"] is True
    assert result["current"] is None
