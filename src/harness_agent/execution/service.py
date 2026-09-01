"""Host-side execution service (v0.3.0).

The ONLY module allowed to spawn a subprocess. Hard guarantees:

* ``subprocess.Popen`` is always called with an argv **list** and
  ``shell=False``.
* ``stdin`` is connected to ``DEVNULL`` -- commands can never wait for
  interactive input from the agent or the CLI session.
* **Bounded streaming capture**: dedicated reader threads consume
  stdout/stderr concurrently while the child runs. Only the first
  :data:`MAX_OUTPUT_CHARS` characters per stream are retained; anything
  beyond is drained and discarded, so an arbitrarily chatty child can
  neither block on a full pipe nor grow parent memory without bound.
* Every execution has a timeout (default 30 s, clamped to 1-60 s). On
  timeout the directly managed subprocess is killed and the bounded
  partial output captured so far is returned with ``timed_out=True``.
* The child environment is scrubbed of secrets AND of variables that
  could silently alter the approved command's behavior
  (:func:`build_child_environment`).

Honesty notes: this is NOT an OS-level sandbox. Commands that execute
repository code (e.g. pytest) still run with the current operating-system
user's privileges. User confirmation is the trust boundary. The timeout
terminates the directly managed subprocess; v0.3.0 does not provide an
OS-level guarantee that every descendant process is terminated.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .broker import ExecutionBroker
from .models import ExecutionPlan, ExecutionResult
from .policy import MAX_OUTPUT_CHARS, clamp_timeout

#: Environment variables removed from the child process verbatim (secrets).
SENSITIVE_ENV_KEYS_EXACT = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AZURE_CLIENT_SECRET",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GIT_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_TOKEN",
        "SLACK_BOT_TOKEN",
    }
)

#: Suffix patterns scrubbed from the child environment (case-insensitive).
SENSITIVE_ENV_KEY_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
)

#: Variables that could silently change the behavior of the approved
#: command (interpreter resolution, startup hooks, pytest plugin/options
#: injection). The user approved a validated argv + cwd -- not whatever
#: these variables would have turned it into.
EXECUTION_BEHAVIOR_ENV_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DEBUG",
    }
)

#: Hardening variables forced into the child environment.
_CHILD_ENV_OVERRIDES = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
}

#: Grace period for reader threads to observe EOF after the child exits.
_THREAD_JOIN_SECONDS = 5.0
#: Read chunk size (characters) for the streaming readers.
_READ_CHUNK_CHARS = 8192


def build_child_environment(parent_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return a copy of *parent_env* with secrets and behavior-injection
    variables removed, plus hardening overrides applied.

    Uses the simpler and more reliable "inherit + selective removal"
    strategy, so benign variables (``PATH``, ``SYSTEMROOT``, ``WINDIR``,
    ``TEMP``, ``TMP``, ``HOME``, ``USERPROFILE``, ``VIRTUAL_ENV``,
    ``LANG`` ...) are preserved while anything that looks like an API
    key, token, secret, password or credential bundle is dropped, and
    interpreter/pytest behavior variables cannot silently reshape the
    approved command.
    """
    source = dict(os.environ) if parent_env is None else dict(parent_env)
    cleaned: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in SENSITIVE_ENV_KEYS_EXACT:
            continue
        if upper in EXECUTION_BEHAVIOR_ENV_KEYS:
            continue
        if upper.endswith(SENSITIVE_ENV_KEY_SUFFIXES):
            continue
        if "_CREDENTIAL" in upper:
            continue
        cleaned[key] = value
    cleaned.update(_CHILD_ENV_OVERRIDES)
    return cleaned


# ---------------------------------------------------------------------------
# Bounded streaming capture
# ---------------------------------------------------------------------------


class _BoundedStore:
    """Accumulates at most ``limit`` characters; flags any overflow.

    Data beyond the limit is never retained -- the caller keeps draining
    the stream so the child can run to completion without blocking.
    """

    __slots__ = ("limit", "parts", "stored", "truncated")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.stored = 0
        self.truncated = False

    def feed(self, text: str) -> None:
        if not text:
            return
        if self.stored < self.limit:
            room = self.limit - self.stored
            if len(text) <= room:
                self.parts.append(text)
                self.stored += len(text)
                return
            self.parts.append(text[:room])
            self.stored = self.limit
        self.truncated = True

    def value(self) -> str:
        return "".join(self.parts)


def _drain_stream(stream, store: _BoundedStore) -> None:
    """Reader-thread body: consume *stream* forever, retain bounded prefix.

    Reading never stops once the limit is reached -- excess data is
    discarded -- so the child's pipe never fills up and the child cannot
    deadlock on write.
    """
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_CHARS)
            if not chunk:
                break
            store.feed(chunk)
    except (OSError, ValueError):
        # Stream closed underneath us (e.g. parent teardown). Bounded
        # partial content collected so far remains valid.
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


@dataclass(frozen=True)
class ProcessOutcome:
    """Low-level outcome of one child-process run."""

    exit_code: Optional[int]
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


#: Injectable runner seam (tests substitute fakes; default is Popen-based).
ProcessRunner = Callable[..., ProcessOutcome]


def _popen_process_runner(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
) -> ProcessOutcome:
    """Run *argv* with bounded streaming capture via ``subprocess.Popen``.

    stdout/stderr are consumed concurrently by daemon threads (preventing
    pipe deadlocks); memory only ever holds the bounded prefix of each
    stream. On timeout the direct child is killed and the partial bounded
    output is returned.
    """
    proc = subprocess.Popen(
        argv,
        shell=False,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )

    stdout_store = _BoundedStore(MAX_OUTPUT_CHARS)
    stderr_store = _BoundedStore(MAX_OUTPUT_CHARS)
    readers = [
        threading.Thread(target=_drain_stream, args=(proc.stdout, stdout_store), daemon=True),
        threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_store), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_THREAD_JOIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    for reader in readers:
        reader.join(_THREAD_JOIN_SECONDS)

    return ProcessOutcome(
        exit_code=proc.returncode,
        stdout=stdout_store.value(),
        stderr=stderr_store.value(),
        stdout_truncated=stdout_store.truncated,
        stderr_truncated=stderr_store.truncated,
        timed_out=timed_out,
    )


def execute_plan(
    plan: ExecutionPlan,
    *,
    timeout_seconds: Optional[int] = None,
    process_runner: Optional[ProcessRunner] = None,
    environment: Optional[dict[str, str]] = None,
) -> ExecutionResult:
    """Execute an approved plan synchronously and return a structured result.

    The plan's frozen argv/cwd are used verbatim -- never anything the
    model supplied after approval. ``display_command`` is display-only
    and is never parsed or executed.

    Args:
        plan: The approved :class:`ExecutionPlan`.
        timeout_seconds: Overrides the plan timeout (still clamped 1-60s).
        process_runner: Injectable low-level runner (tests); default is
            the bounded-streaming :func:`_popen_process_runner`.
        environment: Injectable child environment (tests); default is the
            sanitized :func:`build_child_environment` output.
    """
    run = process_runner if process_runner is not None else _popen_process_runner
    timeout = clamp_timeout(
        timeout_seconds if timeout_seconds is not None else plan.timeout_seconds
    )
    env = environment if environment is not None else build_child_environment()
    argv = [plan.program, *plan.args]

    started = time.monotonic()
    outcome: ProcessOutcome
    try:
        outcome = run(argv, cwd=plan.cwd, env=env, timeout=timeout)
    except OSError as exc:
        # e.g. executable missing -- surfaced as a failed result, never a crash.
        outcome = ProcessOutcome(
            exit_code=None,
            stdout="",
            stderr=f"Failed to launch command: {exc}",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
        )
    duration_ms = int((time.monotonic() - started) * 1000)

    return ExecutionResult(
        plan_id=plan.id,
        command=plan.display_command,
        cwd=plan.cwd,
        risk_level=plan.risk_level,
        approved=True,
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        stdout_truncated=outcome.stdout_truncated,
        stderr_truncated=outcome.stderr_truncated,
        timed_out=outcome.timed_out,
        duration_ms=duration_ms,
    )


def execute_approved(
    broker: ExecutionBroker,
    plan_id: str,
    **service_kwargs: object,
) -> ExecutionResult:
    """Host-side convenience: claim an approved plan, run it, store result.

    The plan is claimed via :meth:`ExecutionBroker.take_for_execution`
    first, so double execution raises instead of running twice.
    """
    plan = broker.take_for_execution(plan_id)
    result = execute_plan(plan, **service_kwargs)  # type: ignore[arg-type]
    broker.record_result(result)
    return result
