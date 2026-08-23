"""The run filter: opentine's own query grammar, evaluated in memory.

`tine ls` and `tine search` accept `status:failed model:opus cost:>0.01 tag:bug
after:2026-07-01`. The console accepts the same words for the same meaning, so
what a user learns at the terminal keeps working here.

Two deliberate differences. The grammar only engages when a field prefix is
present, so a plain multi-word search stays the substring match people already
type. And a parsed query is evaluated against the runs already in memory rather
than through `RunIndex.search`, which writes an index file into the user's runs
directory: reading a directory must not modify it.
"""

from __future__ import annotations

import weakref

from opentine.core import Run

from opentine_gui.graphmodel import _step_search_text
from opentine_gui.text import _format_value

try:
    # The same grammar `tine ls` and `tine search` accept. Present since 0.3.0;
    # guarded so a reshaped opentine costs the field syntax, not the app.
    from opentine.core import parse_query
except Exception:  # pragma: no cover - depends on the installed opentine
    parse_query = None


#: Field prefixes opentine's own query grammar understands. The grammar only
#: engages when one of these is present, so a plain multi-word search keeps
#: behaving as the substring match users already have.
QUERY_FIELDS = ("status:", "model:", "tag:", "cost:", "after:", "before:", "text:")


#: The run filter fires on every keystroke on the render thread and otherwise
#: re-serialises every payload of every run. A loaded Run is not mutated, so its
#: lowercased search text is built once and dropped with the run itself.
#: (Step is a frozen dataclass holding lists, so it is unhashable and cannot be
#: cached this way — but per-step search only ever scans the selected run.)
_RUN_HAYSTACKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _run_search_text(run: Run) -> str:
    # status is the one haystack field the GUI mutates in place (pause/resume),
    # so it is part of the cache validity check rather than just its content.
    status = run.status.value
    try:
        cached = _RUN_HAYSTACKS.get(run)
    except TypeError:  # unhashable Run subclass: fall back to recomputing
        cached = None
    if cached is not None and cached[0] == status:
        return cached[1]
    parts = [
        str(run.id),
        run.status.value,
        str(run.model_info or ""),
        str(run.user_prompt or ""),
        str(run.system_prompt or ""),
        " ".join(str(t) for t in run.tags),
        _format_value(run.metadata, 2000),
    ]
    parts.extend(_step_search_text(step) for step in run.steps)
    text = "\n".join(parts).lower()
    try:
        _RUN_HAYSTACKS[run] = (status, text)
    except TypeError:  # not weak-referenceable or unhashable
        pass
    return text


def _looks_like_a_query(query: str) -> bool:
    lowered = query.lower()
    return any(field in lowered for field in QUERY_FIELDS)


def _parsed_query(query: str):
    """opentine's parsed Query for this text, or None to fall back to substring.

    Returns None when the grammar is unavailable (an older opentine), when the
    text carries no field prefix, or when it does not parse.
    """
    if parse_query is None or not _looks_like_a_query(query):
        return None
    try:
        return parse_query(query)
    except Exception:  # QueryError, or anything a future grammar raises
        return None


def _query_error(query: str) -> str:
    """Why a field query did not parse, or "" if it parsed or is plain text.

    Without this a typo like `cost:abc` silently matches nothing, which reads
    as "no such runs" rather than "that is not a valid filter".
    """
    if parse_query is None or not query or not _looks_like_a_query(query):
        return ""
    try:
        parse_query(query)
    except Exception as e:
        return str(e)
    return ""


def _matches_parsed_query(run: Run, parsed) -> bool:
    """Evaluate a parsed Query against a loaded run.

    Mirrors opentine's own match_entry so the console and `tine ls` agree:
    tags must all be present, model is a case-insensitive substring, status is
    exact, cost and created_at are bounds, and every free-text term must appear.
    """
    try:
        tags = {str(t).lower() for t in run.tags}
        if parsed.tags and not all(str(t).lower() in tags for t in parsed.tags):
            return False
        if parsed.model and str(parsed.model).lower() not in str(run.model_info or "").lower():
            return False
        if parsed.status and run.status.value.lower() != str(parsed.status).lower():
            return False
        cost = run.total_cost
        if parsed.cost_min is not None and cost < parsed.cost_min:
            return False
        if parsed.cost_max is not None and cost > parsed.cost_max:
            return False
        created = run.created_at or 0.0
        if parsed.after is not None and created < parsed.after:
            return False
        if parsed.before is not None and created > parsed.before:
            return False
        if parsed.text:
            haystack = _run_search_text(run)
            if not all(str(term).lower() in haystack for term in parsed.text):
                return False
    except Exception:
        return False  # never let a hostile artifact break the run list
    return True


def _run_matches_filter(run: Run, query: str) -> bool:
    if not query:
        return True
    parsed = _parsed_query(query)
    if parsed is not None:
        return _matches_parsed_query(run, parsed)
    try:
        return query in _run_search_text(run)
    except Exception:
        return False
