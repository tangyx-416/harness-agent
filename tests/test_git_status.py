"""Tests for git_status (porcelain v2 parsing, lifecycle states, edges)."""

import pytest

from harness_agent.git_awareness.parsers import parse_status
from harness_agent.tools.git_tools import git_status_core

from git_test_utils import commit_file, make_git_repo, run_git, write_file


@pytest.fixture()
def git_repo(tmp_path):
    return make_git_repo(tmp_path)


def seeded_repo(tmp_path):
    repo = make_git_repo(tmp_path)
    commit_file(repo, "README.md", "hello\n", "initial commit")
    return repo


def test_clean_repository(tmp_path):
    repo = seeded_repo(tmp_path)
    result = git_status_core(root=repo)

    assert result["ok"] is True
    assert result["clean"] is True
    assert result["branch"] == "main"
    assert result["staged"] == []
    assert result["unstaged"] == []
    assert result["untracked"] == []
    assert result["conflicts"] == []
    assert result["truncated"] is False


def test_untracked_file_reported(tmp_path):
    repo = seeded_repo(tmp_path)
    write_file(repo, "notes.txt", "draft\n")
    result = git_status_core(root=repo)

    assert result["clean"] is False
    paths = [entry["path"] for entry in result["untracked"]]
    assert "notes.txt" in paths


def test_staged_file(tmp_path):
    repo = seeded_repo(tmp_path)
    write_file(repo, "new.txt", "content\n")
    run_git(repo, "add", "--", "new.txt")
    result = git_status_core(root=repo)

    assert len(result["staged"]) == 1
    assert result["staged"][0] == {"path": "new.txt", "status": "A"}


def test_unstaged_modification(tmp_path):
    repo = seeded_repo(tmp_path)
    write_file(repo, "README.md", "changed\n")
    result = git_status_core(root=repo)

    assert result["unstaged"] == [{"path": "README.md", "status": "M"}]
    assert result["staged"] == []


def test_staged_plus_later_unstaged(tmp_path):
    repo = seeded_repo(tmp_path)
    write_file(repo, "README.md", "staged version\n")
    run_git(repo, "add", "--", "README.md")
    write_file(repo, "README.md", "staged + unstaged version\n")
    result = git_status_core(root=repo)

    assert {"path": "README.md", "status": "M"} in result["staged"]
    assert {"path": "README.md", "status": "M"} in result["unstaged"]


def test_deleted_file(tmp_path):
    repo = seeded_repo(tmp_path)
    (repo / "README.md").unlink()
    result = git_status_core(root=repo)

    assert result["unstaged"] == [{"path": "README.md", "status": "D"}]


def test_rename_has_old_path(tmp_path):
    repo = seeded_repo(tmp_path)
    run_git(repo, "mv", "--", "README.md", "INTRO.md")
    result = git_status_core(root=repo)

    renames = [e for e in result["staged"] if e["status"] == "R"]
    assert len(renames) == 1
    assert renames[0]["path"] == "INTRO.md"
    assert renames[0]["old_path"] == "README.md"


def test_file_with_spaces(tmp_path):
    repo = seeded_repo(tmp_path)
    write_file(repo, "my file name.txt", "x\n")
    result = git_status_core(root=repo)
    assert "my file name.txt" in [e["path"] for e in result["untracked"]]


def test_unicode_filename(tmp_path):
    repo = seeded_repo(tmp_path)
    write_file(repo, "指令测试.txt", "x\n")
    result = git_status_core(root=repo)
    assert "指令测试.txt" in [e["path"] for e in result["untracked"]]


def test_current_branch_name(tmp_path):
    repo = make_git_repo(tmp_path, branch="trunk")
    commit_file(repo, "a.txt", "x\n", "c1")
    result = git_status_core(root=repo)
    assert result["branch"] == "trunk"


def test_no_upstream(tmp_path):
    repo = seeded_repo(tmp_path)
    result = git_status_core(root=repo)
    assert result["upstream"] is None
    assert result["ahead"] is None
    assert result["behind"] is None


def test_local_upstream_ahead_behind(tmp_path):
    """ahead/behind come from local tracking refs (no network)."""
    repo = seeded_repo(tmp_path)
    run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    run_git(repo, "config", "remote.origin.url", "https://example.invalid/probe.git")
    run_git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    run_git(repo, "config", "branch.main.remote", "origin")
    run_git(repo, "config", "branch.main.merge", "refs/heads/main")
    commit_file(repo, "more.txt", "x\n", "second")

    result = git_status_core(root=repo)
    assert result["upstream"] == "origin/main"
    assert result["ahead"] == 1
    assert result["behind"] == 0


def test_diverged_ahead_and_behind(tmp_path):
    """Diverged state (ahead=1, behind=1) parsed from local refs only."""
    repo = seeded_repo(tmp_path)
    commit_file(repo, "base.txt", "x\n", "local base")
    # Create the future remote-only commit, then rewind main past it.
    commit_file(repo, "xfile.txt", "x\n", "remote-only commit")
    x_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    run_git(repo, "reset", "--hard", "HEAD~1")
    # origin/main now points at a non-ancestor commit (behind=1)...
    run_git(repo, "update-ref", "refs/remotes/origin/main", x_sha)
    run_git(repo, "config", "remote.origin.url", "https://example.invalid/probe.git")
    run_git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    run_git(repo, "config", "branch.main.remote", "origin")
    run_git(repo, "config", "branch.main.merge", "refs/heads/main")
    # ...and the local branch gains a commit of its own (ahead=1).
    commit_file(repo, "c.txt", "x\n", "local ahead commit")

    result = git_status_core(root=repo)
    assert result["ahead"] == 1
    assert result["behind"] == 1


def test_detached_head(tmp_path):
    repo = seeded_repo(tmp_path)
    run_git(repo, "checkout", "--detach")
    result = git_status_core(root=repo)

    assert result["detached"] is True
    assert result["branch"] is None


def test_unborn_repository(tmp_path):
    repo = make_git_repo(tmp_path)  # no commits
    write_file(repo, "draft.txt", "x\n")
    result = git_status_core(root=repo)

    assert result["ok"] is True
    assert result["unborn"] is True
    assert result["branch"] == "main"
    assert result["head"] == "(initial)"
    assert "draft.txt" in [e["path"] for e in result["untracked"]]


def test_entry_cap_truncation_synthetic():
    """Synthetic porcelain with 600 untracked entries respects the cap."""
    lines = ["# branch.oid " + "a" * 40, "# branch.head main"]
    lines += [f"? f{i}.txt" for i in range(600)]
    raw = "\0".join(lines) + "\0"

    status = parse_status(raw, max_entries=500)
    assert len(status.untracked) == 500
    assert status.truncated is True
    assert status.clean is False


def test_parser_survives_malformed_streams():
    """Malformed/truncated porcelain must never raise (no IndexError)."""
    oid = "# branch.oid " + "a" * 40
    cases = {
        "empty": "",
        "only_separators": "\0\0\0",
        "garbage_token": "not-a-record\0",
        "truncated_1": oid + "\0# branch.head main\0" + "1 M. . m w h i\0",
        "truncated_2_no_orig_path": oid + "\0" + "2 R. . m w h i X100 new.txt\0",
        "truncated_u": oid + "\0" + "u UU . m m m h h h\0",
        "bad_prefix": oid + "\0" + "9 weird record with tokens\0",
    }
    for raw in cases.values():
        status = parse_status(raw)
        total = len(status.staged) + len(status.unstaged) + len(status.untracked)
        assert total >= 0

    # The rename case must not produce an entry with a missing old_path crash:
    status = parse_status("# branch.oid " + "a" * 40 + "\0"
                          "2 R. . m w h i X100 new.txt\0")
    assert status.staged == [] and status.unstaged == []


def test_rename_record_with_missing_orig_path_is_safe():
    raw = "# branch.oid " + "a" * 40 + "\0# branch.head main\02 R. . m w h i X100 new.txt\0"
    status = parse_status(raw)
    # No second NUL slot for old_path: parser must not raise.
    assert isinstance(status.staged, list) and isinstance(status.untracked, list)


def test_non_git_repo(tmp_path):
    result = git_status_core(root=tmp_path)
    assert result["ok"] is False
    assert "not a Git working tree" in result["error"]


def test_git_root_mismatch(tmp_path):
    make_git_repo(tmp_path)
    inner = tmp_path / "sub"
    inner.mkdir()
    result = git_status_core(root=inner)
    assert result["ok"] is False
    assert "differs" in result["error"]
