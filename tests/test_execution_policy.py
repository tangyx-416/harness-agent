"""Tests for the v0.3.0 execution policy (strict allowlist).

Each test evaluates :func:`evaluate_command` against an isolated
temporary repository root. Policy must NEVER execute anything -- these
tests are pure validation checks.
"""

import re
import sys
from pathlib import Path

import pytest

from harness_agent.execution import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    PolicyDecision,
    clamp_timeout,
    evaluate_command,
)


@pytest.fixture()
def repo(tmp_path):
    """A minimal repository root with marker + src/tests directories."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_config.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Allowed patterns
# ---------------------------------------------------------------------------


def test_python_version_allowed(repo):
    decision = evaluate_command("python", ["--version"], root=repo)
    assert decision.allowed is True
    assert decision.argv == (sys.executable, "--version")
    assert decision.display_command == "python --version"
    assert decision.risk_level == "LOW"


def test_pytest_bare_allowed(repo):
    decision = evaluate_command("pytest", [], root=repo)
    assert decision.allowed is True
    assert decision.argv == (sys.executable, "-m", "pytest")
    assert decision.risk_level == "HIGH"


def test_pytest_with_flags_allowed(repo):
    decision = evaluate_command(
        "python", ["-m", "pytest", "-q", "-x", "--tb=short"], root=repo
    )
    assert decision.allowed is True
    assert decision.argv[1:] == ("-m", "pytest", "-q", "-x", "--tb=short")


def test_pytest_repo_relative_path_allowed(repo):
    decision = evaluate_command(
        "python", ["-m", "pytest", "tests", "tests/test_config.py"], root=repo
    )
    assert decision.allowed is True
    assert "tests/test_config.py" in decision.argv


def test_pytest_single_flags_allowed(repo):
    for flags in (["-v"], ["--collect-only"], ["--maxfail=2"], ["-k", "test_ok"]):
        decision = evaluate_command("python", ["-m", "pytest", *flags], root=repo)
        assert decision.allowed is True, flags


def test_ruff_check_allowed(repo):
    decision = evaluate_command("python", ["-m", "ruff", "check", "."], root=repo)
    assert decision.allowed is True
    assert decision.argv == (sys.executable, "-m", "ruff", "check", ".")
    assert decision.risk_level == "MEDIUM"


def test_ruff_check_multiple_paths_allowed(repo):
    decision = evaluate_command("ruff", ["check", "src", "tests"], root=repo)
    assert decision.allowed is True
    assert decision.argv == (sys.executable, "-m", "ruff", "check", "src", "tests")


def test_risk_levels_informational(repo):
    low = evaluate_command("python", ["--version"], root=repo)
    medium = evaluate_command("ruff", ["check", "."], root=repo)
    high = evaluate_command("pytest", [], root=repo)
    assert (low.risk_level, medium.risk_level, high.risk_level) == (
        "LOW",
        "MEDIUM",
        "HIGH",
    )
    # All still require approval -- risk is informational only.
    assert "approval" in low.risk_reason.lower() or low.risk_reason


# ---------------------------------------------------------------------------
# Python restrictions
# ---------------------------------------------------------------------------


def test_python_inline_code_denied(repo):
    decision = evaluate_command("python", ["-c", "print('hello')"], root=repo)
    assert decision.allowed is False
    assert "-c" in decision.reason


def test_python_stdin_denied(repo):
    decision = evaluate_command("python", ["-"], root=repo)
    assert decision.allowed is False


def test_python_script_path_denied(repo):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "tool.py").write_text("print('x')\n", encoding="utf-8")
    decision = evaluate_command("python", ["scripts/tool.py"], root=repo)
    assert decision.allowed is False


def test_python_m_pip_denied(repo):
    decision = evaluate_command("python", ["-m", "pip", "install", "requests"], root=repo)
    assert decision.allowed is False
    assert "pip" in decision.reason


def test_python_arbitrary_module_denied(repo):
    decision = evaluate_command("python", ["-m", "http.server"], root=repo)
    assert decision.allowed is False
    decision = evaluate_command("python", ["-m", "venv", "x"], root=repo)
    assert decision.allowed is False


def test_python_bare_denied(repo):
    decision = evaluate_command("python", [], root=repo)
    assert decision.allowed is False


def test_python_extra_args_after_version_denied(repo):
    decision = evaluate_command("python", ["--version", "-q"], root=repo)
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# Forbidden programs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "program",
    [
        "pip",
        "uv",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "curl",
        "wget",
        "ssh",
        "scp",
        "powershell",
        "pwsh",
        "cmd",
        "bash",
        "sh",
        "git",
        "docker",
        "kubectl",
        "rm",
        "del",
        "mv",
        "cp",
    ],
)
def test_forbidden_programs_denied(repo, program):
    decision = evaluate_command(program, [], root=repo)
    assert decision.allowed is False, program


def test_git_status_denied(repo):
    decision = evaluate_command("git", ["status"], root=repo)
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# Shell metacharacter policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["-q", ";", "rm", "-rf", "."],
        ["-q", "&&", "something"],
        ["||", "something"],
        ["|", "something"],
        [">", "output.txt"],
        ["<", "input.txt"],
        ["`whoami`"],
        ["$(whoami)"],
        ["-k", "test\nKILL"],
    ],
)
def test_shell_metacharacters_denied(repo, args):
    decision = evaluate_command("pytest", args, root=repo)
    assert decision.allowed is False, args


def test_shell_sequence_in_program_denied(repo):
    decision = evaluate_command("pytest && something", [], root=repo)
    assert decision.allowed is False


def test_metachar_free_expression_allowed(repo):
    decision = evaluate_command("pytest", ["-k", "test_ok and not slow"], root=repo)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Working directory confinement
# ---------------------------------------------------------------------------


def test_cwd_root_allowed(repo):
    decision = evaluate_command("python", ["--version"], cwd=".", root=repo)
    assert decision.allowed is True
    assert Path(decision.cwd) == repo.resolve()


def test_cwd_subdirectory_allowed(repo):
    decision = evaluate_command("pytest", ["tests"], cwd="src", root=repo)
    assert decision.allowed is True
    assert Path(decision.cwd) == (repo / "src").resolve()


def test_cwd_parent_escape_denied(repo):
    for bad in ("../", "../../"):
        decision = evaluate_command("python", ["--version"], cwd=bad, root=repo)
        assert decision.allowed is False, bad
        assert "refused" in decision.reason.lower()


def test_cwd_absolute_external_denied(repo):
    outside = tmp_outside_dir(repo)
    decision = evaluate_command("python", ["--version"], cwd=str(outside), root=repo)
    assert decision.allowed is False


def test_cwd_ignored_directory_denied(repo):
    (repo / ".venv").mkdir()
    decision = evaluate_command("python", ["--version"], cwd=".venv", root=repo)
    assert decision.allowed is False


def tmp_outside_dir(repo: Path) -> Path:
    outside = repo.parent / f"outside_{repo.name}"
    outside.mkdir(exist_ok=True)
    return outside


# ---------------------------------------------------------------------------
# Path argument confinement
# ---------------------------------------------------------------------------


def test_external_path_argument_denied(repo):
    outside = tmp_outside_dir(repo)
    decision = evaluate_command("pytest", ["-q", str(outside)], root=repo)
    assert decision.allowed is False


def test_ruff_external_path_denied(repo):
    outside = tmp_outside_dir(repo)
    decision = evaluate_command("ruff", ["check", str(outside)], root=repo)
    assert decision.allowed is False


def test_path_arg_ignored_dir_denied(repo):
    decision = evaluate_command("pytest", ["-q", ".venv"], root=repo)
    assert decision.allowed is False


def test_k_expression_not_treated_as_path(repo):
    decision = evaluate_command("pytest", ["-k", "test_config"], root=repo)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Flag-level validation
# ---------------------------------------------------------------------------


def test_ruff_fix_denied(repo):
    decision = evaluate_command("ruff", ["check", "--fix", "."], root=repo)
    assert decision.allowed is False
    assert "--fix" in decision.reason


def test_ruff_unsafe_fixes_denied(repo):
    decision = evaluate_command("ruff", ["check", "--unsafe-fixes"], root=repo)
    assert decision.allowed is False


def test_ruff_format_subcommand_denied(repo):
    decision = evaluate_command("ruff", ["format", "."], root=repo)
    assert decision.allowed is False


def test_ruff_any_option_denied(repo):
    decision = evaluate_command("ruff", ["check", ".", "--select", "E501"], root=repo)
    assert decision.allowed is False


@pytest.mark.parametrize(
    "flag",
    [
        "-s",
        "--pdb",
        "--cov",
        "-p",
        "-o",
        "--rootdir=..",
        "--confcutdir=..",
        "--no-header",
    ],
)
def test_unsupported_pytest_flags_denied(repo, flag):
    decision = evaluate_command("pytest", [flag], root=repo)
    assert decision.allowed is False, flag


def test_pytest_bad_tb_value_denied(repo):
    decision = evaluate_command("pytest", ["--tb=long"], root=repo)
    assert decision.allowed is False


def test_pytest_bad_maxfail_denied(repo):
    decision = evaluate_command("pytest", ["--maxfail=0"], root=repo)
    assert decision.allowed is False
    decision = evaluate_command("pytest", ["--maxfail", "abc"], root=repo)
    assert decision.allowed is False


def test_pytest_maxfail_split_form_allowed(repo):
    decision = evaluate_command("pytest", ["--maxfail", "3", "-q"], root=repo)
    assert decision.allowed is True
    assert decision.argv[3:5] == ("--maxfail", "3")


# ---------------------------------------------------------------------------
# Normalization and timeout
# ---------------------------------------------------------------------------


def test_python3_alias_normalized(repo):
    decision = evaluate_command("python3", ["--version"], root=repo)
    assert decision.allowed is True
    assert decision.argv[0] == sys.executable


def test_denied_decision_has_no_argv(repo):
    decision = evaluate_command("pip", ["install", "requests"], root=repo)
    assert decision.allowed is False
    assert decision.argv == ()
    assert decision.display_command == ""


def test_timeout_clamped_to_maximum():
    assert clamp_timeout(999999) == MAX_TIMEOUT_SECONDS
    assert clamp_timeout(None) == DEFAULT_TIMEOUT_SECONDS
    assert clamp_timeout(45) == 45
    assert clamp_timeout(0) == 1
    assert clamp_timeout("bogus") == DEFAULT_TIMEOUT_SECONDS


def test_allowed_decision_carries_clamped_timeout(repo):
    decision = evaluate_command(
        "pytest", [], root=repo, timeout_seconds=999999
    )
    assert decision.allowed is True
    assert decision.timeout_seconds == MAX_TIMEOUT_SECONDS


def test_reason_is_human_readable(repo):
    decision = evaluate_command("python", ["-m", "boto3"], root=repo)
    assert decision.allowed is False
    assert "boto3" in decision.reason
