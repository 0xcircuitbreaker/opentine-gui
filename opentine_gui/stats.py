"""What a directory of runs cost, answered from the runs already in memory.

The console shows one run at a time. `tine stats` answers the other question —
what a whole directory cost, which models it used, how the spend splits by
status or by day — and this module computes the same rollup over the `Run`
objects the run list already holds.

It deliberately does not go through `opentine.index.RunIndex.search`, which is
how the CLI answers this: search builds and writes a rebuildable index file into
the user's runs directory, and this console is a reader of that directory.
Nothing here writes anything, and nothing here needs an index.

Two rules shape everything below.

*Absent is not zero.* opentine's own `tine stats` omits the token and duration
keys entirely unless `--deep` collected them, because a `0` there reads as "this
run was free" and gets summed with real figures by whatever consumes it. Every
figure here that may never have been recorded is `None`, and renders as "-".

*A run that could not be read is not a run that cost nothing.* Unreadable runs
are counted in `Rollup.unreadable` and folded into no total. A run is unreadable
only when a figure that must be summed cannot be read: its cost or its step
count. Everything else degrades to a label — an unreadable timestamp is a
missing timestamp, not a missing run — because dropping a run from the spend
over an unparseable tag would understate the one number this module exists to
get right.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opentine_gui.graphmodel import step_cost, step_provider
from opentine_gui.text import _format_counts, _format_timestamp, _oneline, _truncate

#: The groupings the console offers, in the order a chooser should list them.
#: The first five are `tine stats --group-by`; "provider" is this console's own,
#: read through `graphmodel.step_provider`, which recovers the provider from a
#: 0.7.x artifact's billing record as well as from the post-0.7.2 field.
GROUPINGS: tuple[str, ...] = ("model", "status", "tag", "day", "format-version", "provider")

#: Bucket labels for a run that carries nothing to group on. Named the way
#: opentine names them, so a console table and `tine stats` read alike.
NONE_KEY = "(none)"
UNTAGGED = "(untagged)"
UNDATED = "(undated)"
UNRECORDED = "(unrecorded)"

#: Artifact-supplied labels are cut to this before they are grouped on, so one
#: run with a megabyte-long tag cannot widen every row of the table.
MAX_LABEL = 64

#: How many of each an artifact-controlled list a single run contributes. Tags,
#: models and providers are lists an artifact controls, and a 10 MiB `.tine` can
#: hold a great many of them; a table with one row per hostile tag is a denial of
#: service against the render thread. Applied where each list is read, not where
#: the buckets are made, so the tag histogram and the exported model list are
#: bounded too. Keys are taken in sorted order, so which ones survive is stable.
MAX_KEYS_PER_RUN = 64

#: A run costing more than this is not reporting money, so its cost is treated
#: as unreadable rather than summed. Also keeps every total finite: the ceiling
#: times any plausible number of runs stays far inside float range.
MAX_COST = 1e12

#: A step claiming to have taken longer than this is not reporting elapsed time,
#: so it is read as untimed rather than summed. Like `MAX_COST` this is also what
#: keeps a duration total finite: opentine validates a step's duration as finite
#: and non-negative and nothing further, so two runs each reporting 1e308 seconds
#: overflowed `math.fsum` and took the whole rollup down with them.
MAX_DURATION = 1e12  # ~31,700 years

#: The largest token count opentine's own `_usage_value` will accept in a step's
#: usage. A larger figure did not come through that validation, and rendering it
#: means `int(float(...))` printing three hundred digits of precision the file
#: never contained, so it is read as unrecorded instead.
MAX_TOKENS = (1 << 53) - 1

#: The usage dimensions `Run.total_tokens` adds up, mirrored here because that
#: property cannot say whether a zero was measured or never collected.
_TOKEN_DIMENSIONS = (
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
)

#: Leading characters that make a spreadsheet treat a cell as a formula. A run
#: id, a tag or a model name is artifact-controlled text that lands in a cell,
#: so it is quoted out of being executable before it ever reaches a CSV writer.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Bucket:
    """One aggregate row: the totals of the runs sharing a group key."""

    label: str
    runs: int
    steps: int
    #: None when nothing in this bucket was ever priced. Absent is not zero: a
    #: directory of imported runs costs "-", not "$0.0000", exactly as the run
    #: list already reports each of them.
    cost: float | None
    #: True when any run here was flagged incompletely priced, or when some of
    #: its runs were priced and others recorded nothing, which makes `cost` a
    #: lower bound rather than the spend.
    cost_partial: bool
    #: None means no run in this bucket recorded the figure. Never 0: a zero
    #: would be summed with real counts by whatever reads it next.
    tokens: int | None
    duration: float | None
    models: tuple[str, ...]


@dataclass(frozen=True)
class Rollup:
    """Every figure one pass over a set of runs produced."""

    total: Bucket
    buckets: tuple[Bucket, ...]
    #: The grouping actually used, which may not be the one asked for: an
    #: unknown name falls back to "status" rather than raising in a render path.
    group_by: str
    statuses: dict[str, int]
    formats: dict[str, int]
    tags: dict[str, int]
    #: Oldest and newest `created_at` among runs that had one. 0.0 when none
    #: did, which is the same "no timestamp" sentinel the run list already uses.
    oldest: float
    newest: float
    #: Runs that could not be read. Reported, never folded into a total.
    unreadable: int


@dataclass(frozen=True)
class _Facts:
    """What one readable run contributes, extracted once per rollup."""

    keys: tuple[str, ...]
    steps: int
    cost: float
    #: What each of this run's keys actually spent. For a single-valued grouping
    #: that is the whole cost under one key; for model and provider it is the
    #: per-step split, because adding the run's total to every key it names
    #: makes the buckets sum to more than the run.
    costs: dict[str, float]
    #: True when nothing priced this run at all, so its zero is an absence
    #: rather than a measurement.
    unrecorded: bool
    partial: bool
    tokens: int | None
    duration: float | None
    models: tuple[str, ...]
    status: str
    format_version: str
    tags: tuple[str, ...]
    created_at: float


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """`getattr` that also survives the attribute being a property that raises.

    Every field below is read off an object decoded from an untrusted file, and
    `Run` exposes several of its fields as computed properties. A single hostile
    step must not take the whole rollup down with it.
    """
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _number(value: object) -> float | None:
    """A finite, non-negative float, or None for anything that is not one.

    Rejects bool deliberately (True is not a cost) and rejects NaN, which would
    otherwise poison every sum it touched and compare false against itself in
    the bucket sort.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None  # an int too large for a float
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _count(value: object) -> int | None:
    """A non-negative integer token count, or None when nothing usable is there."""
    number = _number(value)
    if number is None or number > MAX_TOKENS:
        return None
    try:
        return int(number)
    except (OverflowError, ValueError):
        return None


def _text(value: object) -> str:
    """Artifact text, flattened to one line and cut to a label's width."""
    try:
        return _truncate(_oneline(value), MAX_LABEL)
    except Exception:
        return ""


def _pricing_incomplete(run: Any) -> bool:
    """Whether opentine flagged this run's pricing as a lower bound.

    The same rule `inspectors._pricing_incompleteness` applies, spelled again
    rather than imported: that module reaches the whole reader stack (sources,
    trust, the graph model) for the three figures it returns, and this one needs
    only the flag. `manifest.pricing.complete` is opentine's own recorded field,
    not a heuristic, so the two readings cannot drift apart on their own.
    """
    try:
        pricing = run.manifest.get("pricing")
        # `get("complete", True) is True`, not `is False`: opentine treats
        # anything that is not literally True — False, 0, "false", null — as an
        # unproven claim and breaches a strict_cost budget on exactly those.
        return isinstance(pricing, dict) and pricing.get("complete", True) is not True
    except Exception:
        return False


def _tokens(steps: list[Any]) -> int | None:
    """Total tokens across *steps*, or None when no step recorded any usage.

    Mirrors `Run.total_tokens` — the larger of the declared total and the sum of
    the dimensions, so a file that states both does not double count — but that
    property returns 0 both for a run that used no tokens and for one that never
    recorded them, which is exactly the distinction this rollup must keep.
    """
    total = 0
    recorded = False
    for step in steps:
        usage = _attr(step, "usage")
        if not isinstance(usage, dict):
            continue
        try:
            declared = _count(usage.get("total"))
            parts = [_count(usage.get(name)) for name in _TOKEN_DIMENSIONS]
        except Exception:
            continue
        measured = [part for part in parts if part is not None]
        if declared is None and not measured:
            continue
        recorded = True
        total += max(declared or 0, sum(measured))
    return total if recorded else None


def _duration(steps: list[Any]) -> float | None:
    """Wall-clock seconds across *steps*, or None when none was measured.

    A step whose duration is 0.0 counts as unmeasured: that is the field's
    default, and opentine leaves it there when it did not time the step. A run
    that genuinely took no measurable time is indistinguishable from one that
    was never timed, and reporting "-" for both is the honest reading. A step
    claiming more than `MAX_DURATION` is unmeasured for the same reason from the
    other end: it is not a stopwatch reading, and summing it overflows.
    """
    total = 0.0
    recorded = False
    for step in steps:
        seconds = _number(_attr(step, "duration"))
        if not seconds or seconds > MAX_DURATION:
            continue
        recorded = True
        total += seconds
    if not recorded or not math.isfinite(total):
        return None
    return total


def _models(run: Any, steps: list[Any]) -> tuple[str, ...]:
    """Every model this run names: the one it declares plus the ones its steps used.

    `tine stats` groups on the single model its index row carries. The console
    holds the whole run, so it can name a model a step switched to mid-run, and
    a run that used two models is counted under both.
    """
    declared = _text(_attr(run, "model_info", ""))
    used = {_text(_attr(step, "model_info", "")) for step in steps}
    used.discard(declared)
    # The declared model is the one `tine stats` groups on, so it is the one the
    # cap keeps: a file naming a thousand models across its steps must not be
    # able to push the run's own model out of the row that reports the run.
    names = sorted(name for name in used if name)[: MAX_KEYS_PER_RUN - (1 if declared else 0)]
    if declared:
        names.append(declared)
    return tuple(sorted(names))


def _providers(steps: list[Any]) -> tuple[str, ...]:
    """Distinct providers the run's steps recorded.

    Through `graphmodel.step_provider`, which is what the run inspector reads:
    `Step.provider` arrived after 0.7.2, the release this console pins as its
    floor, but a 0.7.x adapter still wrote the provider into the step's billing
    record, so the field's absence is not the absence of the fact. Reading only
    the field would have grouped every artifact from the pinned release under
    "(unrecorded)" while the inspector named the provider for the same file.
    """
    names: set[str] = set()
    for step in steps:
        try:
            names.add(_text(step_provider(step)))
        except Exception:
            continue
    return tuple(sorted(name for name in names if name))[:MAX_KEYS_PER_RUN]


def _tags(run: Any) -> tuple[str, ...]:
    # Every step of the normalisation is inside the guard, the truth test
    # included: `tags` is whatever a decoded artifact put there, and an object
    # whose __bool__ raises would otherwise take the rollup down from here.
    raw = _attr(run, "tags", ())
    try:
        if isinstance(raw, str) or not isinstance(raw, Iterable):
            raw = [raw] if raw else []
        labels = {_text(tag) for tag in raw}
    except Exception:
        return ()
    return tuple(sorted(label for label in labels if label))[:MAX_KEYS_PER_RUN]


def _day(created_at: float) -> str:
    """The local calendar day a run was created on.

    Local, not UTC as the CLI uses: this groups what a person did on a day they
    remember, next to timestamps the rest of the console already shows local.
    """
    if not created_at:
        return UNDATED
    try:
        return time.strftime("%Y-%m-%d", time.localtime(created_at))
    except (OSError, OverflowError, ValueError):
        return UNDATED


def _grouping(group_by: object) -> str:
    """Normalise a requested grouping, falling back to "status" for anything else.

    The chooser is the console's own, so a bad value is a bug rather than an
    attack, but raising out of a render path would take the panel down. The
    fallback is recorded in `Rollup.group_by` so the table header cannot claim a
    grouping that was not used.
    """
    try:
        name = str(group_by).strip().lower().replace("_", "-")
    except Exception:
        return "status"
    return name if name in GROUPINGS else "status"


def _keys(
    group_by: str,
    *,
    unattributed: bool = False,
    status: str,
    format_version: str,
    created_at: float,
    tags: tuple[str, ...],
    models: tuple[str, ...],
    providers: tuple[str, ...],
) -> tuple[str, ...]:
    """The buckets one run belongs to; a multi-valued grouping puts it in several.

    A run tagged twice is one run and two rows, which is how `tine stats` counts
    tags and how this counts models and providers too. The multi-valued lists
    arrive already capped at `MAX_KEYS_PER_RUN`, so this does not cap them again:
    one bound applied in one place cannot drift out of step with the histograms
    and the export, which read the same lists.
    """
    if group_by == "status":
        return (status or NONE_KEY,)
    if group_by == "format-version":
        return (format_version or NONE_KEY,)
    if group_by == "day":
        return (_day(created_at),)
    if group_by == "tag":
        return tags or (UNTAGGED,)
    if group_by == "provider":
        # `(unrecorded)` is appended, not substituted: a run where only *some*
        # steps name a provider still spent money on the ones that do not, and
        # without a bucket to hold it that spend left the breakdown while
        # staying in the header.
        if not providers:
            return (UNRECORDED,)
        return (*providers, UNRECORDED) if unattributed else providers
    return models or (NONE_KEY,)


def _facts(run: Any, group_by: str) -> _Facts | None:
    """Everything the rollup needs from one run, or None if the run is unreadable.

    Cost and step count are the two figures a run must yield to be counted: they
    are summed, so a run that cannot state them cannot be added to a total
    without inventing one.
    """
    try:
        steps = list(run.steps)
        cost = _number(run.total_cost)
    except Exception:
        return None
    if cost is None or cost > MAX_COST:
        return None

    # RunStatus is a StrEnum, so a well-formed status stringifies to its value
    # and a foreign one stringifies to whatever the file put there.
    status = _text(_attr(run, "status", ""))
    version = _text(_attr(run, "format_version", ""))
    created = _number(_attr(run, "created_at")) or 0.0
    tags = _tags(run)
    models = _models(run, steps)
    keys = _keys(
        group_by,
        # Whether any *billable* step went without a provider, which is what
        # decides whether the residue bucket has to exist.
        unattributed=any(
            not _text(step_provider(step)) and _text(_attr(_attr(step, "kind", ""), "value", ""))
            == "model"
            for step in steps
        ),
        status=status,
        format_version=version,
        created_at=created,
        tags=tags,
        models=models,
        providers=_providers(steps),
    )
    return _Facts(
        keys=keys,
        steps=len(steps),
        cost=cost,
        costs=_split_cost(run, steps, group_by, keys, cost),
        unrecorded=_nothing_priced(run, steps, cost),
        partial=_pricing_incomplete(run),
        tokens=_tokens(steps),
        duration=_duration(steps),
        models=models,
        status=status or NONE_KEY,
        format_version=version or NONE_KEY,
        tags=tags,
        created_at=created,
    )


def _split_cost(
    run: Any, steps: list[Any], group_by: str, keys: tuple[str, ...], cost: float
) -> dict[str, float]:
    """This run's cost, divided among the keys that actually spent it."""
    if group_by not in ("model", "provider") or len(keys) < 2:
        return {key: cost for key in keys}
    per_key: dict[str, float] = {}
    if group_by == "model":
        try:  # opentine's own per-model split, the one the inspector renders
            by_model = run.cost_breakdown().by_model
        except Exception:
            by_model = {}
        for name, amount in (by_model or {}).items():
            value = _number(amount)
            if value is None:
                continue
            key = _text(name) or NONE_KEY
            per_key[key] = per_key.get(key, 0.0) + value
    else:
        for step in steps:
            provider = _text(step_provider(step)) or UNRECORDED
            # `step_cost`, not the bare field: `Run.total_cost` prefers
            # billing["known_subtotal_usd"], so reading `cost` made every
            # provider bucket $0.0000 under a non-zero headline.
            value = _number(step_cost(step)) or 0.0
            per_key[provider] = per_key.get(provider, 0.0) + value
    # Any key the split did not answer for contributes nothing rather than the
    # whole run: a bucket that cannot be attributed is not a bucket that spent.
    return {key: per_key.get(key, 0.0) for key in keys}


def _nothing_priced(run: Any, steps: list[Any], cost: float) -> bool:
    """Whether this run's zero means "not priced" rather than "cost nothing".

    The same reading `inspectors._recorded_cost_state` applies, kept here rather
    than imported: that module pulls in the whole reader stack, and this needs
    one boolean.
    """
    if cost:
        return False
    try:
        billable = [s for s in steps if _text(_attr(_attr(s, "kind", ""), "value", "")) == "model"]
        if not billable:
            return True
        for step in billable:
            billing = _attr(step, "billing", None)
            status = billing.get("status") if isinstance(billing, dict) else None
            if isinstance(status, str) and status in ("complete", "partial", "unmetered"):
                return False
        return True
    except Exception:
        return False


def _bucket(label: str, group: list[_Facts], *, total: bool = False) -> Bucket:
    """One aggregate row. `total` sums each run once rather than per key.

    Without the flag the total row looked its own label up in each run's
    per-key split, so a run naming a model or provider literally "all"
    contributed that key's share to the headline instead of its whole cost.
    """
    counted = [facts.tokens for facts in group if facts.tokens is not None]
    timed = [facts.duration for facts in group if facts.duration is not None]
    priced = [facts for facts in group if not facts.unrecorded]
    return Bucket(
        label=label,
        runs=len(group),
        steps=sum(facts.steps for facts in group),
        # fsum, not sum: costs are four-decimal quantities opentine adds up in
        # Decimal, and a directory of thousands of them drifts visibly in the
        # last place if they are accumulated pairwise. None when nothing in the
        # bucket was ever priced: absent is not zero, which is the rule this
        # module exists to keep.
        cost=(
            math.fsum(
                facts.cost if total else facts.costs.get(label, facts.cost) for facts in group
            )
            if priced
            else None
        ),
        cost_partial=any(facts.partial for facts in group)
        or bool(priced and len(priced) != len(group)),
        tokens=sum(counted) if counted else None,
        duration=math.fsum(timed) if timed else None,
        models=tuple(sorted({model for facts in group for model in facts.models})),
    )


def _histogram(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def rollup(runs: Iterable[Any], *, group_by: str = "status") -> Rollup:
    """Aggregate *runs* into one total and one bucket per group key.

    *runs* holds `Run` objects, or the `RunEntry` rows the run list is built
    from; an entry is unwrapped so a caller can pass either without rebuilding
    the list. Nothing is read from disk and nothing is written.
    """
    grouping = _grouping(group_by)
    everything: list[_Facts] = []
    members: dict[str, list[_Facts]] = {}
    unreadable = 0
    for item in runs:
        run = _attr(item, "run", None) or item
        facts = _facts(run, grouping)
        if facts is None:
            unreadable += 1
            continue
        everything.append(facts)
        for key in facts.keys:
            members.setdefault(key, []).append(facts)

    stamps = [facts.created_at for facts in everything if facts.created_at]
    buckets = [_bucket(label, group) for label, group in members.items()]
    # Cost first because the question is what this cost, then run count, then
    # label so that two buckets that tie still order the same way every refresh.
    # An unpriced bucket sorts as zero spend, but keeps its own cost of None:
    # ordering by "how much did this cost" cannot be answered for it, and
    # putting it last is the honest place for an unanswerable row.
    buckets.sort(key=lambda bucket: (-(bucket.cost or 0.0), -bucket.runs, bucket.label))
    return Rollup(
        total=_bucket("all", everything, total=True),
        buckets=tuple(buckets),
        group_by=grouping,
        statuses=_histogram(facts.status for facts in everything),
        formats=_histogram(facts.format_version for facts in everything),
        tags=_histogram(tag for facts in everything for tag in facts.tags),
        oldest=min(stamps) if stamps else 0.0,
        newest=max(stamps) if stamps else 0.0,
        unreadable=unreadable,
    )


def _cost_text(value: float | None, partial: bool) -> str:
    """Cost with the same ">=" lower-bound marker the run inspector uses.

    None renders "-", the same way every other never-recorded figure in this
    module does: a run nothing priced has no cost to report, and "$0.0000" is a
    measurement it never made.
    """
    if value is None:
        return "-"
    return f"{'>=' if partial else ''}${value:.4f}"


def _int_text(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _seconds_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def _versions_text(counts: dict[str, int]) -> str:
    """Format-version histogram, rendered the way `tine stats` renders it."""
    if not counts:
        return "(none)"
    return ", ".join(f"v{version} {count}" for version, count in counts.items())


def _join(values: tuple[str, ...], limit: int = 8) -> str:
    if not values:
        return "-"
    shown = ", ".join(values[:limit])
    extra = len(values) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _table(result: Rollup, limit: int) -> list[str]:
    shown = list(result.buckets[:limit]) if limit and limit > 0 else list(result.buckets)
    rows = [[result.group_by, "runs", "steps", "cost", "tokens", "duration"]]
    rows += [
        [
            bucket.label,
            f"{bucket.runs:,}",
            f"{bucket.steps:,}",
            _cost_text(bucket.cost, bucket.cost_partial),
            _int_text(bucket.tokens),
            _seconds_text(bucket.duration),
        ]
        for bucket in shown
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines: list[str] = []
    for index, row in enumerate(rows):
        cells = [row[0].ljust(widths[0])]
        cells += [cell.rjust(widths[column + 1]) for column, cell in enumerate(row[1:])]
        lines.append("  " + "  ".join(cells).rstrip())
        if index == 0:
            lines.append("  " + "  ".join("-" * width for width in widths))
    hidden = len(result.buckets) - len(shown)
    if hidden > 0:
        lines.append(f"  ({hidden:,} more group(s) not shown)")
    return lines


def rollup_lines(result: Rollup, *, limit: int = 12) -> list[str]:
    """Render *result* as a flat text block, at most *limit* group rows.

    A `limit` of zero or less shows every group, the way `tine stats --limit 0`
    does. Figures that were never recorded render as "-" and never as 0.
    """
    total = result.total
    caveat = (
        f"Unreadable: {result.unreadable:,} run(s), excluded from every number above"
        if result.unreadable
        else ""
    )
    if not total.runs:
        return ["No readable runs to summarise."] + ([caveat] if caveat else [])

    mean = None if total.cost is None else total.cost / total.runs
    headline = (
        f"{total.runs:,} run(s), {total.steps:,} step(s), "
        f"cost {_cost_text(total.cost, total.cost_partial)} "
        f"(mean {_cost_text(mean, False)}/run)"
    )
    lines = [
        headline,
        f"Tokens: {_int_text(total.tokens)}   Duration: {_seconds_text(total.duration)}",
        f"Window: {_format_timestamp(result.oldest)} .. {_format_timestamp(result.newest)}",
        f"Models: {_join(total.models)}",
        f"Statuses: {_format_counts(result.statuses)}",
        f"Format versions: {_versions_text(result.formats)}",
        f"Tags: {_format_counts(result.tags)}",
    ]
    if total.cost_partial:
        lines.append("Cost is a lower bound: at least one run was only partially priced.")
    if caveat:
        lines.append(caveat)

    lines += ["", f"By {result.group_by}"]
    # A run carrying three tags is one run and three rows. Saying so where the
    # rows disagree with the headline is cheaper than a reader deciding the
    # headline is wrong.
    if sum(bucket.runs for bucket in result.buckets) > total.runs:
        lines.append(f"  (a run appears under every {result.group_by} it has)")
    return lines + _table(result, limit)


def _cell(value: str) -> str:
    """Artifact text, made inert for a spreadsheet.

    A tag of `=HYPERLINK("http://…"&A1)` is a live formula the moment an export
    is double-clicked. The leading quote is the standard neutralisation: the
    cell still reads as its text, and nothing evaluates.
    """
    return f"'{value}" if value.startswith(_FORMULA_LEAD) else value


def csv_rows(result: Rollup) -> list[list[str]]:
    """*result* as a header row plus one row per bucket, ready for `csv.writer`.

    Costs and durations are bare numbers, not `$1.2345`, so a spreadsheet reads
    the column as a column of numbers; `cost_partial` carries the lower-bound
    caveat the "$>=" prefix carries on screen. A figure that was never recorded
    is an empty cell, which every consumer reads as no value — a 0 would be
    read as a measurement, and a "-" as text in a numeric column.
    """
    header = [
        "group",
        "label",
        "runs",
        "steps",
        "cost",
        "cost_partial",
        "tokens",
        "duration",
        "models",
    ]

    def row(group: str, bucket: Bucket) -> list[str]:
        return [
            group,
            _cell(bucket.label),
            str(bucket.runs),
            str(bucket.steps),
            "" if bucket.cost is None else f"{bucket.cost:.4f}",
            "true" if bucket.cost_partial else "false",
            "" if bucket.tokens is None else str(bucket.tokens),
            "" if bucket.duration is None else f"{bucket.duration:.3f}",
            _cell(", ".join(bucket.models)),
        ]

    rows = [header, row("total", result.total)]
    rows += [row(result.group_by, bucket) for bucket in result.buckets]
    if result.unreadable:
        # The corrupt-artifact count travels with the numbers rather than being
        # left behind on screen: an export that silently drops it reads as a
        # complete accounting of the directory, and is not one.
        rows.append(
            ["unreadable", "(excluded from every row above)", str(result.unreadable)]
            + [""] * (len(header) - 3)
        )
    return rows
