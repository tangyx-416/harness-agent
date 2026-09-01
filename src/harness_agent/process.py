"""Shared bounded process runner (private utility module).

Single, tested implementation of bounded streaming subprocess capture,
used by BOTH the execution service (v0.3, user-approved execution) and
the Git read service (v0.4, read-only Git awareness):

* argv **list** + ``shell=False``
* ``stdin=DEVNULL`` (no interactive input)
* ``stdout``/``stderr`` PIPE, decoded as UTF-8 (git/py output) with
  ``errors="replace"``
* concurrent daemon reader threads -- the first ``stdout_limit`` /
  ``stderr_limit`` characters per stream are retained, anything beyond
  is drained and discarded, so a chatty child can neither deadlock on a
  full pipe nor grow parent memory without bound
* timeout: the directly managed child is killed on expiry and bounded
  partial output is returned (``timed_out=True``)
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

#: Grace period for reader threads to observe EOF after the child exits.
THREAD_JOIN_SECONDS = 5.0
#: Read chunk size (characters) for the streaming readers.
READ_CHUNK_CHARS = 8192


@dataclass(frozen=True)
class ProcessOutcome:
    """Low-level outcome of one child-process run."""

    exit_code: Optional[int]
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


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
            chunk = stream.read(READ_CHUNK_CHARS)
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


def run_bounded(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
) -> ProcessOutcome:
    """Run *argv* with bounded streaming capture via ``subprocess.Popen``.

    stdout/stderr are consumed concurrently by daemon threads (preventing
    pipe deadlocks); parent memory only ever holds the bounded prefix of
    each stream. On timeout the direct child is killed and the partial
    bounded output is returned.
    """
    proc = subprocess.Popen(
        argv,
        shell=False,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )

    stdout_store = _BoundedStore(stdout_limit)
    stderr_store = _BoundedStore(stderr_limit)
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
            proc.wait(timeout=THREAD_JOIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    for reader in readers:
        reader.join(THREAD_JOIN_SECONDS)

    return ProcessOutcome(
        exit_code=proc.returncode,
        stdout=stdout_store.value(),
        stderr=stderr_store.value(),
        stdout_truncated=stdout_store.truncated,
        stderr_truncated=stderr_store.truncated,
        timed_out=timed_out,
    )
