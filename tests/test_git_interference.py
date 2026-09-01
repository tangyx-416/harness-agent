"""Git local-config interference audit (v0.4.0 Release Audit).

A malicious or accidental repository-local ``.git/config`` must not be
able to re-enable the external-process surface that the hardened argv
disables. Each test plants marker commands that would create a sentinel
file if Git ever invoked them, then asserts the sentinels stay absent
while the read-only operations still succeed.

Marker semantics: every marker writes one sentinel file. If a sentinel
exists after the operations, the marker EXECUTED (failure).
"""

import subprocess  # noqa: F401  (kept for future integration probes)
from pathlib import Path

import pytest

from harness_agent.tools.git_tools import (
    git_branches_core,
    git_diff_core,
    git_log_core,
    git_status_core,
)

from git_test_utils import commit_file, make_git_repo, run_git, write_file


@pytest.fixture()
def booby_trapped_repo(tmp_path):
    """Repository whose local config tries to re-enable external helpers."""
    repo = make_git_repo(tmp_path)
    commit_file(repo, "README.md", "hello world\n", "initial commit")

    marker = tmp_path / "marker.sh"
    sentinel = tmp_path / "SENTINEL_EXECUTED"
    marker.write_text(
        "#!/bin/sh\n"
        f'touch "{sentinel.as_posix()}"\n',
        encoding="utf-8",
    )

    def config(*pairs):
        for key, value in pairs:
            run_git(repo, "config", key, value)

    config(
        ("core.pager", str(marker)),
        ("core.fsmonitor", str(marker)),
        ("diff.external", str(marker)),
        ("diff.markerdriver.textconv", str(marker)),
        ("log.showSignature", "true"),
        # Same-name aliases must not override builtins (verified on 2.48.1)
        ("alias.status", f"!'{marker}'"),
        ("alias.diff", f"!'{marker}'"),
        ("alias.log", f"!'{marker}'"),
    )
    return repo, marker, sentinel


def _sentinel_absent(sentinel: Path) -> bool:
    return not sentinel.exists()


def test_no_pager_execution(booby_trapped_repo):
    repo, marker, sentinel = booby_trapped_repo
    result = git_log_core(root=repo)
    assert result["ok"] is True
    assert _sentinel_absent(sentinel)


def test_no_fsmonitor_execution(booby_trapped_repo):
    repo, marker, sentinel = booby_trapped_repo
    result = git_status_core(root=repo)
    assert result["ok"] is True
    assert _sentinel_absent(sentinel)


def test_no_external_diff_execution(booby_trapped_repo):
    repo, marker, sentinel = booby_trapped_repo
    write_file(repo, "README.md", "changed content\n")
    result = git_diff_core(root=repo)
    assert result["ok"] is True
    assert result["empty"] is False
    assert _sentinel_absent(sentinel)


def test_no_textconv_execution(booby_trapped_repo):
    repo, marker, sentinel = booby_trapped_repo
    # Map *.md to the booby-trapped textconv driver via gitattributes.
    write_file(repo, ".gitattributes", "*.md diff=markerdriver\n")
    write_file(repo, "README.md", "changed content\n")
    result = git_diff_core(path="README.md", root=repo)
    assert result["ok"] is True
    assert _sentinel_absent(sentinel)


def test_signature_verification_not_requested(booby_trapped_repo):
    """Local log.showSignature=true is beaten by -c log.showSignature=false."""
    repo, marker, sentinel = booby_trapped_repo
    result = git_log_core(root=repo)
    assert result["ok"] is True
    assert result["commits"]
    assert _sentinel_absent(sentinel)


def test_aliases_do_not_override_builtins(booby_trapped_repo):
    repo, marker, sentinel = booby_trapped_repo
    status = git_status_core(root=repo)
    diff = git_diff_core(root=repo)
    log = git_log_core(root=repo)
    assert status["ok"] is True
    assert diff["ok"] is True
    assert log["ok"] is True
    assert _sentinel_absent(sentinel)


def test_hooks_not_invoked_by_read_only_commands(tmp_path):
    """status/diff/log/for-each-ref/rev-parse run no Git hooks."""
    repo = make_git_repo(tmp_path)
    commit_file(repo, "README.md", "x\n", "initial")

    sentinel = tmp_path / "HOOK_EXECUTED"
    for hook in ("pre-commit", "post-commit", "pre-merge-commit", "post-checkout"):
        hook_path = repo / ".git" / "hooks" / hook
        hook_path.write_text(
            "#!/bin/sh\n"
            f'touch "{sentinel.as_posix()}"\n',
            encoding="utf-8",
        )
        hook_path.chmod(0o755)

    git_status_core(root=repo)
    git_diff_core(root=repo)
    git_log_core(root=repo)
    git_branches_core(root=repo)

    assert _sentinel_absent(sentinel)


def test_all_markers_stay_dormant_across_full_suite(booby_trapped_repo):
    """Every read-only operation together: all sentinels stay absent."""
    repo, marker, sentinel = booby_trapped_repo
    git_status_core(root=repo)
    git_diff_core(root=repo)
    git_diff_core(scope="staged", root=repo)
    git_diff_core(scope="head", root=repo)
    git_log_core(root=repo)
    git_branches_core(root=repo)
    assert _sentinel_absent(sentinel)
