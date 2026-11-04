"""Run exports beyond OpenTelemetry: a standalone HTML page and ATIF JSON.

Both documents are rendered by opentine itself - `tine view` (0.24) and the
ATIF exporter - so the console adds nothing an artifact could exploit, only
the atomic, no-silent-overwrite write the OTel export already uses. The HTML
page additionally gets a Content-Security-Policy that forbids every script and
remote load: opentine escapes artifact text, and the policy is what keeps the
page inert if that ever regressed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentine_gui.otelio import write_bytes_atomic

try:
    from opentine._view_html import render_run as _render_html
except Exception:  # pragma: no cover - only reachable against a foreign opentine
    _render_html = None
try:
    from opentine._atif_export import to_atif as _to_atif
except Exception:  # pragma: no cover
    _to_atif = None

#: No scripts, no remote anything; inline styles and data: images only.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'"
)
_CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'


@dataclass(frozen=True)
class Export:
    """One export format: its menu label, file suffix and renderer."""

    key: str
    label: str
    suffix: str


HTML = Export("html", "HTML page", ".html")
ATIF = Export("atif", "ATIF JSON", ".atif.json")
FORMATS = (HTML, ATIF)


def available(fmt: Export) -> bool:
    return (_render_html if fmt is HTML else _to_atif) is not None


def html_page(run: Any, *, cost: str) -> str:
    """opentine's `tine view` page for `run`, with a script-free CSP."""
    if _render_html is None:
        raise RuntimeError("this opentine has no HTML view")
    page = _render_html(run, cost=cost)
    marker = '<meta charset="utf-8">'
    if marker in page:
        return page.replace(marker, marker + _CSP_META, 1)
    return _CSP_META + page


def atif_document(run: Any) -> dict:
    if _to_atif is None:
        raise RuntimeError("this opentine has no ATIF exporter")
    return _to_atif(run)


def render(fmt: Export, run: Any, *, cost: str) -> bytes:
    if fmt is HTML:
        return html_page(run, cost=cost).encode("utf-8")
    text = json.dumps(atif_document(run), indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def write(fmt: Export, run: Any, target: Path, *, cost: str, overwrite: bool = False) -> int:
    """Render and write one export atomically; returns the bytes written."""
    payload = render(fmt, run, cost=cost)
    write_bytes_atomic(target, payload, overwrite=overwrite)
    return len(payload)


__all__ = ["ATIF", "CSP", "FORMATS", "HTML", "Export", "available", "render", "write"]
