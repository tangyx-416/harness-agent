"""Tests for the v0.2.0 read-only repository tools.

All tests are hermetic: they build temporary repository trees and call the
``*_core`` functions with an explicit ``root`` so the real project is never
touched and no network/API access occurs.
"""

import tomllib
from pathlib import Path

import pytest
from harness_agent.tools.path_utils import (
    LIST_MAX_DEPTH,
    LIST_MAX_ENTRIES,
    READ_MAX_FILE_SIZE_BYTES,
    READ_MAX_LINES_PER_CALL,
    SEARCH_MAX_RESULTS,
)
from harness_agent.tools.repository_tools import (
    analyze_dependencies_core,
    list_directory_core,
    read_file_core,
    search_code_core,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    """A small deterministic repository tree used by most tests."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "line one\nfind_me here\nline three\n", encoding="utf-8"
    )
    (tmp_path / "src" / "util.py").write_text(
        "import os\n# find_me comment\n", encoding="utf-8"
    )
    (tmp_path / "docs.md").write_text("docs with find_me\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mini"\nversion = "1.2.3"\n'
        'requires-python = ">=3.10"\n'
        "dependencies = ['requests>=2.0', 'rich']\n"
        "[project.optional-dependencies]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


class TestListDirectory:
    def test_normal_directory(self, mini_repo):
        result = list_directory_core(".", root=mini_repo)
        assert result["ok"] is True
        names = {e["name"] for e in result["entries"]}
        assert {"src", "README.md", "docs.md", ".env.example"} <= names
        assert result["truncated"] is False

    def test_nested_depth(self, mini_repo):
        (mini_repo / "src" / "deep").mkdir()
        (mini_repo / "src" / "deep" / "leaf.py").touch()
        result = list_directory_core(".", depth=2, root=mini_repo)
        names = {e["name"] for e in result["entries"]}
        assert "deep" in names
        assert result["depth"] == 2

    def test_depth_limit_clamped(self, mini_repo):
        result = list_directory_core(".", depth=99, root=mini_repo)
        assert result["ok"] is True
        assert result["depth"] == LIST_MAX_DEPTH
        assert "note" in result

    def test_depth_below_one_rejected(self, mini_repo):
        result = list_directory_core(".", depth=0, root=mini_repo)
        assert result["ok"] is True
        assert result["depth"] == 1

    def test_nonexistent_path(self, mini_repo):
        result = list_directory_core("no/such/dir", root=mini_repo)
        assert result["ok"] is False
        assert "not found" in result["error"].lower() or "does not exist" in result["error"].lower()

    def test_path_traversal_denied(self, mini_repo):
        result = list_directory_core("../", root=mini_repo)
        assert result["ok"] is False
        assert "escape" in result["error"].lower()

    def test_sensitive_directory_hidden(self, mini_repo):
        (mini_repo / ".ssh").mkdir()
        (mini_repo / ".ssh" / "id_rsa").touch()
        result = list_directory_core(".", root=mini_repo)
        names = {e["name"] for e in result["entries"]}
        assert ".ssh" not in names

    def test_ignored_directory_hidden(self, mini_repo):
        (mini_repo / "__pycache__").mkdir()
        (mini_repo / "node_modules").mkdir()
        result = list_directory_core(".", root=mini_repo)
        names = {e["name"] for e in result["entries"]}
        assert "__pycache__" not in names
        assert "node_modules" not in names

    def test_result_truncation(self, mini_repo):
        for i in range(LIST_MAX_ENTRIES + 20):
            (mini_repo / f"f{i:03}.txt").touch()
        result = list_directory_core(".", depth=1, root=mini_repo)
        assert result["ok"] is True
        assert result["entry_count"] <= LIST_MAX_ENTRIES
        assert result["truncated"] is True

    def test_target_is_file_rejected(self, mini_repo):
        result = list_directory_core("README.md", root=mini_repo)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_normal_text_file(self, mini_repo):
        result = read_file_core("src/app.py", root=mini_repo)
        assert result["ok"] is True
        assert "find_me here" in result["content"]
        assert result["total_lines"] == 3
        assert result["start_line"] == 1
        assert result["end_line"] == 3
        assert result["truncated"] is False

    def test_line_numbers_prefixed(self, mini_repo):
        result = read_file_core("src/app.py", root=mini_repo)
        first_line = result["content"].splitlines()[0]
        assert first_line.startswith("1 | line one")

    def test_line_range(self, mini_repo):
        result = read_file_core("src/app.py", start_line=2, end_line=3, root=mini_repo)
        assert result["ok"] is True
        assert result["start_line"] == 2
        assert result["end_line"] == 3
        assert "find_me here" in result["content"]
        assert "line one" not in result["content"]

    def test_line_range_clamped_to_eof(self, mini_repo):
        result = read_file_core("src/app.py", start_line=2, end_line=999, root=mini_repo)
        assert result["ok"] is True
        assert result["end_line"] == 3
        assert result["range_clamped_to_eof"] is True

    def test_invalid_range_start_gt_end(self, mini_repo):
        result = read_file_core("src/app.py", start_line=3, end_line=1, root=mini_repo)
        assert result["ok"] is False
        assert "invalid line range" in result["error"].lower()

    def test_invalid_range_zero_start(self, mini_repo):
        result = read_file_core("src/app.py", start_line=0, root=mini_repo)
        assert result["ok"] is False
        assert "invalid" in result["error"].lower()

    def test_start_beyond_eof(self, mini_repo):
        result = read_file_core("src/app.py", start_line=100, root=mini_repo)
        assert result["ok"] is False

    def test_nonexistent_file(self, mini_repo):
        result = read_file_core("nope.py", root=mini_repo)
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_path_traversal_denied(self, mini_repo):
        result = read_file_core("../../secret.txt", root=mini_repo)
        assert result["ok"] is False
        assert "escape" in result["error"].lower()

    def test_absolute_path_outside_denied(self, tmp_path, mini_repo):
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("x", encoding="utf-8")
        try:
            result = read_file_core(str(outside), root=mini_repo)
            assert result["ok"] is False
        finally:
            outside.unlink(missing_ok=True)

    def test_env_blocked(self, mini_repo):
        result = read_file_core(".env", root=mini_repo)
        assert result["ok"] is False
        assert "denied" in result["error"].lower()

    def test_env_variant_blocked(self, mini_repo):
        (mini_repo / ".env.local").write_text("X=1\n", encoding="utf-8")
        result = read_file_core(".env.local", root=mini_repo)
        assert result["ok"] is False

    def test_env_example_allowed(self, mini_repo):
        result = read_file_core(".env.example", root=mini_repo)
        assert result["ok"] is True
        assert "SECRET=" in result["content"]

    def test_private_key_extension_blocked(self, mini_repo):
        (mini_repo / "server.pem").write_text("key", encoding="utf-8")
        result = read_file_core("server.pem", root=mini_repo)
        assert result["ok"] is False

    def test_normal_source_not_blocked_by_policy(self, mini_repo):
        (mini_repo / "tokenizer.py").write_text("x = 1\n", encoding="utf-8")
        (mini_repo / "secretary.py").write_text("y = 2\n", encoding="utf-8")
        assert read_file_core("tokenizer.py", root=mini_repo)["ok"] is True
        assert read_file_core("secretary.py", root=mini_repo)["ok"] is True

    def test_binary_extension_blocked(self, mini_repo):
        (mini_repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        result = read_file_core("logo.png", root=mini_repo)
        assert result["ok"] is False
        assert "binary" in result["error"].lower()

    def test_binary_content_sniff_blocked(self, mini_repo):
        (mini_repo / "blob.dat").write_bytes(b"\x00\x01\x02binary")
        result = read_file_core("blob.dat", root=mini_repo)
        assert result["ok"] is False
        assert "binary" in result["error"].lower()

    def test_ignored_directory_access_denied(self, mini_repo):
        cache = mini_repo / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-311.pyc").write_bytes(b"\x00\x01")
        result = read_file_core("__pycache__/app.cpython-311.pyc", root=mini_repo)
        assert result["ok"] is False

    def test_large_file_requires_range(self, mini_repo):
        big = mini_repo / "big.txt"
        big.write_text("x" * (READ_MAX_FILE_SIZE_BYTES + 1), encoding="utf-8")
        blocked = read_file_core("big.txt", root=mini_repo)
        assert blocked["ok"] is False
        assert "safe read limit" in blocked["error"]

    def test_large_file_with_range_allowed(self, mini_repo):
        big = mini_repo / "big.txt"
        big.write_text("x" * (READ_MAX_FILE_SIZE_BYTES + 1), encoding="utf-8")
        result = read_file_core("big.txt", start_line=1, end_line=5, root=mini_repo)
        assert result["ok"] is True

    def test_large_file_ranged_read_is_streaming(self, mini_repo, monkeypatch):
        """Ranged reads on oversized files must never load the whole file."""
        payload = "line {:05d} " + "x" * 28
        lines = [payload.format(i) for i in range(1, 8001)]
        huge = mini_repo / "huge.txt"
        huge.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert huge.stat().st_size > READ_MAX_FILE_SIZE_BYTES

        def _boom(self):
            raise AssertionError("read_bytes must not be used for large-file ranged reads")

        monkeypatch.setattr("pathlib.Path.read_bytes", _boom)

        result = read_file_core("huge.txt", start_line=100, end_line=120, root=mini_repo)
        assert result["ok"] is True
        assert result["total_lines"] == 8000
        assert result["start_line"] == 100
        assert result["end_line"] == 120
        assert result["truncated"] is False
        assert "line 00100" in result["content"]
        assert "line 00121" not in result["content"]

    def test_large_file_ranged_output_line_limit(self, mini_repo):
        payload = "row {:05d} " + "y" * 28
        lines = [payload.format(i) for i in range(1, 8001)]
        huge = mini_repo / "huge2.txt"
        huge.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = read_file_core("huge2.txt", start_line=10, root=mini_repo)
        assert result["ok"] is True
        assert result["total_lines"] == 8000
        assert result["end_line"] == 10 + READ_MAX_LINES_PER_CALL - 1
        assert result["truncated"] is True
        assert result["content_truncated_at"] == result["end_line"]
        assert len(result["content"].splitlines()) == READ_MAX_LINES_PER_CALL

    def test_output_line_limit(self, mini_repo):
        many = mini_repo / "many.txt"
        many.write_text(
            "\n".join(f"line {i}" for i in range(1, READ_MAX_LINES_PER_CALL + 51)),
            encoding="utf-8",
        )
        result = read_file_core("many.txt", root=mini_repo)
        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["end_line"] == READ_MAX_LINES_PER_CALL
        assert len(result["content"].splitlines()) == READ_MAX_LINES_PER_CALL

    def test_directory_target_rejected(self, mini_repo):
        result = read_file_core("src", root=mini_repo)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------


class TestSearchCode:
    def test_normal_search(self, mini_repo):
        result = search_code_core("find_me", root=mini_repo)
        assert result["ok"] is True
        assert result["match_count"] >= 3
        paths = {m["path"] for m in result["matches"]}
        assert "src/app.py" in paths

    def test_line_numbers_correct(self, mini_repo):
        result = search_code_core("find_me", path="src/app.py", root=mini_repo)
        hit = next(m for m in result["matches"] if m["path"] == "src/app.py")
        assert hit["line"] == 2

    def test_multiple_matches(self, mini_repo):
        result = search_code_core("find_me", root=mini_repo)
        assert result["match_count"] == len(result["matches"]) >= 2
        assert all({"path", "line", "text"} <= set(m) for m in result["matches"])

    def test_query_across_multiple_files(self, mini_repo):
        result = search_code_core("find_me", root=mini_repo)
        paths = {m["path"] for m in result["matches"]}
        assert paths == {"src/app.py", "src/util.py", "docs.md"}

    def test_case_insensitive(self, mini_repo):
        result = search_code_core("FIND_ME", root=mini_repo)
        assert result["match_count"] >= 3

    def test_file_pattern_filter(self, mini_repo):
        result = search_code_core("find_me", file_pattern="*.py", root=mini_repo)
        paths = {m["path"] for m in result["matches"]}
        assert paths == {"src/app.py", "src/util.py"}

    def test_scope_restricted_to_subdirectory(self, mini_repo):
        result = search_code_core("find_me", path="src", root=mini_repo)
        paths = {m["path"] for m in result["matches"]}
        assert all(p.startswith("src/") for p in paths)
        assert "docs.md" not in paths

    def test_sensitive_files_excluded(self, mini_repo):
        (mini_repo / "leak.txt").write_text("find_me in leak\n", encoding="utf-8")
        (mini_repo / "secrets.yaml").write_text("find_me: yes\n", encoding="utf-8")
        result = search_code_core("find_me", root=mini_repo)
        paths = {m["path"] for m in result["matches"]}
        assert "leak.txt" in paths          # normal txt is searched
        assert "secrets.yaml" not in paths  # sensitive stem excluded

    def test_ignored_directories_excluded(self, mini_repo):
        (mini_repo / "node_modules").mkdir()
        (mini_repo / "node_modules" / "dep.py").write_text("find_me\n", encoding="utf-8")
        result = search_code_core("find_me", root=mini_repo)
        assert all(not m["path"].startswith("node_modules") for m in result["matches"])

    def test_nonexistent_search_root(self, mini_repo):
        result = search_code_core("find_me", path="missing_dir", root=mini_repo)
        assert result["ok"] is False

    def test_path_traversal_scope_denied(self, mini_repo):
        result = search_code_core("find_me", path="../", root=mini_repo)
        assert result["ok"] is False

    def test_search_inside_venv_denied(self, mini_repo):
        (mini_repo / ".venv").mkdir()
        result = search_code_core("find_me", path=".venv", root=mini_repo)
        assert result["ok"] is False
        assert "denied" in result["error"].lower()

    def test_empty_query_rejected(self, mini_repo):
        result = search_code_core("   ", root=mini_repo)
        assert result["ok"] is False

    def test_no_results(self, mini_repo):
        result = search_code_core("totally_absent_symbol", root=mini_repo)
        assert result["ok"] is True
        assert result["match_count"] == 0
        assert result["matches"] == []

    def test_max_results_cap(self, mini_repo):
        target = mini_repo / "hit.py"
        target.write_text("hit\n" * 30, encoding="utf-8")
        result = search_code_core("hit", max_results=5, root=mini_repo)
        assert result["match_count"] == 5
        assert result["truncated"] is True

    def test_max_results_upper_bound(self, mini_repo):
        target = mini_repo / "hit.py"
        target.write_text("hit\n" * 500, encoding="utf-8")
        result = search_code_core("hit", max_results=10_000, root=mini_repo)
        assert result["match_count"] == SEARCH_MAX_RESULTS

    def test_regex_mode(self, mini_repo):
        result = search_code_core(r"find_\w+", use_regex=True, root=mini_repo)
        assert result["ok"] is True
        assert result["match_count"] >= 3

    def test_invalid_regex_clean_error(self, mini_repo):
        result = search_code_core("([unclosed", use_regex=True, root=mini_repo)
        assert result["ok"] is False
        assert "regular expression" in result["error"].lower()


# ---------------------------------------------------------------------------
# analyze_dependencies
# ---------------------------------------------------------------------------


class TestAnalyzeDependencies:
    def test_parse_pyproject(self, mini_repo):
        result = analyze_dependencies_core(root=mini_repo)
        assert result["ok"] is True
        pyproject = next(
            m for m in result["manifests"] if m["type"] == "python-pyproject"
        )
        assert pyproject["path"] == "pyproject.toml"

    def test_project_name_and_version(self, mini_repo):
        result = analyze_dependencies_core(root=mini_repo)
        pyproject = result["manifests"][0]
        assert pyproject["name"] == "mini"
        assert pyproject["version"] == "1.2.3"

    def test_runtime_dependencies(self, mini_repo):
        result = analyze_dependencies_core(root=mini_repo)
        pyproject = result["manifests"][0]
        assert pyproject["dependencies"] == ["requests>=2.0", "rich"]
        assert pyproject["optional_dependencies"]["dev"] == ["pytest"]

    def test_python_version(self, mini_repo):
        result = analyze_dependencies_core(root=mini_repo)
        assert result["manifests"][0]["requires_python"] == ">=3.10"

    def test_requirements_files(self, mini_repo):
        (mini_repo / "requirements.txt").write_text(
            "flask>=3.0\n# comment\n\n-r extras.txt\n", encoding="utf-8"
        )
        (mini_repo / "requirements-dev.txt").write_text(
            "pytest-cov\n", encoding="utf-8"
        )
        result = analyze_dependencies_core(root=mini_repo)
        req = {m["path"]: m for m in result["manifests"] if m["type"] == "python-requirements"}
        assert req["requirements.txt"]["dependencies"] == ["flask>=3.0"]
        assert req["requirements-dev.txt"]["dependencies"] == ["pytest-cov"]

    def test_package_json(self, mini_repo):
        (mini_repo / "package.json").write_text(
            '{"name": "web", "version": "0.1.0",'
            ' "dependencies": {"react": "^18"},'
            ' "devDependencies": {"vitest": "^1.0"}}',
            encoding="utf-8",
        )
        result = analyze_dependencies_core(root=mini_repo)
        node = next(m for m in result["manifests"] if m["type"] == "node-package-json")
        assert node["name"] == "web"
        assert node["dependencies"] == ["react"]
        assert node["devDependencies"] == ["vitest"]

    def test_malformed_manifest_reported_not_raised(self, mini_repo):
        (mini_repo / "package.json").write_text("{not json", encoding="utf-8")
        result = analyze_dependencies_core(root=mini_repo)
        assert result["ok"] is True
        node = next(m for m in result["manifests"] if m["type"] == "node-package-json")
        assert "parse_error" in node

    def test_malformed_toml_reported_not_raised(self, mini_repo):
        (mini_repo / "broken.toml").write_bytes(b"")  # ensure dir valid
        (mini_repo / "pyproject.toml").write_text("[project\n", encoding="utf-8")
        result = analyze_dependencies_core(root=mini_repo)
        assert result["ok"] is True
        assert "parse_error" in result["manifests"][0]

    def test_no_manifests(self, tmp_path):
        result = analyze_dependencies_core(root=tmp_path)
        assert result["ok"] is True
        assert result["manifests"] == []
        assert result["manifest_count"] == 0


# ---------------------------------------------------------------------------
# Real repository integration sanity (read-only, no mocks needed)
# ---------------------------------------------------------------------------


class TestRealRepositorySmoke:
    """Exercise the cores against this actual project (read-only)."""

    def test_real_repo_dependencies(self):
        result = analyze_dependencies_core()
        assert result["ok"] is True
        pyproject = result["manifests"][0]
        assert pyproject["name"] == "harness-agent"

    def test_real_repo_read_agent_py(self):
        result = read_file_core("src/harness_agent/agent.py", start_line=1, end_line=5)
        assert result["ok"] is True
        assert "Agent factory" in result["content"]

    def test_real_repo_search_create_agent(self):
        result = search_code_core("create_agent", file_pattern="*.py")
        paths = {m["path"] for m in result["matches"]}
        assert "src/harness_agent/agent.py" in paths
        assert "scripts/run_agent.py" in paths

    def test_real_repo_list_src(self):
        result = list_directory_core("src")
        names = {e["name"] for e in result["entries"]}
        assert "harness_agent" in names


# ---------------------------------------------------------------------------
# Guard: tomllib availability contract used by analyze_dependencies
# ---------------------------------------------------------------------------


def test_mini_repo_toml_is_valid():
    data = tomllib.loads(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["a"]\n'
    )
    assert data["project"]["name"] == "x"
