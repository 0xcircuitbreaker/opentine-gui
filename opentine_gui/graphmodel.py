"""The shape of a run's step graph, independent of how it is drawn.

A `.tine` step graph has two kinds of edge. `parent_ids` is the execution
lineage the console has always drawn. `causal_ids` (opentine 0.7.1) names the
non-parent ancestors a step actually required — the edges a v3 repository run
carries with it through an export. They matter here because opentine's fork
keeps the *causal* closure, not the parent closure: a console that draws only
parent edges shows a strict subgraph of the run and then forks something wider
than it showed. Preview and result have to agree, so both kinds are modelled.
"""

from __future__ import annotations

import math
import weakref

from opentine.core import Run, Step, StepKind

from opentine_gui.text import _format_value, _oneline, _sanitize, _truncate

try:
    # opentine 0.6.0's single authority on which steps a fork keeps. It was
    # extracted precisely because a second, independently computed preview can
    # disagree with the fork it previews (it did: it listed the descendants).
    # Private, so a rename must cost the preview, not the console.
    from opentine._graph_analysis import retained_closure as _opentine_retained_closure
except Exception:  # pragma: no cover - depends on the installed opentine
    _opentine_retained_closure = None


def step_provider(step: Step) -> str:
    """Who served this step's call, as recorded, or "" if nothing recorded it.

    `Step.provider` is post-0.7.2. Below that the adapter still wrote the
    provider into the billing calculation, and the rate card id it chose is
    prefixed with it, so the identity is usually recoverable from a 0.7.x
    artifact even though the field is not there.
    """
    direct = getattr(step, "provider", "")
    if isinstance(direct, str) and direct.strip():
        return _oneline(direct)
    billing = getattr(step, "billing", None)
    if isinstance(billing, dict):
        calculation = billing.get("calculation")
        if isinstance(calculation, dict):
            recorded = calculation.get("provider")
            if isinstance(recorded, str) and recorded.strip():
                return _oneline(recorded)
        card = billing.get("rate_card_id")
        if isinstance(card, str) and ":" in card:
            return _oneline(card.split(":", 1)[0])
    return ""


def step_causal_ids(step: Step) -> list[str]:
    """Recorded causal edges, coerced. Absent on an opentine older than 0.7.1."""
    raw = getattr(step, "causal_ids", None)
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def causal_edges(run: Run) -> list[tuple[str, str]]:
    return _view(run, "causal_edges", _read_causal_edges)


def _read_causal_edges(run: Run) -> list[tuple[str, str]]:
    """(cause, effect) pairs inside this run, dangling and duplicate edges dropped.

    A causal edge may name an event that lives in another run — the v3 store is
    one graph across runs — and a hand-edited artifact may name nothing at all.
    `retained_closure` skips those rather than failing, so this does too.
    """
    by_id = {step.id: step for step in run.steps}
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for step in run.steps:
        parents = set(step.parent_ids)
        for cause in step_causal_ids(step):
            if cause not in by_id or cause == step.id or cause in parents:
                continue  # already drawn as lineage, or not in this graph
            pair = (cause, step.id)
            if pair not in seen:
                seen.add(pair)
                edges.append(pair)
    return edges


def retained_slice(run: Run, step_id: str) -> set[str] | None:
    """The steps a fork from `step_id` would keep, or None if it cannot be known.

    Prefers opentine's own helper so the preview cannot drift from the fork. The
    local fallback walks the same two edge classes; it is used only when the
    private helper is not importable.
    """
    if _opentine_retained_closure is not None:
        try:
            return set(_opentine_retained_closure(run, step_id))
        except Exception:
            return None
    try:
        by_id = {step.id: step for step in run.steps}
        if step_id not in by_id:
            return None
        keep: set[str] = set()
        pending = [step_id]
        while pending:
            current = pending.pop()
            step = by_id.get(current)
            if current in keep or step is None:
                continue
            keep.add(current)
            pending.extend([*step.parent_ids, *step_causal_ids(step)])
        return keep
    except Exception:
        return None


def _step_depths(run: Run) -> dict[str, int]:
    return _view(run, "depths", _read_step_depths)


def _read_step_depths(run: Run) -> dict[str, int]:
    """Longest-path depth per step, iterative so 1000+-step chains don't overflow.

    Both edge classes constrain the layout: a causal ancestor drawn to the right
    of the step that needed it would be a picture of the wrong graph. Cycle
    back-edges contribute nothing (steps on a pure cycle get depth 0).
    """
    by_id = {step.id: step for step in run.steps}
    incoming: dict[str, list[str]] = {}
    for step in run.steps:
        parents = [p for p in step.parent_ids if p in by_id and p != step.id]
        for cause in step_causal_ids(step):
            if cause in by_id and cause != step.id and cause not in parents:
                parents.append(cause)
        incoming[step.id] = parents

    memo: dict[str, int] = {}
    for step in run.steps:
        if step.id in memo:
            continue
        # frame: [step_id, parents, next_parent_index, best_parent_depth]
        stack: list[list] = [[step.id, incoming[step.id], 0, -1]]
        on_stack = {step.id}
        while stack:
            frame = stack[-1]
            sid, parents, idx, best = frame
            if idx < len(parents):
                frame[2] += 1
                parent = parents[idx]
                if parent in memo:
                    frame[3] = max(best, memo[parent])
                elif parent not in on_stack:
                    stack.append([parent, incoming[parent], 0, -1])
                    on_stack.add(parent)
            else:
                memo[sid] = best + 1 if best >= 0 else 0
                on_stack.discard(sid)
                stack.pop()
                if stack:
                    stack[-1][3] = max(stack[-1][3], memo[sid])
    return memo


def _graph_stats(run: Run) -> dict[str, int]:
    step_ids = {step.id for step in run.steps}
    child_counts: dict[str, int] = {}
    links = 0
    roots = 0
    for step in run.steps:
        parents = [p for p in step.parent_ids if p in step_ids]
        if parents:
            for parent_id in parents:
                links += 1
                child_counts[parent_id] = child_counts.get(parent_id, 0) + 1
        else:
            roots += 1
    depths = _step_depths(run)
    return {
        "roots": roots,
        "links": links,
        "causal": len(causal_edges(run)),
        "branches": sum(1 for count in child_counts.values() if count > 1),
        "max_depth": max(depths.values(), default=0),
    }


def _node_label(step: Step, *, highlighted: bool = False) -> str:
    """One line naming what a step did.

    Collapsed to a single line before it is returned: this is a node title in the
    graph, but the comparison pane renders the same string into a flat text block
    where a newline would open a row of its own.
    """
    kind = step.kind.value
    prefix = "* " if highlighted else ""
    if step.kind == StepKind.tool:
        name = (step.tool_info or {}).get("name") or step.inputs.get("name", "?")
        return _oneline(f"{prefix}{kind}: {_truncate(name, 16)}")
    if step.kind == StepKind.error:
        error = step.error or {}
        text = (
            error.get("message")
            or error.get("type")
            or step.inputs.get("message")
            or step.outputs.get("error")
            or step.inputs.get("text")
            or ""
        )
    elif step.kind == StepKind.done:
        text = (
            step.outputs.get("answer") or step.outputs.get("text") or step.inputs.get("text") or ""
        )
    elif step.kind == StepKind.model:
        text = step.outputs.get("text") or step.inputs.get("text") or ""
    else:
        text = step.inputs.get("text") or ""
    if text:
        return _oneline(f"{prefix}{kind}: {_truncate(text, 18)}")
    return _oneline(f"{prefix}{kind}: {_sanitize(step.short_id)}")


def step_cost(step: Step) -> float:
    """What this step cost, read the way opentine's own total reads it.

    `Run.total_cost` prefers `billing["known_subtotal_usd"]` over `Step.cost`
    (`_graph_run._step_cost_decimal`). Reading the bare field meant the step
    inspector could state a number that contradicted the run total assembled
    from the same steps, on any artifact whose writer set one and not the other.
    """
    billing = getattr(step, "billing", None)
    if isinstance(billing, dict) and "known_subtotal_usd" in billing:
        try:
            value = float(billing["known_subtotal_usd"])
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is not None and math.isfinite(value) and value >= 0:
            return value
    try:
        return float(step.cost)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _node_subtitle(step: Step) -> str:
    """The second line of a node: who served it, what it cost, how long it took."""
    provider = step_provider(step)
    who = f"{provider}  " if provider else ""
    return f"{who}{step.duration:.2f}s  ${step_cost(step):.4f}"


def _step_haystack(step: Step) -> list[str]:
    # Fields like model_info may be None (or non-str) in third-party .tine
    # files that opentine loads without type-checking; coerce before joining.
    return [
        str(step.id),
        step.kind.value,
        " ".join(str(p) for p in step.parent_ids),
        " ".join(step_causal_ids(step)),
        str(step.model_info or ""),
        step_provider(step),
        _format_value(step.inputs, 500),
        _format_value(step.outputs, 500),
        _format_value(step.tool_info, 500),
        _format_value(step.error, 500),
    ]


def _step_search_text(step: Step) -> str:
    return "\n".join(_step_haystack(step)).lower()


def _step_matches_filter(step: Step, query: str) -> bool:
    return query in _step_search_text(step)


#: Derived views of one run's graph, built once per run object: the depth map,
#: the causal edge list and the per-step search text. Each walks every step, and
#: the panels ask for them repeatedly — the layout, the graph summary, the run
#: inspector and the filter all want the same answer about the same run. A `Run`
#: is hashable and weak-referenceable; a `Step` is neither.
_GRAPH_VIEWS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _view(run: Run, name: str, compute):
    """`compute(run)` once per run object, or straight through if it cannot be cached."""
    try:
        cached = _GRAPH_VIEWS.get(run)
    except TypeError:
        return compute(run)
    if cached is None:
        cached = {}
        try:
            _GRAPH_VIEWS[run] = cached
        except TypeError:
            return compute(run)
    if name not in cached:
        cached[name] = compute(run)
    return cached[name]


def _run_step_texts(run: Run) -> dict[str, str]:
    return _view(
        run, "step_texts", lambda r: {step.id: _step_search_text(step) for step in r.steps}
    )


def _matching_steps(run: Run | None, query: str) -> list[str]:
    if not run or not query:
        return []
    texts = _run_step_texts(run)
    return [
        step.id
        for step in run.steps
        if query in texts.get(step.id, "") or _step_matches_filter(step, query)
    ]


def run_providers(run: Run) -> dict[str, int]:
    """How many steps each recorded provider served."""
    counts: dict[str, int] = {}
    try:
        steps = run.steps
    except Exception:
        return counts
    for step in steps:
        provider = step_provider(step)
        if provider:
            counts[provider] = counts.get(provider, 0) + 1
    return counts
