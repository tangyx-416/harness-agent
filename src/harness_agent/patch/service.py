"""Host-side patch application service (v0.6.0).

The ONLY module allowed to write repository source files (other than the
host-only CLI helpers that call it). It performs a single, atomic, previously
approved write for one immutable :class:`PatchPlan`.

Hard guarantees (see :mod:`.policy` for the validation that ran at prepare):

* **Edit** -- before writing, the target's current bytes are re-hashed and
  re-validated. A mismatch with ``original_sha256`` (stale/conflicting
  content) or a symlink introduced after prepare means NO write occurs and the
  outcome is ``conflict``. Otherwise the new content is written to a temp file
  in the same directory, flushed/fsynced, its original mode preserved, and
  atomically ``os.replace``-d over the target. On failure the original is
  untouched and the temp file is cleaned up (outcome ``failed``).
* **Create** -- the target is created with exclusive semantics (``O_CREAT |
  O_EXCL``) so an existing file is never overwritten; if the target appeared
  between prepare and apply the outcome is ``conflict``. UTF-8, LF-normalized,
  no BOM. On failure any partial file we created is removed (``failed``).

Honesty notes: this is single-file, non-transactional writes on a normal file
system. ``os.replace`` and ``O_EXCL`` give strong local guarantees but are not
an OS-level transactional journal; hostile concurrent processes racing us on
the same path are out of scope. The trust boundary is host-side user approval.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
import tempfile
from pathlib import Path

from .broker import PatchBroker, PatchBrokerError
from .models import (
    OP_CREATE,
    OP_EDIT,
    STATUS_APPLIED,
    STATUS_CONFLICT,
    STATUS_FAILED,
    PatchPlan,
    PatchResult,
)
from .policy import encode_proposed

#: Default mode applied to newly created files (POSIX 0644).
_CREATE_MODE = 0o644


class PatchApplyError(Exception):
    """Raised for infrastructure-level apply failures (never file conflict)."""


# ---------------------------------------------------------------------------
# Core apply (deterministic, injectable-ish, testable)
# ---------------------------------------------------------------------------


def _revalidate_path(plan: PatchPlan, root: Path) -> str | None:
    """Return a conflict reason if the target path is no longer safe to write."""
    target = Path(plan.abs_path)
    if target.is_symlink():
        return "target became a symlink after prepare"
    try:
        resolved = (root / plan.repo_path).resolve(strict=False)
        resolved.relative_to(root)
    except ValueError:
        return "path now escapes the repository root"
    return None


def apply_plan(plan: PatchPlan, root: Path) -> PatchResult:
    """Apply one immutable, approved plan. Returns a structured PatchResult.

    *status* is ``applied`` only when the file was actually written and the
    resulting SHA-256 matches ``proposed_sha256``. Otherwise it is ``conflict``
    (no write) or ``failed`` (write attempted but errored).
    """
    if plan.operation == OP_EDIT:
        return _apply_edit(plan, root)
    return _apply_create(plan, root)


def _apply_edit(plan: PatchPlan, root: Path) -> PatchResult:
    conflict = _revalidate_path(plan, root)
    if conflict is not None:
        return _result(plan, STATUS_CONFLICT, None, conflict)

    target = Path(plan.abs_path)
    try:
        if not target.is_file():
            return _result(plan, STATUS_CONFLICT, None, "target file missing")
        current = target.read_bytes()
    except OSError as exc:
        return _result(plan, STATUS_FAILED, None, f"failed to read target: {exc}")

    if hashlib.sha256(current).hexdigest() != plan.original_sha256:
        return _result(
            plan,
            STATUS_CONFLICT,
            None,
            "target content changed since prepare (SHA-256 mismatch); no write",
        )

    proposed_bytes = encode_proposed(plan)
    temp_path: Path | None = None
    try:
        original_mode = stat_module.S_IMODE(target.stat().st_mode)
    except OSError as exc:
        return _result(plan, STATUS_FAILED, None, f"stat failed: {exc}")

    try:
        temp_path = _write_temp(target, proposed_bytes, original_mode)
        os.replace(temp_path, target)
        temp_path = None
    except OSError as exc:
        _cleanup(temp_path)
        return _result(
            plan,
            STATUS_FAILED,
            None,
            f"atomic replace failed (original unchanged): {exc}",
        )

    try:
        written_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as exc:
        return _result(plan, STATUS_FAILED, None, f"failed to verify write: {exc}")
    if written_sha != plan.proposed_sha256:
        return _result(plan, STATUS_CONFLICT, None, "written bytes mismatch (unexpected)")
    return _result(plan, STATUS_APPLIED, written_sha, "Patch applied.")


def _apply_create(plan: PatchPlan, root: Path) -> PatchResult:
    conflict = _revalidate_path(plan, root)
    if conflict is not None:
        return _result(plan, STATUS_CONFLICT, None, conflict)

    target = Path(plan.abs_path)
    if target.exists():
        return _result(
            plan,
            STATUS_CONFLICT,
            None,
            "target appeared after prepare; existing file is never overwritten",
        )

    proposed_bytes = encode_proposed(plan)
    fd: int | None = None
    created_here = False
    written_ok = False
    try:
        try:
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _CREATE_MODE,
            )
            created_here = True
            with os.fdopen(fd, "wb") as fh:
                fd = None
                fh.write(proposed_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            written_ok = True
        except FileExistsError:
            return _result(
                plan,
                STATUS_CONFLICT,
                None,
                "target exists; existing file is never overwritten",
            )
        except OSError as exc:
            return _result(plan, STATUS_FAILED, None, f"create failed: {exc}")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created_here and not written_ok and target.exists():
            _safe_unlink(target)

    written_sha = hashlib.sha256(proposed_bytes).hexdigest()
    if written_sha != plan.proposed_sha256:
        _safe_unlink(target)
        return _result(plan, STATUS_CONFLICT, None, "written bytes mismatch (unexpected)")
    return _result(plan, STATUS_APPLIED, written_sha, "File created.")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_temp(target: Path, data: bytes, mode: int) -> Path:
    """Write *data* to a sibling temp file with mode, fsync, and return it."""
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    fd_open = True
    try:
        fh = os.fdopen(fd, "wb")
        fd_open = False
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_path, mode)
    except OSError:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        _cleanup(temp_path)
        raise
    return temp_path


def _cleanup(temp_path: Path | None) -> None:
    if temp_path is None:
        return
    try:
        temp_path.unlink()
    except OSError:
        pass


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _result(
    plan: PatchPlan, status: str, sha256: str | None, message: str
) -> PatchResult:
    return PatchResult(
        plan_id=plan.id,
        repo_path=plan.repo_path,
        operation=plan.operation,
        status=status,
        sha256=sha256,
        message=message,
    )


# ---------------------------------------------------------------------------
# Host convenience
# ---------------------------------------------------------------------------


def apply_approved(broker: PatchBroker, plan_id: str, root: Path) -> PatchResult:
    """Host-side convenience: claim an approved plan, apply it, store result.

    The plan is claimed via :meth:`PatchBroker.take_for_apply` first, so a
    double apply raises instead of writing twice.
    """
    plan = broker.take_for_apply(plan_id)
    try:
        result = apply_plan(plan, root)
    except PatchBrokerError:
        raise
    except Exception as exc:  # never leak unhandled failures past the host
        result = _result(plan, STATUS_FAILED, None, f"apply failed unexpectedly: {exc}")
    broker.record_application(
        plan_id,
        status=result.status,
        sha256=result.sha256,
        message=result.message,
    )
    return result
