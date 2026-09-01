"""Tests for the host-side execution service.

Two layers of verification:

* Unit level: a recording ``FakePopen`` verifies exactly how
  ``subprocess.Popen`` is invoked (shell=False, argv list, validated cwd,
  DEVNULL stdin, PIPE streams, sanitized env, wait timeout).
* Real level: harmless real child processes prove bounded streaming
  capture (multi-MB output never accumulates in the parent result),
  deadlock-free dual-stream output, timeout kill with partial output,
  and end-to-end `python --version` execution.
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness_agent.execution import (
    MAX_OUTPUT_CHARS,
    ExecutionBroker,
    ExecutionPlan,
    build_child_environment,
    execute_approved,
    execute_plan,
)
from harness_agent.execution.service import (
    EXECUTION_BEHAVIOR_ENV_KEYS,
    MAX_OUTPUT_CHARS as SERVICE_OUTPUT_CAP,
    SENSITIVE_ENV_KEYS_EXACT,
    SENSITIVE_ENV_KEY_SUFFIXES,
    _BoundedStore,
    _popen_process_runner,
)


def make_plan(tmp_path, **overrides) -> ExecutionPlan:
    fields = dict(
        id="plan-1",
        program=sys.executable,
        args=("--version",),
        cwd=str(tmp_path),
        display_command="python --version",
        risk_level="LOW",
        risk_reason="version only",
        timeout_seconds=30,
        created_at=0.0,
    )
    fields.update(overrides)
    return ExecutionPlan(**fields)


# ---------------------------------------------------------------------------
# FakePopen: records exactly how Popen is called
# ---------------------------------------------------------------------------


class FakePopen:
    """Mimics the Popen surface used by _popen_process_runner."""

    instances = []

    def __init__(self, argv, return_code=0, stdout_text="", stderr_text="",
                 wait_behavior="return", **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.wait_behavior = wait_behavior
        # Real Popen keeps returncode=None until the child is reaped.
        self.returncode = None if wait_behavior == "timeout_hang" else return_code
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.wait_calls = []
        self.killed = False
        FakePopen.instances.append(self)

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_behavior == "timeout_then_return" and not self.killed:
            raise subprocess.TimeoutExpired(cmd=" ".join(self.argv), timeout=timeout)
        if self.wait_behavior == "timeout_hang":
            # Child (or an unreaped descendant holding the pipe) never exits.
            raise subprocess.TimeoutExpired(cmd=" ".join(self.argv), timeout=timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        if self.wait_behavior == "timeout_then_return":
            self.returncode = 1

    def poll(self):
        return self.returncode


@pytest.fixture()
def fake_popen(monkeypatch):
    FakePopen.instances = []

    def _install(**proc_kwargs):
        def factory(argv, **kwargs):
            return FakePopen(argv, **proc_kwargs, **kwargs)

        monkeypatch.setattr(
            "harness_agent.process.subprocess.Popen", factory
        )
        return FakePopen.instances

    return _install


# ---------------------------------------------------------------------------
# subprocess invocation guarantees (unit level)
# ---------------------------------------------------------------------------


def test_shell_false_enforced(tmp_path, fake_popen):
    fake_popen(return_code=0)
    execute_plan(make_plan(tmp_path))
    assert FakePopen.instances[-1].kwargs["shell"] is False


def test_argv_list_used_not_string(tmp_path, fake_popen):
    fake_popen(return_code=0)
    plan = make_plan(tmp_path, args=("-m", "pytest", "-q"))
    execute_plan(plan)
    assert FakePopen.instances[-1].argv == [sys.executable, "-m", "pytest", "-q"]
    assert isinstance(FakePopen.instances[-1].argv, list)


def test_validated_cwd_used(tmp_path, fake_popen):
    fake_popen(return_code=0)
    plan = make_plan(tmp_path, cwd=str(tmp_path / "sub"))
    execute_plan(plan)
    assert FakePopen.instances[-1].kwargs["cwd"] == str(tmp_path / "sub")


def test_wait_timeout_passed(tmp_path, fake_popen):
    fake_popen(return_code=0)
    execute_plan(make_plan(tmp_path, timeout_seconds=45))
    assert FakePopen.instances[-1].wait_calls[-1] == 45


def test_stdin_disabled(tmp_path, fake_popen):
    fake_popen(return_code=0)
    execute_plan(make_plan(tmp_path))
    assert FakePopen.instances[-1].kwargs["stdin"] is subprocess.DEVNULL


def test_streams_are_piped_text(tmp_path, fake_popen):
    fake_popen(return_code=0)
    execute_plan(make_plan(tmp_path))
    kwargs = FakePopen.instances[-1].kwargs
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_stdout_captured(tmp_path, fake_popen):
    fake_popen(return_code=0, stdout_text="hello world")
    result = execute_plan(make_plan(tmp_path))
    assert result.stdout == "hello world"


def test_stderr_captured(tmp_path, fake_popen):
    fake_popen(return_code=0, stderr_text="warning happened")
    result = execute_plan(make_plan(tmp_path))
    assert result.stderr == "warning happened"


def test_exit_code_propagated(tmp_path, fake_popen):
    fake_popen(return_code=3)
    result = execute_plan(make_plan(tmp_path))
    assert result.exit_code == 3
    assert result.timed_out is False


def test_timeout_kills_child_and_returns_partial(tmp_path, fake_popen):
    fake_popen(return_code=0, stdout_text="partial output",
               wait_behavior="timeout_then_return")
    result = execute_plan(make_plan(tmp_path))
    proc = FakePopen.instances[-1]
    assert proc.killed is True
    assert result.timed_out is True
    assert result.stdout == "partial output"
    assert result.exit_code == 1  # returncode observed after kill


def test_timeout_exit_code_none_when_unreaped(tmp_path, fake_popen):
    fake_popen(stdout_text="", wait_behavior="timeout_hang")
    result = execute_plan(make_plan(tmp_path))
    assert result.timed_out is True
    # Child never reaped -- exit code stays unknown, result stays bounded.
    assert result.exit_code is None


def test_duration_present(tmp_path, fake_popen):
    fake_popen(return_code=0)
    result = execute_plan(make_plan(tmp_path))
    assert isinstance(result.duration_ms, int)
    assert result.duration_ms >= 0


def test_launch_failure_becomes_structured_result(tmp_path, fake_popen, monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("executable not found")

    monkeypatch.setattr(
        "harness_agent.process.subprocess.Popen", boom
    )
    result = execute_plan(make_plan(tmp_path))
    assert result.exit_code is None
    assert "Failed to launch command" in result.stderr
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Environment sanitization
# ---------------------------------------------------------------------------


def test_child_env_hides_secrets_from_subprocess(tmp_path, fake_popen, monkeypatch):
    """Parent OPENAI_API_KEY must never reach the child environment."""
    fake_popen(return_code=0)
    monkeypatch.setenv("OPENAI_API_KEY", "test-super-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token-value")

    execute_plan(make_plan(tmp_path))

    env = FakePopen.instances[-1].kwargs["env"]
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "test-super-secret-value" not in str(env)
    # Essential variables survive.
    assert env.get("PATH") == os.environ.get("PATH")
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert env.get("PYTHONNOUSERSITE") == "1"


def test_build_child_environment_scrubbing(monkeypatch):
    parent = {
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:/Windows",
        "WINDIR": "C:/Windows",
        "TEMP": "/tmp",
        "TMP": "/tmp",
        "HOME": "/home/u",
        "USERPROFILE": "C:/Users/u",
        "VIRTUAL_ENV": "C:/repo/.venv",
        "LANG": "en_US",
        "OPENAI_API_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_SESSION_TOKEN": "x",
        "GITHUB_TOKEN": "x",
        "GH_TOKEN": "x",
        "MY_SERVICE_TOKEN": "x",
        "MY_SERVICE_SECRET": "x",
        "MY_SERVICE_PASSWORD": "x",
        "OTHER_API_KEY": "x",
        "GOOGLE_APPLICATION_CREDENTIALS": "x",
        "PYTHONPATH": "C:/injected",
        "PYTHONHOME": "C:/injected",
        "PYTHONSTARTUP": "C:/injected.py",
        "PYTHONINSPECT": "1",
        "PYTHONUSERBASE": "C:/injected",
        "PYTEST_ADDOPTS": "-p evil_plugin",
        "PYTEST_PLUGINS": "evil_plugin",
        "PYTEST_DEBUG": "1",
    }
    cleaned = build_child_environment(parent)

    kept = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
            "USERPROFILE", "VIRTUAL_ENV", "LANG")
    for key in kept:
        assert key in cleaned, key

    for key in parent:
        if key not in kept:
            assert key not in cleaned, key

    assert cleaned["PYTHONDONTWRITEBYTECODE"] == "1"
    assert cleaned["PYTHONNOUSERSITE"] == "1"
    assert cleaned["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert cleaned["PYTHONUTF8"] == "1"
    assert cleaned["PYTHONIOENCODING"] == "utf-8"


def test_sensitive_pattern_tables_cover_required_keys():
    required = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    }
    for key in required:
        assert (
            key in SENSITIVE_ENV_KEYS_EXACT
            or key in EXECUTION_BEHAVIOR_ENV_KEYS
            or key.endswith(SENSITIVE_ENV_KEY_SUFFIXES)
        ), key


# ---------------------------------------------------------------------------
# Bounded store unit tests
# ---------------------------------------------------------------------------


def test_bounded_store_under_limit():
    store = _BoundedStore(100)
    store.feed("x" * 100)
    assert store.value() == "x" * 100
    assert store.truncated is False


def test_bounded_store_exact_limit_not_flagged():
    store = _BoundedStore(100)
    for _ in range(10):
        store.feed("x" * 10)
    assert store.stored == 100
    assert store.truncated is False


def test_bounded_store_overflow_flags_and_discards():
    store = _BoundedStore(100)
    store.feed("x" * 60)
    store.feed("y" * 60)  # pushes past limit; only part stored, rest discarded
    assert store.stored == 100
    assert len(store.value()) == 100
    assert store.truncated is True


def test_service_output_cap_matches_policy():
    assert SERVICE_OUTPUT_CAP == MAX_OUTPUT_CHARS


# ---------------------------------------------------------------------------
# Real-process bounded capture (stress, ~2 MB -- well below GB scale)
# ---------------------------------------------------------------------------


def _stress_plan(tmp_path, code, **overrides) -> ExecutionPlan:
    return make_plan(
        tmp_path, args=("-c", code), display_command="python -c <stress>",
        **overrides,
    )


def test_real_large_stdout_is_bounded(tmp_path):
    two_mb = 2 * 1024 * 1024
    plan = _stress_plan(
        tmp_path, f"import sys; sys.stdout.write('x' * {two_mb}); sys.stdout.flush()"
    )
    result = execute_plan(plan, timeout_seconds=60)

    assert result.exit_code == 0
    assert result.timed_out is False
    # The 2 MB child output never accumulated in the parent result.
    assert len(result.stdout) <= MAX_OUTPUT_CHARS
    assert len(result.stdout) < 100_000
    assert result.stdout_truncated is True


def test_real_large_stderr_is_bounded(tmp_path):
    two_mb = 2 * 1024 * 1024
    plan = _stress_plan(
        tmp_path,
        f"import sys; sys.stderr.write('e' * {two_mb}); sys.stderr.flush()",
    )
    result = execute_plan(plan, timeout_seconds=60)

    assert result.exit_code == 0
    assert len(result.stderr) <= MAX_OUTPUT_CHARS
    assert result.stderr_truncated is True
    assert result.stdout == ""


def test_real_dual_stream_output_no_deadlock(tmp_path):
    mb = 1024 * 1024
    plan = _stress_plan(
        tmp_path,
        f"import sys\n"
        f"for i in range(4):\n"
        f"    sys.stdout.write('o' * {mb})\n"
        f"    sys.stderr.write('e' * {mb})\n"
        f"sys.stdout.flush(); sys.stderr.flush()",
    )
    result = execute_plan(plan, timeout_seconds=60)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert len(result.stdout) <= MAX_OUTPUT_CHARS
    assert len(result.stderr) <= MAX_OUTPUT_CHARS


def test_real_timeout_returns_bounded_partial_output(tmp_path):
    plan = _stress_plan(
        tmp_path,
        "import sys, time\n"
        "sys.stdout.write('begin' + 'x' * 50000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
        timeout_seconds=2,
    )
    result = execute_plan(plan, timeout_seconds=2)

    assert result.timed_out is True
    assert result.stdout.startswith("begin")
    assert len(result.stdout) <= MAX_OUTPUT_CHARS
    assert result.duration_ms < 30000  # killed promptly, not 60 s


def test_real_execution_python_version(tmp_path):
    """End-to-end with the real subprocess: `python --version`."""
    result = execute_plan(make_plan(tmp_path))

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "Python" in result.stdout
    assert "test-super-secret" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# v0.4.0 release audit: decoding robustness (invalid UTF-8, child encoding)
# ---------------------------------------------------------------------------


def test_real_invalid_utf8_stdout_replaced_not_crash(tmp_path):
    """Invalid UTF-8 on stdout becomes replacement chars; result survives."""
    plan = _stress_plan(
        tmp_path,
        "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfe\\x80end')",
    )
    result = execute_plan(plan, timeout_seconds=30)

    assert result.exit_code == 0
    assert result.stdout.startswith("ok")
    assert result.stdout.endswith("end")
    assert "\ufffd" in result.stdout  # U+FFFD replacement character
    assert result.timed_out is False


def test_real_invalid_utf8_stderr_replaced_not_crash(tmp_path):
    plan = _stress_plan(
        tmp_path,
        "import sys; sys.stderr.buffer.write(b'bad\\xff\\xfe\\x80tail')",
    )
    result = execute_plan(plan, timeout_seconds=30)

    assert result.exit_code == 0
    assert result.stderr.startswith("bad")
    assert result.stderr.endswith("tail")
    assert "\ufffd" in result.stderr


def test_real_invalid_utf8_both_streams_no_deadlock(tmp_path):
    plan = _stress_plan(
        tmp_path,
        "import sys\n"
        "for _ in range(200):\n"
        "    sys.stdout.buffer.write(b'o\\xff\\xfe')\n"
        "    sys.stderr.buffer.write(b'e\\x80\\xff')\n"
        "sys.stdout.flush(); sys.stderr.flush()",
    )
    result = execute_plan(plan, timeout_seconds=30)

    assert result.exit_code == 0
    assert result.timed_out is False  # completed: no pipe deadlock
    assert "\ufffd" in result.stdout
    assert "\ufffd" in result.stderr


def test_invalid_utf8_plus_truncation_plus_timeout(tmp_path):
    """Invalid bytes + bounded retention + timeout all coexist."""
    plan = _stress_plan(
        tmp_path,
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\xff' * 200000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
        timeout_seconds=2,
    )
    result = execute_plan(plan, timeout_seconds=2)

    assert result.timed_out is True
    assert len(result.stdout) <= MAX_OUTPUT_CHARS
    assert result.stdout_truncated is True
    assert result.duration_ms < 30000


def test_child_python_stdio_is_utf8(tmp_path):
    """PYTHONUTF8/PYTHONIOENCODING pin the child stdio codec to UTF-8."""
    plan = _stress_plan(
        tmp_path,
        "import sys; sys.stdout.write('enc=' + sys.stdout.encoding)",
    )
    result = execute_plan(plan, timeout_seconds=30)

    assert result.exit_code == 0
    assert result.stdout.startswith("enc=")
    codec = result.stdout.split("=", 1)[1].strip().lower()
    assert codec.replace("-", "") == "utf8", codec


def test_real_child_env_verification(tmp_path, monkeypatch):
    """Real child confirms secrets/behavior vars absent, essentials present."""
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret-value-123")
    monkeypatch.setenv("PYTHONPATH", "fake-path")
    monkeypatch.setenv("PYTEST_ADDOPTS", "fake-options")

    probe = (
        "import os, sys\n"
        "missing = [k for k in ('OPENAI_API_KEY', 'PYTHONPATH', 'PYTEST_ADDOPTS') if k in os.environ]\n"
        "required = [k for k in ('PATH',) if k not in os.environ]\n"
        "flags = [k for k in ('PYTHONDONTWRITEBYTECODE', 'PYTHONNOUSERSITE', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD') if os.environ.get(k) != '1']\n"
        "sys.exit(0 if not (missing or required or flags) else 7)\n"
    )
    plan = _stress_plan(tmp_path, probe)
    result = execute_plan(plan, timeout_seconds=30)

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "fake-secret-value-123" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Broker convenience
# ---------------------------------------------------------------------------


def test_execute_approved_claims_then_records(tmp_path):
    broker = ExecutionBroker()
    plan = make_plan(tmp_path)
    broker.register(plan)
    broker.approve(plan.id)

    result = execute_approved(broker, plan.id)

    assert result.exit_code == 0
    assert broker.status(plan.id) == "executed"
    assert broker.get_result(plan.id) == result


def test_execute_approved_refuses_unapproved(tmp_path):
    broker = ExecutionBroker()
    plan = make_plan(tmp_path)
    broker.register(plan)

    from harness_agent.execution import BrokerError

    with pytest.raises(BrokerError):
        execute_approved(broker, plan.id)


def test_popen_runner_smoke(tmp_path):
    """The default runner works when called directly."""
    outcome = _popen_process_runner(
        [sys.executable, "--version"],
        cwd=str(tmp_path),
        env=build_child_environment({}),
        timeout=30,
    )
    assert outcome.exit_code == 0
    assert "Python" in outcome.stdout
    assert outcome.timed_out is False
