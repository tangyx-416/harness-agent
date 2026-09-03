"""Host-side safe terminal rendering for untrusted patch preview data.

When the CLI shows a COMPLETE patch diff (or its path/summaries) to the user,
it is rendering **untrusted data**: the ``new_text`` of a proposal may contain
arbitrary characters. A malicious proposal could otherwise embed ANSI escape
sequences, carriage returns, backspaces, bells or Unicode bidi formatting
controls that manipulate the terminal and let the *user see one thing* while
the host writes another.

This module provides :func:`render_untrusted_terminal_text`, which turns
dangerous control / formatting characters into a visible, unambiguous escaped
notation **for display only**. It never changes the real proposed source
content held in :class:`~harness_agent.patch.PatchPlan`; raw source data
stays distinct from what is rendered on screen.

* ``\\n`` is preserved as a line separator (the only structural newline).
* ``\\t`` is preserved (deterministic, not harmful).
* Every other C0 control (NUL, CR, backspace, bell, ESC, ...) is rendered as
  ``\\xNN``.
* ``DEL`` and C1 controls are rendered as ``\\xNN``.
* Unicode bidi formatting controls (``U+202A..U+202E``, ``U+2066..U+2069``)
  are rendered as ``<U+XXXX>`` so source cannot be silently re-ordered.
"""

from __future__ import annotations

# Unicode bidi formatting controls (Trojan Source). Rendered as <U+XXXX>.
_BIDI_CONTROLS = frozenset(
    {
        0x202A,  # LRE
        0x202B,  # RLE
        0x202C,  # PDF
        0x202D,  # LRO
        0x202E,  # RLO
        0x2066,  # LRI
        0x2067,  # RLI
        0x2068,  # FSI
        0x2069,  # PDI
    }
)


_DEFAULT_REPLACEMENTS = {
    "\\": "\\\\",
}


def _escape_ch(char: int) -> str:
    """Return a single safe visible form for one dangerous code point."""
    if char in _BIDI_CONTROLS:
        return f"<U+{char:04X}>"
    if char <= 0xFF:
        return f"\\x{char:02x}"
    return f"\\u{char:04x}"


def render_untrusted_terminal_text(
    text: str, *, keep_tab: bool = True, quote_backslash: bool = True
) -> str:
    """Return a terminal-safe rendering of *text* for display.

    Only ``\\n`` and (optionally) ``\\t`` survive literally; every C0 / C1 /
    ``DEL`` control character and every Unicode bidi formatting control is
    escaped to a visible notation so it cannot drive the terminal. The input
    string is returned unchanged semantically; this is a display transform.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x0A:  # \n -- line separator, allowed
            out.append(ch)
        elif code == 0x09 and keep_tab:  # \t -- allowed, deterministic
            out.append(ch)
        elif code == 0x5C and quote_backslash:  # backslash
            out.append("\\\\")
        elif code < 0x20 or 0x7F <= code <= 0x9F or code in _BIDI_CONTROLS:
            out.append(_escape_ch(code))
        else:
            out.append(ch)
    return "".join(out)
