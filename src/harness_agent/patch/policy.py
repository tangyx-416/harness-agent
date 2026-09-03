"""v0.6.0 patch policy: validate immutable, single-file source proposals.

Security model::

    LLM proposes -> Patch Policy validates -> User approves -> Host applies

The policy is the ONLY place that turns a raw model request into an immutable
:class:`PatchPlan`. It enforces:

* **Safe repository-relative path** -- no absolute paths, ``..``, drive/UNC
  letters, NUL bytes, sensitive/ignored locations, binary extensions or
  symlink escapes.
* **Text only** -- UTF-8 (with optional BOM only); binary and other encodings
  are rejected.
* **Single newline convention** -- LF or CRLF preserved; mixed newlines are
  rejected.
* **Exact, unique, non-overlapping replacements** -- every ``old_text`` must
  match the target exactly once, and replacement spans must not overlap, so
  the result is deterministic and independent of the order the model supplied
  them in.
* **A COMPLETE unified diff** -- the whole change is generated and stored. A
  proposal whose diff would exceed the limit is rejected outright; there is
  never a truncated approvable patch (no hidden changes).
* **Create** targets a file that does not exist yet; its parent must already
  be a directory; it never overwrites.

Nothing here touches the filesystem except reading (to compute hashes/diffs) --
writing is reserved for :mod:`.service` under host approval.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from ..tools.path_utils import (
    is_known_binary_extension,
    path_is_dangerous,
    sniff_binary_content,
)
from .models import OP_CREATE, OP_EDIT, PatchPlan, PatchReplacement

# ---------------------------------------------------------------------------
# Limits (single source of truth)
# ---------------------------------------------------------------------------

#: Largest file (bytes) an edit proposal may target.
MAX_FILE_SIZE_BYTES = 256 * 1024
#: Largest allowed number of replacements in one edit.
MAX_REPLACEMENTS = 64
#: Characters allowed in one ``old_text`` / ``new_text`` value.
MAX_REPLACEMENT_TEXT_CHARS = 5000
#: Largest proposed content (characters) for a ``create`` plan.
MAX_CREATE_CONTENT_CHARS = 50_000
#: Hard cap on the COMPLETE unified diff. A proposal whose diff is larger is
#: rejected -- never returned truncated.
MAX_DIFF_CHARS = 100_000
#: Characters allowed in one user-visible summary line.
MAX_SUMMARY_CHARS = 500


class PatchPolicyError(ValueError):
    """Raised when a raw patch request cannot produce a valid PatchPlan."""


# ---------------------------------------------------------------------------
# Small typing helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReplacementMatch:
    start: int
    end: int
    new_text: str


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def _reject(reason: str) -> "PatchPolicyError":
    return PatchPolicyError(reason)


#: Windows reserved device-name stems (case-insensitive). A path component may
#: not be (or begin with, as a ``.ext`` body like ``CON.txt`` / ``NUL.py``) one
#: of these, because Windows still interprets the stem as a device regardless
#: of extension.
_WINDOWS_RESERVED_DEVICE_STEMS = frozenset(
    {
        "con", "prn", "aux", "nul",
        "clock$",
        # COM1..COM9, LPT1..LPT9
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def _component_has_control_chars(part: str) -> bool:
    """True when a path component carries any C0 control char, DEL, or C1."""
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in part)


def _is_windows_reserved_component(part: str) -> bool:
    """Return True when *part* names a Windows reserved device.

    ``CON``, ``CON.txt``, ``nul.PY`` are all devices on Windows because the
    segment stem is interpretive. We refuse the whole component to stay safe
    on the user's primary (Windows) platform.
    """
    lowered = part.lower()
    stem = lowered.split(".", 1)[0] if "." in lowered else lowered
    return stem in _WINDOWS_RESERVED_DEVICE_STEMS


def _normalize_repo_path(user_path: str, root: Path) -> str:
    """Validate *user_path* is a safe repository-relative path.

    Returns the normalized POSIX-relative path. Raises otherwise.
    """
    raw = (user_path or "").strip()
    if not raw:
        raise _reject("A repository-relative file path is required.")
    if raw in {".", "/"}:
        raise _reject(f"Invalid file path ({raw!r}): must name a file.")
    if "\x00" in raw:
        raise _reject("File path must not contain NUL characters.")

    # Reject absolute paths, drive letters, UNC, and ../ traversal verbatim.
    if raw.startswith(("/", "\\")) or ":" in raw:
        raise _reject(f"Absolute or drive path refused ({raw!r}).")
    parts = raw.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise _reject(f"Path traversal (..) refused ({raw!r}).")
    parts = [part for part in parts if part != ""]
    if not parts:
        raise _reject(f"Invalid file path ({raw!r}).")

    # Hardened Windows/terminal path rules for source editing:
    # * no C0/C1 controls or DEL in any component (prevents diff-header
    #   injection like ``--- a/evil`` / ``+++ fake.py`` from newline paths);
    # * no trailing space/dot in a component (Windows canonicalizes them,
    #   creating ambiguous, invisible aliases);
    # * no Windows reserved device names (CON/NUL/COM1/... even with an
    #   extension) or NTFS Alternate Data Stream colons (already blocked by the
    #   drive-letter ``:`` check above, listed here for documentation).
    for part in parts:
        if _component_has_control_chars(part):
            raise _reject(f"Path contains control characters ({raw!r}).")
        if part.endswith(" ") or part.endswith("."):
            raise _reject(
                f"Path component must not end in a space or dot ({raw!r})."
            )
        if _is_windows_reserved_component(part):
            raise _reject(f"Windows reserved device name refused ({raw!r}).")

    rel = "/".join(parts)
    resolved = (root / rel).resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError:
        raise _reject(f"File path escapes the repository root ({raw!r}).")
    if resolved.is_symlink():
        raise _reject(f"Symlink target refused ({rel!r}).")

    return rel


def _validate_target(rel: str, root: Path, resolved: Path) -> None:
    if path_is_dangerous(resolved, root, target_is_dir=False):
        raise _reject(f"Refused ({rel!r}): sensitive or ignored location.")
    suffix = resolved.suffix.lower()
    if is_known_binary_extension(suffix):
        raise _reject(f"Refused ({rel!r}): binary extension.")
    # Parent chain must not escape the repo via symlink.
    try:
        parent_real = resolved.parent.resolve(strict=False)
        parent_real.relative_to(root)
    except ValueError:
        raise _reject(f"Refused ({rel!r}): parent resolves outside the repository.")


# ---------------------------------------------------------------------------
# Encoding / newline helpers
# ---------------------------------------------------------------------------


def _decode_edit_bytes(data: bytes) -> tuple[str, bool]:
    """Decode UTF-8 (with optional BOM). Returns (text, has_bom) or raises."""
    has_bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if has_bom else data
    if b"\x00" in body:
        raise _reject("Binary content refused (NUL byte detected).")
    try:
        return body.decode("utf-8"), has_bom
    except UnicodeDecodeError:
        raise _reject("Unsupported encoding: only UTF-8 (or UTF-8 BOM) is allowed.")


def _ensure_encodable(text: str, *, what: str = "Patch content") -> None:
    """Raise a structured rejection if *text* cannot be encoded as UTF-8.

    Guards against lone surrogates / invalid code points supplied by the
    model, which would otherwise surface as an uncaught ``UnicodeEncodeError``
    out of the patch tool. Returns normally when encodable.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _reject(f"{what} cannot be encoded as UTF-8.")


def _detect_newline(text: str) -> str:
    """Return the file's single newline convention; reject mixed newlines."""
    crlf = text.count("\r\n")
    lf = text.count("\n")
    cr_alone = text.count("\r") - crlf
    if cr_alone:
        raise _reject("Mixed or CR-only newlines refused; only LF or CRLF is allowed.")
    if crlf and lf != crlf:
        raise _reject("Mixed LF/CRLF newlines refused.")
    if crlf:
        return "\r\n"
    return "\n"


# ---------------------------------------------------------------------------
# Replacement machinery (exact, unique, non-overlapping, order independent)
# ---------------------------------------------------------------------------


def _apply_replacements(
    text: str, replacements: tuple[PatchReplacement, ...]
) -> str:
    matches: list[_ReplacementMatch] = []
    for r in replacements:
        old, new = r.old_text, r.new_text
        if old == new:
            raise _reject("A replacement whose old_text equals its new_text is a no-op.")
        start = 0
        positions: list[int] = []
        while True:
            idx = text.find(old, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + len(old)
        if not positions:
            raise _reject(
                f"Replacement old_text does not match the file ({old[:40]!r})."
            )
        if len(positions) > 1:
            raise _reject(
                "Replacement old_text is not unique; provide more context "
                f"({old[:40]!r})."
            )
        matches.append(
            _ReplacementMatch(positions[0], positions[0] + len(old), new)
        )

    matches.sort(key=lambda m: m.start)
    for i in range(1, len(matches)):
        if matches[i].start < matches[i - 1].end:
            raise _reject("Overlapping replacements refused.")

    parts: list[str] = []
    cursor = 0
    for m in matches:
        parts.append(text[cursor : m.start])
        parts.append(m.new_text)
        cursor = m.end
    parts.append(text[cursor:])
    return "".join(parts)


def _has_terminal_controls(text: str) -> bool:
    """True when *text* carries any C0/C1/DEL control or bidi formatting."""
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return True
        if 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            return True
    return False


def _normalize_summaries(summaries: object) -> tuple[str, ...]:
    if isinstance(summaries, str) or not isinstance(summaries, (list, tuple)):
        raise _reject("summaries must be a list of short strings.")
    clean: list[str] = []
    for item in summaries:
        if not isinstance(item, str):
            raise _reject("Each summary must be a string.")
        stripped = item.strip()
        if not stripped:
            raise _reject("Summary must not be blank.")
        if "\x00" in stripped:
            raise _reject("Summaries must not contain NUL characters.")
        if _has_terminal_controls(stripped):
            raise _reject(
                "Summary must be plain printable Unicode "
                "(no control or bidi formatting characters)."
            )
        _ensure_encodable(stripped, what="Summary")
        if len(stripped) > MAX_SUMMARY_CHARS:
            raise _reject(
                f"Summary exceeds the {MAX_SUMMARY_CHARS}-character limit."
            )
        clean.append(stripped)
    if not clean:
        raise _reject("At least one summary is required.")
    return tuple(clean)


def _normalize_replacements(replacements: object) -> tuple[PatchReplacement, ...]:
    if not isinstance(replacements, (list, tuple)):
        raise _reject("replacements must be a list of {old_text, new_text} objects.")
    if not replacements:
        raise _reject("At least one replacement is required for an edit.")
    if len(replacements) > MAX_REPLACEMENTS:
        raise _reject(f"Replacement limit is {MAX_REPLACEMENTS}.")
    clean: list[PatchReplacement] = []
    for item in replacements:
        if not isinstance(item, dict):
            raise _reject("Each replacement must be an object with old_text/new_text.")
        old = item.get("old_text")
        new = item.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str):
            raise _reject("Each replacement needs string old_text and new_text.")
        if "\x00" in old or "\x00" in new:
            raise _reject("Replacement text must not contain NUL characters.")
        # Harden line endings so the model can send either \n or \r\n.
        old = old.replace("\r\n", "\n").replace("\r", "\n")
        new = new.replace("\r\n", "\n").replace("\r", "\n")
        if not old:
            raise _reject("Empty old_text refused.")
        if len(old) > MAX_REPLACEMENT_TEXT_CHARS or len(new) > MAX_REPLACEMENT_TEXT_CHARS:
            raise _reject(
                f"Replacement text exceeds the {MAX_REPLACEMENT_TEXT_CHARS}-character "
                "limit."
            )
        _ensure_encodable(old, what="replacement old_text")
        _ensure_encodable(new, what="replacement new_text")
        clean.append(PatchReplacement(old_text=old, new_text=new))
    return tuple(clean)


def _normalize_create_content(content: object) -> str:
    if not isinstance(content, str):
        raise _reject("create requires string content.")
    if "\x00" in content:
        raise _reject("Content must not contain NUL characters.")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > MAX_CREATE_CONTENT_CHARS:
        raise _reject(
            f"Create content exceeds the {MAX_CREATE_CONTENT_CHARS}-character limit."
        )
    _ensure_encodable(normalized, what="create content")
    body = normalized.encode("utf-8")
    if len(body) > MAX_FILE_SIZE_BYTES:
        raise _reject(
            f"Create content exceeds the {MAX_FILE_SIZE_BYTES}-byte encoded limit."
        )
    return normalized


_NO_NEWLINE_MARKER = "\\ No newline at end of file"


def _insert_no_newline_markers(
    diff_lines: list[str], old: str | None, new: str
) -> list[str]:
    """Insert ``\\ No newline at end of file`` markers into a unified diff.

    ``difflib`` does not flag a removed or added final newline, so a change
    like ``"hello\\n" -> "hello"`` would otherwise render identically and the
    user could approve believing the final newline was kept. We add git-style
    markers so the approval preview is *visually complete*. This only affects
    the display draft, never the stored proposed content.
    """
    out = list(diff_lines)
    last_add = last_rem = -1
    for i, ln in enumerate(out):
        if ln.startswith("+"):
            last_add = i
        elif ln.startswith("-") and not ln.startswith("---"):
            last_rem = i

    insert_after: dict[int, str] = {}
    if old is not None and old and not old.endswith("\n") and last_rem >= 0:
        insert_after[last_rem] = _NO_NEWLINE_MARKER + "\n"
    if new and not new.endswith("\n") and last_add >= 0:
        insert_after[last_add] = _NO_NEWLINE_MARKER + "\n"

    if not insert_after:
        return out
    result: list[str] = []
    for i, ln in enumerate(out):
        result.append(ln)
        if i in insert_after:
            result.append(insert_after[i])
    return result


def _build_diff(
    rel: str, old: str | None, new: str, *, from_devnull: bool
) -> str:
    """Build a COMPLETE unified diff for one file (display draft).

    Includes visual ``No newline at end of file`` markers so a removed or added
    final newline is never invisible to the approving user (see
    :func:`_insert_no_newline_markers`).
    """
    a_name = "/dev/null" if from_devnull else f"a/{rel}"
    b_name = f"b/{rel}"
    old_lines = [] if from_devnull else (old or "").splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    lines = list(
        unified_diff(
            old_lines,
            new_lines,
            fromfile=a_name,
            tofile=b_name,
            n=3,
            lineterm="\n",
        )
    )
    return "".join(_insert_no_newline_markers(lines, old, new))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_diff(prepare_plan: PatchPlan) -> str:
    """Regenerate/hold the complete diff on the plan (idempotent, internal)."""
    return prepare_plan.diff


def prepare_patch(
    *,
    path: str,
    operation: str,
    root: Path,
    replacements: object = None,
    content: str | None = None,
    summaries: object = (),
) -> PatchPlan:
    """Validate a raw source-edit request and return an immutable PatchPlan.

    This never writes. It reads the target only to compute hashes and the
    complete diff.

    Raises:
        PatchPolicyError: Any validation failure (denial).
    """
    rel = _normalize_repo_path(path, root)
    resolved = (root / rel).resolve(strict=False)
    _validate_target(rel, root, resolved)

    clean_summaries = _normalize_summaries(summaries)

    if operation not in (OP_EDIT, OP_CREATE):
        raise _reject(
            f"Unsupported operation ({operation!r}); only 'edit' and 'create'."
        )

    if operation == OP_CREATE:
        return _prepare_create(rel, resolved, root, content, clean_summaries)

    return _prepare_edit(rel, resolved, root, replacements, clean_summaries)


def _prepare_create(
    rel: str,
    resolved: Path,
    root: Path,
    content: object,
    summaries: tuple[str, ...],
) -> PatchPlan:
    if not resolved.parent.is_dir():
        raise _reject(f"Cannot create {rel!r}: its parent directory does not exist.")
    if not resolved.parent.is_dir() or resolved.parent.is_symlink():
        raise _reject(f"Cannot create {rel!r}: invalid parent directory.")
    if resolved.exists():
        raise _reject(f"Cannot create {rel!r}: target already exists (never overwrite).")

    proposed = _normalize_create_content(content)
    proposed_bytes = proposed.encode("utf-8")
    proposed_sha = hashlib.sha256(proposed_bytes).hexdigest()
    diff = _build_diff(rel, None, proposed, from_devnull=True)
    _check_diff_size(diff)

    return PatchPlan(
        id=uuid.uuid4().hex,
        operation=OP_CREATE,
        repo_path=rel,
        abs_path=str(resolved),
        summaries=summaries,
        replacements=(),
        proposed_content=proposed,
        has_bom=False,
        original_sha256="",
        proposed_sha256=proposed_sha,
        diff=diff,
        created_at=time.time(),
    )


def _prepare_edit(
    rel: str,
    resolved: Path,
    root: Path,
    replacements: object,
    summaries: tuple[str, ...],
) -> PatchPlan:
    if not resolved.is_file():
        raise _reject(f"Cannot edit {rel!r}: file does not exist.")
    stat = resolved.stat()
    if stat.st_size > MAX_FILE_SIZE_BYTES:
        raise _reject(
            f"Refused ({rel!r}): file exceeds the "
            f"{MAX_FILE_SIZE_BYTES}-byte size limit."
        )
    data = resolved.read_bytes()
    text, has_bom = _decode_edit_bytes(data)
    if sniff_binary_content(data):
        raise _reject(f"Binary content refused ({rel!r}).")
    newline = _detect_newline(text)

    clean_replacements = _normalize_replacements(replacements)

    # Agent-visible text semantics must match the policy's matching semantics
    # (指令9 §16/§K): readers split lines, so an agent copies ``old_text`` in a
    # CRLF-normalized-agnostic form (LF). We therefore match against an
    # LF-normalized copy of the target and reconstruct the original CRLF bytes
    # afterwards, so a CRLF file edited by an LF ``old_text`` still preserves
    # every original newline byte exactly.
    if newline == "\r\n":
        match_text = text.replace("\r\n", "\n")
    else:
        match_text = text
    proposed_lf = _apply_replacements(match_text, clean_replacements)
    if newline == "\r\n":
        proposed = proposed_lf.replace("\n", "\r\n")
    else:
        proposed = proposed_lf

    _ensure_encodable(proposed, what="proposed content")
    proposed_bytes = (b"\xef\xbb\xbf" if has_bom else b"") + proposed.encode("utf-8")
    if len(proposed_bytes) > MAX_FILE_SIZE_BYTES:
        raise _reject(
            f"Refused ({rel!r}): proposed result exceeds the "
            f"{MAX_FILE_SIZE_BYTES}-byte size limit."
        )
    proposed_sha = hashlib.sha256(proposed_bytes).hexdigest()
    original_sha = hashlib.sha256(data).hexdigest()
    diff = _build_diff(rel, text, proposed, from_devnull=False)
    _check_diff_size(diff)

    return PatchPlan(
        id=uuid.uuid4().hex,
        operation=OP_EDIT,
        repo_path=rel,
        abs_path=str(resolved),
        summaries=summaries,
        replacements=clean_replacements,
        proposed_content=proposed,
        has_bom=has_bom,
        original_sha256=original_sha,
        proposed_sha256=proposed_sha,
        diff=diff,
        created_at=time.time(),
    )


def _check_diff_size(diff: str) -> None:
    if len(diff) > MAX_DIFF_CHARS:
        raise _reject(
            "The generated diff is too large. The proposal is refused rather "
            "than returned truncated (no hidden changes)."
        )


def encode_proposed(plan: PatchPlan) -> bytes:
    """Return the exact bytes the host will write for *plan*."""
    body = plan.proposed_content.encode("utf-8")
    if plan.has_bom:
        return b"\xef\xbb\xbf" + body
    return body
