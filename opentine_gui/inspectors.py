"""What the console says about a run, a step, and a comparison.

Every line here is assembled from artifact-controlled data and rendered into a
flat text panel, so two rules hold throughout: interpolated values go through
`_oneline` (a newline would otherwise open a row indistinguishable from the
console's own trust verdicts), and every reader fails open (a hostile or
third-party artifact must cost one missing line, never the panel).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from opentine.core import Run, Step

from opentine_gui.graphmodel import (
    _graph_stats,
    _matching_steps,
    _node_label,
    causal_edges,
    run_providers,
    step_causal_ids,
    step_provider,
)
from opentine_gui.sources import _signature_verdict, _verify_integrity_cached
from opentine_gui.text import (
    _format_compact,
    _format_counts,
    _format_timestamp,
    _format_value,
    _indent_block,
    _mapping_lines,
    _oneline,
    _truncate,
)
from opentine_gui.trust import (
    coverage_lines,
    integrity_line,
    signature_line,
    signature_scheme,
)
from opentine_gui.trust import verifier as trust_verifier

try:
    # opentine 0.4.0's only surface for the fork-id check. It is not exported
    # from opentine.core, the package root, Run, the CLI or MCP, so a private
    # import is the only option; a later rename must degrade, not crash.
    from opentine._fork_identity import verify_fork_id
except Exception:  # pragma: no cover - depends on the installed opentine
    verify_fork_id = None


# ---------------------------------------------------------------- cost/pricing


def _pricing_incompleteness(run: Run) -> tuple[bool, int, int]:
    """(incomplete, unpriced, total) from manifest.pricing; fails open on any shape.

    opentine records when its catalog could not price an invocation, which makes
    total_cost a lower bound rather than the spend. Nothing validates the shape
    of manifest.pricing, so every branch here tolerates arbitrary JSON.
    """
    try:
        pricing = run.manifest.get("pricing")
    except Exception:
        return (False, 0, 0)
    if not isinstance(pricing, dict) or pricing.get("complete") is not False:
        return (False, 0, 0)  # absent, True, or unreadable -> no caveat
    raw = pricing.get("invocations")
    if not isinstance(raw, list):
        return (True, 0, 0)  # the flag stands; counts unknown
    items = [i for i in raw if isinstance(i, dict)]
    unpriced = sum(1 for i in items if i.get("status") not in ("complete", "unmetered"))
    return (True, unpriced, len(items))


def _recorded_cost_state(run: Run) -> str:
    """How to read this run's recorded cost: "priced", "partial" or "unrecorded".

    A run whose steps carry no billing at all — an imported trace, a run
    captured against an unmetered local model — legitimately sums to zero. That
    is not the same claim as "this run cost nothing", and the console must not
    make the second claim on the strength of the first.
    """
    try:
        if _pricing_incompleteness(run)[0]:
            return "partial"
        steps = run.steps
        billable = [s for s in steps if s.kind.value == "model"]
        if not billable:
            return "priced" if run.total_cost else "unrecorded"
        if run.total_cost:
            return "priced"
        if any(getattr(s, "billing", None) for s in billable):
            return "priced"  # billed and genuinely free (an unmetered local model)
        return "unrecorded"
    except Exception:
        return "priced"


def _cost_text(run: Run, amount: float | None = None) -> str:
    """Cost with a marker when opentine did not price the whole run.

    `>=` means opentine priced some invocations and not others, so the number is
    a floor. `no cost recorded` means nothing was priced at all, which is what
    an imported trace looks like — printing `$0.0000` there states a spend the
    artifact never claimed.
    """
    value = run.total_cost if amount is None else amount
    state = _recorded_cost_state(run)
    if amount is None and state == "unrecorded":
        return "no cost recorded"
    return f"{'>=' if state == 'partial' else ''}${value:.4f}"


def _cost_cell(run: Run) -> str:
    """The run table's cost column: the same honesty, in less width."""
    state = _recorded_cost_state(run)
    if state == "unrecorded":
        return "-"
    return f"{'>=' if state == 'partial' else ''}${run.total_cost:.4f}"


def _pricing_line(run: Run) -> str:
    incomplete, unpriced, total = _pricing_incompleteness(run)
    if not incomplete:
        if _recorded_cost_state(run) == "unrecorded":
            return (
                "Pricing: nothing priced at capture "
                "(imported or unmetered - use Run > Price this run)"
            )
        return ""
    if total:
        return (
            f"Pricing: incomplete - {unpriced} of {total} invocation(s) unpriced "
            "(cost is a lower bound)"
        )
    return "Pricing: incomplete (cost is a lower bound)"


def _cost_attribution_lines(run: Run, *, limit: int = 4) -> list[str]:
    """Where the money went, when more than one model or kind spent any."""
    try:
        breakdown = run.cost_breakdown()
    except Exception:
        return []
    lines: list[str] = []
    for label, mapping in (("model", breakdown.by_model), ("kind", breakdown.by_kind)):
        spenders = sorted(((k, v) for k, v in (mapping or {}).items() if v), key=lambda kv: -kv[1])
        if len(spenders) < 2:
            continue  # a single spender adds nothing over the Cost line
        shown = ", ".join(
            f"{_oneline(k) or '(unattributed)'} ${v:.4f}" for k, v in spenders[:limit]
        )
        if len(spenders) > limit:
            shown += f", +{len(spenders) - limit} more"
        lines.append(f"Cost by {label}: {shown}")
    return lines


def _budget_line(run: Run) -> str:
    """Configured budget with the incurred total beside each limit, if any."""
    try:
        budget = run.budget()
    except Exception:
        return ""
    if budget is None:
        return ""
    parts: list[str] = []
    if budget.max_cost is not None:
        # The table cell renderer, not the prose one: this is an
        # incurred/limit pair, and "no cost recorded/$0.5000" puts a sentence
        # where a number belongs.
        parts.append(f"cost {_cost_cell(run)}/${budget.max_cost:.4f}")
    if budget.max_steps is not None:
        parts.append(f"steps {len(run.steps)}/{budget.max_steps}")
    if budget.max_duration is not None:
        parts.append(f"duration {run.total_duration:.1f}s/{budget.max_duration:.1f}s")
    if budget.max_usage is not None:
        parts.append(f"tokens {run.total_tokens}/{budget.max_usage}")
    if not parts:
        return ""
    return f"Budget: {', '.join(parts)} (on breach: {_oneline(budget.on_breach)})"


def _budget_breach_line(run: Run) -> str:
    """Why a run died, when opentine halted it for exceeding its budget.

    opentine records metadata['budget_state'] and sets status=failed. Without
    this the run looks like any other failure and the user hunts for a crash
    that never happened. metadata is untrusted and outside the integrity
    digest, so every field is treated as advisory.
    """
    state = run.metadata.get("budget_state") if isinstance(run.metadata, dict) else None
    if not isinstance(state, dict) or not state.get("breached"):
        return ""
    dimension = _oneline(state.get("dimension") or "budget")
    incurred, limit = state.get("incurred"), state.get("limit")
    if incurred is None or limit is None:
        return f"Budget BREACHED: {dimension}"
    return f"Budget BREACHED: {dimension} {_oneline(incurred)} > {_oneline(limit)}"


# ------------------------------------------------------------------- provenance


def _format_version_line(run: Run) -> str:
    migration = run.metadata.get("migration")
    if isinstance(migration, list) and migration:
        first = migration[0] if isinstance(migration[0], dict) else {}
        last = migration[-1] if isinstance(migration[-1], dict) else {}
        origin = _oneline(first.get("from", "?"))
        tool = _oneline(last.get("tool", "?"))
        return f"Format: v{run.format_version} (migrated from v{origin} by {tool})"
    return f"Format: v{run.format_version}"


def _fork_reason_label(basis: object, reason: object) -> str:
    """"Fork reason", or flagged unverified when the text is not attested.

    opentine deliberately leaves `metadata.fork_reason` out of
    `_SIGNED_METADATA_KEYS` (for 0.3.0 signature compatibility) and the whole
    metadata block sits outside the integrity digest, so the plaintext can be
    rewritten on a signed, integrity-clean artifact. `metadata.fork.intent` IS
    signed and IS committed to by the run id, and it is sha256 over the
    canonical intent object — so a reason that reproduces it is bound to the
    fork act, and one that does not must not be shown as if it were.
    """
    if not isinstance(basis, dict) or not isinstance(reason, str):
        return "Fork reason (unverified)"
    recorded = basis.get("intent")
    if not isinstance(recorded, str):
        return "Fork reason (unverified)"
    # Byte-identical to opentine's own canonical encoding for this shape,
    # so no private module is imported.
    canonical = json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "Fork reason" if digest == recorded else "Fork reason (unverified)"


def _fork_lineage_lines(run: Run) -> list[str]:
    """Where a fork came from, and which fork act it is.

    Since opentine 0.4.0 a fork id identifies the *act*, not the
    (source, point) coordinate, so two sibling forks share forked_from and
    fork_point while being different runs. The branch and whether the act
    carried a random nonce are what tell them apart. Pre-0.4.0 forks have no
    metadata.fork and simply render the origin line.
    """
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    basis = metadata.get("fork")
    basis = basis if isinstance(basis, dict) else None
    # forked_from is unprotected on unsigned artifacts; fall back to the fork
    # record so stripping it cannot silently downgrade a fork to a root run.
    origin = metadata.get("forked_from") or (basis.get("source") if basis else "")
    if not origin:
        return []
    point = metadata.get("fork_point") or (basis.get("point") if basis else "")
    lines = [
        f"Forked from: {_oneline(_truncate(origin, 120))}"
        + (f" at step {_oneline(_truncate(point, 80))}" if point else "")
    ]

    if isinstance(basis, dict):
        parts = []
        branch = basis.get("branch")
        if branch:
            parts.append(f"branch {_oneline(branch)}")
        nonce = basis.get("nonce")
        if isinstance(nonce, str):
            # An empty nonce is opentine's opt-in to a reproducible fork id;
            # anything else means this is one specific fork act among possible
            # siblings that share the same source and point.
            parts.append("reproducible" if nonce == "" else "unique act")
        if parts:
            lines.append(f"Fork: {', '.join(parts)}")
        # metadata sits outside the integrity digest, so a post-hoc edit to the
        # fork record still verifies "ok". This is the only check that catches it.
        if verify_fork_id is not None:
            try:
                verdict = verify_fork_id(run)
            except Exception:
                verdict = None
            if verdict is False:
                lines.append("Fork id: DOES NOT MATCH its recorded basis")
            elif verdict is True:
                lines.append("Fork id: verified against its recorded basis")
    reason = metadata.get("fork_reason")
    if reason:
        lines.append(f"{_fork_reason_label(basis, reason)}: {_oneline(_truncate(reason, 200))}")
    return lines


def _trust_lines(path: Path | None, *, config=None) -> list[str]:
    """Integrity digest, signature state, and what each of them actually covers."""
    if path is None:
        return ["Integrity: (not on disk yet)"]
    try:
        stat_result = path.stat()
    except OSError as e:
        return [f"Integrity: unreadable ({e})"]

    integrity = _verify_integrity_cached(path, stat_result)
    # A configured key is bound into a fresh closure per call; the verdict cache
    # is keyed by the configuration's fingerprint, so this costs nothing beyond
    # the first check of each file under that key.
    checker = trust_verifier(config) if getattr(config, "configured", False) else None
    signature = _signature_verdict(path, stat_result, checker, getattr(config, "fingerprint", ""))
    scheme = signature_scheme(path)
    lines = [integrity_line(integrity), signature_line(signature, scheme=scheme)]
    lines.extend(
        coverage_lines(signature, scheme=scheme, draft=bool(integrity.get("draft")))
    )
    problem = getattr(config, "problem", "")
    if problem:
        lines.append(f"Signing key: {_oneline(problem)}")
    return lines


# ---------------------------------------------------------------- run and step


def _run_detail_lines(
    run: Run, *, trust: list[str] | None = None, extra: list[str] | None = None
) -> list[str]:
    """The run inspector, as text. `trust` is injected so this stays pure."""
    kind_counts: dict[str, int] = {}
    for step in run.steps:
        kind_counts[step.kind.value] = kind_counts.get(step.kind.value, 0) + 1
    stats = _graph_stats(run)
    run_id = str(run.id)
    graph_line = (
        f"Graph: {stats['roots']} root(s), {stats['links']} link(s), "
        f"{stats['branches']} branch point(s), depth {stats['max_depth']}"
    )
    if stats["causal"]:
        graph_line += f", {stats['causal']} causal edge(s)"
    lines = [
        f"Run: {run_id}" if len(run_id) <= 32 else f"Run: {run_id[:12]}...",
        f"Model: {_oneline(run.model_info) or '(none)'}",
        f"Status: {run.status.value}",
        f"Created: {_format_timestamp(run.created_at)}",
        _format_version_line(run),
        f"Steps: {len(run.steps)}",
        f"Step kinds: {_format_counts(kind_counts)}",
        graph_line,
        f"Cost: {_cost_text(run)}",
        f"Tokens: {run.total_tokens}",
        f"Duration: {run.total_duration:.1f}s",
    ]
    providers = run_providers(run)
    if providers:
        lines.insert(2, f"Provider: {_format_counts(providers)}")
    pricing_line = _pricing_line(run)
    if pricing_line:
        lines.append(pricing_line)
    lines.extend(_cost_attribution_lines(run))
    budget_line = _budget_line(run)
    if budget_line:
        lines.append(budget_line)
    breach = _budget_breach_line(run)
    if breach:
        lines.append(breach)
    if run.tags:
        lines.append(f"Tags: {', '.join(_oneline(t) for t in sorted(run.tags))}")
    if run.refs:
        refs = ", ".join(f"{_oneline(name)} -> {_oneline(tip)}" for name, tip in run.refs.items())
        lines.append(f"Refs: {refs}")
    lines.extend(trust or [])
    lines.extend(extra or [])
    if len(run_id) > 32:
        lines.append(f"Full id: {run_id}")
    lines.extend(["", "Prompt:", *_indent_block(_truncate(run.user_prompt or "", 700))])
    if run.system_prompt:
        lines.extend(["", "System prompt:", *_indent_block(_truncate(run.system_prompt, 400))])
    lineage = _fork_lineage_lines(run)
    if lineage:
        lines.append("")
        lines.extend(lineage)
    return lines


def _step_detail_lines(step: Step) -> list[str]:
    """The step inspector, as text."""
    parents = ", ".join(_oneline(p) for p in step.parent_ids) if step.parent_ids else "(root)"
    lines = [
        f"ID: {_oneline(step.id)}",
        f"Kind: {step.kind.value}",
        f"Parents: {parents}",
        f"Model: {_oneline(step.model_info) or '(none)'}",
        f"Duration: {step.duration:.3f}s",
        f"Cost: ${step.cost:.6f}",
    ]
    provider = step_provider(step)
    if provider:
        lines.insert(4, f"Provider: {provider}")
    causal = step_causal_ids(step)
    if causal:
        # These are the edges a fork follows beyond the parent line, so they are
        # part of what this step *is* provenance-wise, not a footnote.
        lines.insert(3, f"Causally required: {', '.join(_oneline(c) for c in causal)}")
    if step.timestamp:
        lines.append(f"Time: {_format_timestamp(step.timestamp)}")
    if step.usage:
        lines.extend(["", "Usage:", *_mapping_lines(step.usage)])
    if step.billing:
        lines.extend(["", "Billing:", *_mapping_lines(step.billing)])
    if step.tool_info:
        lines.extend(["", "Tool:", *_mapping_lines(step.tool_info)])
    lines.extend(["", "Inputs:", *_mapping_lines(step.inputs)])
    lines.extend(["", "Outputs:", *_mapping_lines(step.outputs)])
    if step.error:
        lines.extend(["", "Error:", *_mapping_lines(step.error)])
    return lines


# ------------------------------------------------------------------- summaries


def _run_list_summary(runs: list[Run], visible_runs: list[Run], query: str) -> str:
    if not runs:
        return "No .tine runs loaded. Change directory or wait for agents to write runs."
    status_counts: dict[str, int] = {}
    for run in visible_runs:
        status_counts[run.status.value] = status_counts.get(run.status.value, 0) + 1
    total_cost = sum(run.total_cost for run in visible_runs)
    shown = f"{len(visible_runs)}/{len(runs)} shown" if query else f"{len(runs)} run(s)"
    counts = _format_counts(status_counts)
    partial = sum(1 for run in visible_runs if _pricing_incompleteness(run)[0])
    unpriced = sum(1 for run in visible_runs if _recorded_cost_state(run) == "unrecorded")
    cost = f"{'>=' if partial else ''}${total_cost:.4f}"
    notes = []
    if partial:
        notes.append(f"{partial} partially priced")
    if unpriced:
        notes.append(f"{unpriced} with no recorded cost")
    note = f" ({', '.join(notes)})" if notes else ""
    return f"{shown} - {counts} - visible cost {cost}{note}"


def _dag_summary(run: Run, query: str = "", matches: set[str] | None = None) -> str:
    stats = _graph_stats(run)
    summary = (
        f"{len(run.steps)} step(s), {stats['links']} link(s), "
        f"{stats['branches']} branch point(s), depth {stats['max_depth']}"
    )
    if stats["causal"]:
        summary += f", {stats['causal']} causal"
    if not query:
        return summary
    matched = matches or set(_matching_steps(run, query))
    return f"{summary} - {len(matched)}/{len(run.steps)} match query '{query}'"


def _highlight_summary(run: Run, matches: set[str]) -> str:
    if not matches:
        return "No matching steps"
    labels = [
        _truncate(_node_label(step).replace("* ", "", 1), 48)
        for step in run.steps
        if step.id in matches
    ]
    return "Matches: " + ", ".join(labels[:6])


#: Problems that still yield a usable run in the list, unlike a parse failure.
_WARNING_MARKERS = (": integrity ", ": duplicate run id ")


def _split_load_problems(problems: list[str]) -> tuple[list[str], list[str]]:
    """(fatal, warnings) — files that failed to load vs. runs that loaded anyway."""
    fatal, warnings = [], []
    for problem in problems:
        (warnings if any(m in problem for m in _WARNING_MARKERS) else fatal).append(problem)
    return fatal, warnings


def _load_problem_header(fatal: int, warnings: int) -> str:
    parts = []
    if fatal:
        parts.append(f"{fatal} load error(s)")
    if warnings:
        parts.append(f"{warnings} warning(s)")
    return " / ".join(parts) if parts else "Load errors"


# ------------------------------------------------------------------ transcript


def _tool_call_names(calls: object) -> list[str]:
    """Tool names from a transcript turn's `tool_calls`, in either recorded shape.

    An OpenAI-shaped call nests the name under "function"; opentine's own
    runtime writes it flat. Both are artifact-controlled, so neither is trusted
    to be a dict at all.
    """
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if not name and isinstance(call.get("function"), dict):
            name = call["function"].get("name")
        if isinstance(name, str) and name:
            names.append(_oneline(name))
    return names


def _transcript_turns(run: Run) -> list[dict[str, str]]:
    """Normalise Run.transcript into turns the console can render.

    opentine's runtime writes {"role", "content"} plus a "step_id" on the turns
    that produced a step, and "name" on tool results. Newer runtimes add tool
    call plumbing (`tool_call_id`, `tool_calls`) and separated reasoning.
    Everything here is artifact-controlled, so each field is coerced and the
    whole thing fails open.
    """
    try:
        raw = run.transcript or []
    except Exception:
        return []
    turns: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            content = _format_value(content, 4000) if content is not None else ""
        names = _tool_call_names(entry.get("tool_calls"))
        call_names = ", ".join(names[:6])
        if len(names) > 6:
            # Every other truncation in this file marks itself; a heading that
            # silently stops at six reads as a turn that asked for six.
            call_names += f", +{len(names) - 6} more"
        reasoning = entry.get("reasoning_content")
        turns.append(
            {
                "role": _oneline(entry.get("role") or "?"),
                "name": _oneline(entry.get("name") or ""),
                "step_id": _oneline(entry.get("step_id") or ""),
                "tool_call_id": _oneline(entry.get("tool_call_id") or ""),
                "tool_calls": call_names,
                "reasoning": _truncate(reasoning, 2000) if isinstance(reasoning, str) else "",
                "content": content,
            }
        )
    return turns


def _transcript_heading(turn: dict[str, str]) -> str:
    """The one-line header above a turn's content."""
    role = turn.get("role") or "?"
    label = f"{role}: {turn['name']}" if turn.get("name") else role
    parts = [label]
    if turn.get("tool_calls"):
        parts.append(f"calls {turn['tool_calls']}")
    if turn.get("tool_call_id"):
        parts.append(f"for {_truncate(turn['tool_call_id'], 12)}")
    text = "  ".join(parts)
    return f"{text}  [{_truncate(turn['step_id'], 12)}]" if turn.get("step_id") else text


def _transcript_summary(run: Run) -> str:
    turns = _transcript_turns(run)
    if not turns:
        return (
            "This run has no transcript. opentine records one when an agent runs; "
            "artifacts assembled from a graph do not carry it."
        )
    roles: dict[str, int] = {}
    for turn in turns:
        roles[turn["role"]] = roles.get(turn["role"], 0) + 1
    linked = sum(1 for t in turns if t["step_id"])
    return f"{len(turns)} turn(s) - {_format_counts(roles)} - {linked} linked to a step"


# ------------------------------------------------------------------------ diff


def _extra_step_deltas(left: Run, right: Run, *, limit: int = 12) -> list[str]:
    """Differences opentine's own `Run.diff` does not look at.

    `_graph_diff._fields` compares inputs, outputs, model_info, tool_info and
    error, plus cost/usage/billing drift. Neither `provider` nor `causal_ids` is
    in either list, and neither is hashed into a step id — so two steps that
    differ only in who served the call are the *same* step to that comparison
    and produce no delta at all. The console reports them itself, labelled as
    its own extension rather than as opentine's verdict.
    """
    try:
        left_steps = {step.id: step for step in left.steps}
        right_steps = {step.id: step for step in right.steps}
    except Exception:
        return []
    lines: list[str] = []
    for step_id in [sid for sid in left_steps if sid in right_steps]:
        a, b = left_steps[step_id], right_steps[step_id]
        before, after = step_provider(a), step_provider(b)
        if before != after:
            lines.append(
                f"  {step_id[:12]}  provider: {before or '(none)'} -> {after or '(none)'}"
            )
        causal_a, causal_b = step_causal_ids(a), step_causal_ids(b)
        if causal_a != causal_b:
            lines.append(
                f"  {step_id[:12]}  causal edges: {len(causal_a)} -> {len(causal_b)}"
            )
        if len(lines) >= limit:
            lines.append("  ...and more")
            break
    return lines


def _format_run_diff(left: Run, right: Run, *, max_steps: int = 25, max_fields: int = 8) -> str:
    """Human-readable semantic diff of two runs, using opentine's own Run.diff."""
    diff = left.diff(right)
    lines = [
        f"A: {left.id}",
        f"B: {right.id}",
        "",
        f"Common ancestor: {diff.common_ancestor or '(none - unrelated runs)'}",
        f"Cost: {_cost_text(left)} -> {_cost_text(right)}",
        f"Steps: {len(left.steps)} -> {len(right.steps)}",
        "",
    ]

    def step_list(label: str, steps) -> None:
        lines.append(f"{label} ({len(steps)}):")
        if not steps:
            lines.append("  (none)")
            return
        for step in steps[:max_steps]:
            lines.append(f"  {step.id[:12]}  {_node_label(step)}")
        if len(steps) > max_steps:
            lines.append(f"  ...and {len(steps) - max_steps} more")

    step_list("Only in A", diff.only_a)
    lines.append("")
    step_list("Only in B", diff.only_b)
    lines.append("")

    lines.append(f"Changed ({len(diff.changed)}):")
    if not diff.changed:
        lines.append("  (none)")
    for change in diff.changed[:max_steps]:
        a_id = getattr(change.step_a, "id", "?")
        b_id = getattr(change.step_b, "id", "?")
        lines.append(f"  {a_id[:12]} -> {b_id[:12]}")
        for delta in change.fields[:max_fields]:
            keys = f" [{', '.join(map(str, delta.changed_keys))}]" if delta.changed_keys else ""
            lines.append(f"    {_oneline(delta.name)}{keys}")
            lines.append(f"      - {_oneline(_format_compact(delta.before, 160))}")
            lines.append(f"      + {_oneline(_format_compact(delta.after, 160))}")
        if len(change.fields) > max_fields:
            lines.append(f"    ...and {len(change.fields) - max_fields} more field(s)")
    if len(diff.changed) > max_steps:
        lines.append(f"  ...and {len(diff.changed) - max_steps} more changed step(s)")

    extra = _extra_step_deltas(left, right)
    left_causal, right_causal = len(causal_edges(left)), len(causal_edges(right))
    if left_causal != right_causal:
        extra.append(f"  causal edges in the graph: {left_causal} -> {right_causal}")
    if extra:
        # Under its own heading, always. Indented two spaces directly beneath
        # "Changed (N):" this reads as one of opentine's own changed steps,
        # which is the opposite of what the section is for.
        lines.extend(["", "Also differs (fields opentine's diff does not compare):", *extra])
    return "\n".join(lines)
