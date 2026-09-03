"""User-Approved Source Editing subsystem (v0.6.0).

Security model::

    LLM proposes -> Patch Policy validates -> User approves -> Host applies

* :mod:`.policy`  -- validates raw requests into immutable, single-file PatchPlans.
* :mod:`.models`  -- immutable PatchPlan / PatchReplacement / PatchResult.
* :mod:`.broker`  -- in-memory pending plans; single-use lifecycle.
* :mod:`.service` -- the only place source files are ever written.

The agent can only *prepare* patch proposals; approval and application live in
the trusted host layer (CLI), exactly like execution approval in v0.3.0.
"""

from __future__ import annotations

from .broker import (
    MAX_PATCH_PLANS,
    STATUS_APPLIED,
    STATUS_APPLYING,
    STATUS_APPROVED,
    STATUS_CONFLICT,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    PatchBroker,
    PatchBrokerError,
)
from .models import OP_CREATE, OP_EDIT, PatchPlan, PatchReplacement, PatchResult
from .policy import (
    MAX_CREATE_CONTENT_CHARS,
    MAX_DIFF_CHARS,
    MAX_FILE_SIZE_BYTES,
    MAX_REPLACEMENTS,
    MAX_REPLACEMENT_TEXT_CHARS,
    MAX_SUMMARY_CHARS,
    PatchPolicyError,
    encode_proposed,
    prepare_patch,
)
from .render import render_untrusted_terminal_text
from .service import PatchApplyError, apply_approved, apply_plan

__all__ = [
    "MAX_CREATE_CONTENT_CHARS",
    "MAX_DIFF_CHARS",
    "MAX_FILE_SIZE_BYTES",
    "MAX_PATCH_PLANS",
    "MAX_REPLACEMENTS",
    "MAX_REPLACEMENT_TEXT_CHARS",
    "MAX_SUMMARY_CHARS",
    "OP_CREATE",
    "OP_EDIT",
    "PatchApplyError",
    "PatchBroker",
    "PatchBrokerError",
    "PatchPlan",
    "PatchPolicyError",
    "PatchReplacement",
    "PatchResult",
    "STATUS_APPLIED",
    "STATUS_APPLYING",
    "STATUS_APPROVED",
    "STATUS_CONFLICT",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "apply_approved",
    "apply_plan",
    "encode_proposed",
    "prepare_patch",
    "render_untrusted_terminal_text",
]
