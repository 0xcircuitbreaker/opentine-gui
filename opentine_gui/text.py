"""Rendering artifact-controlled text safely.

Everything a `.tine` file contains is attacker-controlled as far as this console
is concerned: a run id, a model name, a tool argument and a model's own output
all arrive from a file someone else may have written. The inspectors render as
flat text panels, so a newline in any of those fields would open a row that is
pixel-identical to the console's own — including the Integrity, Signature and
Fork-id rows that state whether the artifact can be trusted at all.

Every interpolated value therefore passes through this module first.
"""

from __future__ import annotations

import json
import re
import time

#: Line breaks and the separators that behave like them (NEL, LS, PS), which
#: would otherwise let artifact text start a new row in a flat text panel.
_LINE_BREAKS = re.compile(r"[\r\n\x0b\x0c\x85  ]+")

#: C0/C1 controls minus the ones handled above. These do not print, but they do
#: move a terminal's cursor and confuse a text layout engine.
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

#: Characters that change how the text around them is *laid out* without being
#: visible themselves: the bidirectional overrides and isolates, the invisible
#: operators, and the zero-width space and no-break space. A right-to-left
#: override inside a model name can print "Signature: verified" out of
#: characters that read as something else entirely, which is the same forgery a
#: newline would commit, one layer down. Zero-width joiners (U+200C/U+200D) and
#: the directional *marks* (U+200E/U+200F) are deliberately kept: they carry
#: meaning in real text, and they cannot reorder a run on their own.
_INVISIBLE = re.compile("[\u200b\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def _sanitize(s: str) -> str:
    """Make one string safe to hand to a text widget.

    Lone surrogates go first: Dear PyGui's native text renderer segfaults on
    them. Then the invisible layout controls, which are the same spoofing
    problem as a newline in a different alphabet.

    The ASCII fast path matters: this runs over every rendered string, including
    payload blocks, and almost all of them are ASCII.
    """
    if s.isascii():
        return s
    return _INVISIBLE.sub("", s.encode("utf-8", "replace").decode("utf-8"))


def _oneline(value: object) -> str:
    """Collapse untrusted text to a single line.

    The run and step inspectors render as one flat text widget, so a newline in
    an artifact-supplied field (a model name, a tag, a prompt) would start a new
    row that is pixel-identical to the console's own — including the
    Integrity/Signature/Fork-id lines that state whether the artifact is
    trustworthy. Every interpolated artifact value goes through here so those
    verdicts cannot be forged by the file they describe.
    """
    text = _LINE_BREAKS.sub(" ", _sanitize(str(value)))
    return _CONTROLS.sub("", text.replace("\t", " ")).strip()


def _indent_block(text: str, prefix: str = "  ") -> list[str]:
    """Render possibly multi-line text with every line indented under a heading."""
    cleaned = _CONTROLS.sub("", _sanitize(str(text)).replace("\t", " "))
    return [f"{prefix}{line}" for line in _LINE_BREAKS.split(cleaned)] or [f"{prefix}"]


def _elide_middle(text: str, n: int) -> str:
    """Shorten keeping both ends, so ids sharing a prefix stay distinguishable.

    Run ids are commonly "demo-complete"/"demo-running" or a shared hash prefix;
    truncating only the tail renders them all identically.
    """
    text = _sanitize(str(text))
    if len(text) <= n:
        return text
    if n <= 3:
        return text[:n]
    keep = n - 1  # one char for the ellipsis
    head = (keep + 1) // 2
    return f"{text[:head]}…{text[len(text) - (keep - head):]}"


def _truncate(v: object, n: int) -> str:
    s = _sanitize(str(v))
    return s if len(s) <= n else s[: n - 3] + "..."


def _format_value(value: object, limit: int) -> str:
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return _truncate(rendered, limit)


def _format_compact(value: object, limit: int) -> str:
    """Single-line rendering — diff rows stay scannable where pretty-printing would not."""
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        rendered = json.dumps(value, sort_keys=True, separators=(", ", ": "))
    except (TypeError, ValueError):
        rendered = str(value)
    return _truncate(rendered, limit)


def _mapping_lines(data: dict, *, limit: int = 700) -> list[str]:
    if not data:
        return ["  (none)"]
    lines: list[str] = []
    for key, value in data.items():
        formatted = _format_value(value, limit)
        key_text = _oneline(key)
        if "\n" in formatted:
            lines.append(f"  {key_text}:")
            lines.extend(_indent_block(formatted, "    "))
        else:
            lines.append(f"  {key_text}: {_oneline(formatted)}")
    return lines


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return ", ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))


def _format_timestamp(timestamp: float) -> str:
    if not timestamp:
        return "(unknown)"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return f"(invalid timestamp: {timestamp!r})"


def _format_age(timestamp: float, *, now: float | None = None) -> str:
    """Coarse "how long ago", for a table column too narrow for a full stamp."""
    if not timestamp:
        return "-"
    try:
        delta = (time.time() if now is None else now) - float(timestamp)
    except (TypeError, ValueError, OverflowError):
        return "-"
    if delta < 0:
        return "future"
    for seconds, suffix in ((86400.0, "d"), (3600.0, "h"), (60.0, "m")):
        if delta >= seconds:
            return f"{int(delta // seconds)}{suffix}"
    return f"{int(delta)}s"


def _format_bytes(size: float) -> str:
    """Human file size. Used where a raw byte count would be noise."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"
