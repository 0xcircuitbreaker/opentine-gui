"""Re-pricing a loaded run, so an imported one is not reported as free.

`Run.total_cost` adds up the cost each step recorded *at capture*. A run that
opentine executed carries one. A run imported from OpenTelemetry, or from a
framework's own log, carries `cost = 0.0` on every step, because nothing on that
path ever priced it. Summing those produces a confident `$0.0000` that is
pixel-identical to the one shown for a run that genuinely cost nothing, and the
console has no way to tell the reader which it is looking at.

opentine's own position is that an uncosted step is *unknown*, never free. This
module applies that position here: it re-derives a run's price from the run's own
record against opentine's signed pricing catalog, and reports a step the catalog
cannot answer for as `unknown` carrying no amount at all, rather than as a zero
that reads as "free". A quote is a report about an artifact and never an edit to
it, so nothing here writes anything, to the run or to disk.

Two versions of opentine can answer the question, and the console supports both.
0.8.0 has the pass itself (`opentine._pricing_pass`), which is preferred because
there should be one pricing arithmetic in a codebase and not two. 0.7.2, the
floor this console supports, ships only the billing primitives, so the rollup
around them is reimplemented below and bottoms out in the same `bill()`.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from opentine.billing import PricingCatalog, Usage, bill, load_catalogs
from opentine.billing.types import as_date

from opentine_gui.graphmodel import step_provider
from opentine_gui.text import _oneline, _truncate

try:
    # opentine 0.8.0's own post-hoc pass. Private and unreleased, so it is used
    # where it exists and reimplemented below where it does not. Its answer is
    # preferred even though the fallback agrees with it: the released `bill()`
    # cannot select a rate card's time-of-day window, and the pass can.
    from opentine._pricing_pass import price_run as _opentine_price_run
except Exception:  # pragma: no cover - depends on the installed opentine
    _opentine_price_run = None

#: Only a model call meets a rate card. A tool or think step is not "free", it
#: is simply not a billable call, so it is counted apart as skipped and never
#: folded into a total as a zero.
_PRICEABLE_KINDS = frozenset({"model"})

#: Statuses that mean the catalog actually answered for a step. "partial" is an
#: answer with a hole in it: its known subtotal is genuinely attributable, and
#: the caveat rides along in the status rather than in a silently smaller number.
_PRICED_STATUSES = frozenset({"complete", "partial", "unmetered"})

_UNKNOWN = "unknown"

#: Artifact-supplied names (a model, a provider, a step id) are rendered, so they
#: are bounded here rather than wherever they are eventually drawn.
_NAME_LIMIT = 80
_DETAIL_LIMIT = 200

#: Width of the name column in the rendered breakdown.
_COLUMN = 34


@dataclass(frozen=True)
class StepQuote:
    """What the catalog says one recorded model step cost."""

    step_id: str
    provider: str
    model: str
    status: str
    #: `None` whenever the catalog did not answer. Never 0.0 as a stand-in: a
    #: zero beside a model name reads as "this call was free".
    amount_usd: float | None
    rate_card_id: str | None
    detail: str
    #: True when the provider was supplied by the reader rather than recorded.
    #: A price computed from an assumption has to say so wherever it is shown.
    assumed: bool = False


@dataclass(frozen=True)
class RunQuote:
    """A run's post-hoc price, naming the catalog that produced every figure."""

    available: bool
    total_usd: float
    priced: int
    unknown: int
    skipped: int
    by_model: dict[str, float]
    by_provider: dict[str, float]
    unknown_models: tuple[str, ...]
    catalog_id: str
    catalog_hash: str
    effective_at: str
    steps: tuple[StepQuote, ...]
    detail: str
    #: The provider the reader supplied for steps that recorded none, if any.
    assumed_provider: str = ""


# Loading the catalog parses and verifies a signed 75-card document off disk,
# which costs enough (tens of milliseconds) that doing it per run, per redraw,
# would be felt. The lock is not contention control but a guard against two
# threads paying that cost at once when a background scan and the UI both ask.
_CATALOG_LOCK = threading.Lock()
_CATALOG: PricingCatalog | None = None
_CATALOG_DETAIL = ""
_CATALOG_LOADED = False


def load_catalog(*, refresh: bool = False) -> PricingCatalog | None:
    """The pricing catalog opentine would use, or None if none could be loaded.

    Cached for the process. `refresh=True` re-reads it, which is what the console
    calls after a user installs an overlay: `load_catalogs` layers the per-user
    and per-workspace files over the bundled one, so the answer can change
    without this process restarting.
    """
    global _CATALOG, _CATALOG_DETAIL, _CATALOG_LOADED
    with _CATALOG_LOCK:
        if refresh:
            _CATALOG_LOADED = False
        if not _CATALOG_LOADED:
            try:
                _CATALOG, _CATALOG_DETAIL = load_catalogs(), ""
            except Exception as e:
                # An overlay is a file a user (or something that wrote to their
                # config directory) supplied, so a malformed or unsigned one
                # must cost the price panel and nothing else.
                _CATALOG = None
                _CATALOG_DETAIL = _reason(e)
            _CATALOG_LOADED = True
        return _CATALOG


def catalog_available() -> bool:
    """Whether a post-hoc price can be quoted at all."""
    return load_catalog() is not None


def quote_run(
    run: Any, *, effective_at: str | date | None = None, assume_provider: str = ""
) -> RunQuote:
    """Re-price *run* from its own record. Read-only: the run is not touched.

    Leaving *effective_at* unset cards each step on the day it was recorded, so a
    run replayed months later is priced at what it actually cost. Passing a date
    pins the whole run to that day's cards, which is the "what would this cost
    today" question. A step that recorded no timestamp has no day of its own and
    falls back to the as-of date, so `effective_at` is reported as "recorded"
    only to say which rule was applied, not to claim every step had a date.
    """
    steps = _run_steps(run)
    when, pinned, note = _resolve_as_of(effective_at)
    catalog = load_catalog()
    if catalog is None:
        billable = sum(1 for step in steps if _read_record(step)[0] in _PRICEABLE_KINDS)
        return RunQuote(
            available=False,
            total_usd=0.0,
            priced=0,
            unknown=billable,
            skipped=len(steps) - billable,
            by_model={},
            by_provider={},
            unknown_models=(),
            catalog_id="",
            catalog_hash="",
            effective_at=_as_of_label(when, pinned),
            steps=(),
            detail=_join(note, _CATALOG_DETAIL or "no pricing catalog could be loaded"),
        )
    assumed = _truncate(_oneline(assume_provider), _NAME_LIMIT)
    # opentine's own pass reads `Step.provider` and nothing else, so it cannot
    # honour an assumption and cannot recover the provider from the billing
    # record of an artifact written before that field existed — which is most of
    # them, and exactly the runs a reader wants priced. The local rollup answers
    # in both of those cases; the upstream pass answers when neither applies,
    # because there should be one pricing arithmetic and not two.
    upstream = not assumed and not _needs_recovery(steps)
    quotes = _upstream_quotes(run, when=when, pinned=pinned, catalog=catalog) if upstream else None
    if quotes is None:
        quotes = [
            quote
            for quote in (
                _quote_step(
                    step, catalog=catalog, when=when, pinned=pinned, assume_provider=assumed
                )
                for step in steps
            )
            if quote is not None
        ]
    return _roll_up(
        quotes,
        # Whatever priced the run answered for the billable steps and passed over
        # the rest. A step nothing answered for is not a zero, it is not a
        # billable call, and it is counted here rather than in the total.
        skipped=max(0, len(steps) - len(quotes)),
        catalog=catalog,
        effective_at=_as_of_label(when, pinned),
        note=note,
        assumed_provider=assumed if any(q.assumed for q in quotes) else "",
    )


def quote_lines(quote: RunQuote, *, limit: int = 6) -> list[str]:
    """Render a quote as a flat text block, biggest subtotals first.

    *limit* caps each breakdown; the rows left out are counted rather than
    dropped silently, because a truncated breakdown that does not say it was
    truncated reads as the whole run.
    """
    try:
        # `int()` raises OverflowError, not ValueError, on an infinite float, so
        # the arithmetic errors are caught here too rather than reaching a caller.
        rows = max(0, int(limit))
    except (ArithmeticError, TypeError, ValueError):
        rows = 6
    if not quote.available:
        return ["Post-hoc price: unavailable", f"  {quote.detail or 'no pricing catalog'}"]
    # Sliced rather than elided: the hash is already sanitized and bounded, and
    # a prefix a reader can match against `tine price` output is the point of it.
    catalog = _oneline(quote.catalog_hash)[:12] or "unknown"
    lines = [f"Post-hoc price (catalog {catalog}, as of {_oneline(quote.effective_at)})"]
    # A total assembled from nothing is not zero: `$0.0000 from 0 priced steps`
    # is the same "free" claim the whole module exists to avoid making.
    total = _format_usd(quote.total_usd if quote.priced else None)
    lines.append(f"  {'total'.ljust(14)}{total}  from {_plural(quote.priced, 'priced step')}")
    if quote.unknown:
        named = ", ".join(quote.unknown_models[:rows]) if rows else ""
        rest = len(quote.unknown_models) - rows
        if rest > 0:
            named = f"{named}, +{rest} more" if named else f"{rest} models"
        suffix = f": {_truncate(named, 160)}" if named else ""
        lines.append(f"  {'unknown'.ljust(14)}{_plural(quote.unknown, 'step')}{suffix}")
    if quote.skipped:
        lines.append(f"  {'not billable'.ljust(14)}{_plural(quote.skipped, 'step')}")
    if quote.assumed_provider:
        assumed = sum(1 for step in quote.steps if step.assumed)
        lines.append(
            f"  {'assumed'.ljust(14)}{_plural(assumed, 'step')} recorded no provider; "
            f"priced as {_truncate(_oneline(quote.assumed_provider), 40)}"
        )
    lines.extend(_breakdown("by model", quote.by_model, rows))
    lines.extend(_breakdown("by provider", quote.by_provider, rows))
    if quote.detail:
        lines.append(f"  note: {quote.detail}")
    return lines


def catalog_providers() -> tuple[str, ...]:
    """Providers the loaded catalog can price for, for a reader to choose from.

    Offered rather than guessed: mapping a model name to a provider by prefix
    would be this console inventing provenance the artifact does not carry.
    """
    catalog = load_catalog()
    if catalog is None:
        return ()
    names: set[str] = set()
    try:
        for card in getattr(catalog, "cards", ()) or ():
            provider = getattr(card, "provider", "")
            if isinstance(provider, str) and provider.strip():
                names.add(_truncate(_oneline(provider), _NAME_LIMIT))
    except Exception:
        return ()
    return tuple(sorted(names))


def unpriced_reason(run: Any) -> str:
    """Why a run's *recorded* cost cannot be read as the cost, or "" if it can.

    The console shows `Run.total_cost` whether or not anything ever priced the
    run. This is the one clause that goes beside it when that number is a floor
    rather than a total, so it names how much of the run is missing instead of
    just doubting all of it.
    """
    billable = unpriced = 0
    for step in _run_steps(run):
        if _read_record(step)[0] not in _PRICEABLE_KINDS:
            continue
        billable += 1
        if not _cost_is_attributable(step):
            unpriced += 1
    if not unpriced:
        return ""
    if unpriced < billable:
        return f"{unpriced} of {billable} model steps recorded no price; the total is a floor"
    if billable == 1:
        return "the one model step recorded no price; $0.00 here means unknown, not free"
    return f"none of the {billable} model steps recorded a price; $0.00 means unknown, not free"


def _cost_is_attributable(step: Any) -> bool:
    """Whether this step's recorded cost came from something that priced it."""
    billing = getattr(step, "billing", None)
    if isinstance(billing, dict):
        status = billing.get("status")
        if isinstance(status, str):
            # An artifact that says "unknown" is not contradicted by a cost
            # field beside it: that is exactly the shape a bare import has after
            # opentine attaches an honest unknown to it.
            return status in _PRICED_STATUSES
    cost = _finite(getattr(step, "cost", None))
    return cost is not None and cost > 0


def _run_steps(run: Any) -> list[Any]:
    """The run's steps, or none. `Run.steps` walks a graph a hostile file wrote."""
    try:
        return list(getattr(run, "steps", None) or ())
    except Exception:
        return []


def _read_record(step: Any) -> tuple[str, str, str, dict[str, Any], str]:
    """Read `(kind, provider, model, usage, id)` off a step or a trace event.

    The two carriers spell two of these differently (`model_info`/`model`,
    `id`/`span_id`), and neither is guaranteed to hold the type it declares once
    the file has been through someone else's hands, so every field is coerced
    rather than trusted.
    """
    kind = getattr(step, "kind", "")
    model = getattr(step, "model_info", None)
    if model is None:
        model = getattr(step, "model", "")
    identifier = getattr(step, "id", None) or getattr(step, "span_id", "")
    usage = getattr(step, "usage", None)
    return (
        str(getattr(kind, "value", kind) or ""),
        step_provider(step),
        str(model or ""),
        dict(usage) if isinstance(usage, dict) else {},
        str(identifier or ""),
    )


def _record_moment(step: Any) -> datetime | None:
    """The instant a step was recorded, in UTC, or None if it recorded none.

    `Step.timestamp` *defaults* to 0.0, so 0 has to read as "not recorded"
    rather than as 1970 — which would price every timestamp-less step against
    rate cards that did not exist yet and report the whole run as unknown.
    """
    raw = _finite(getattr(step, "timestamp", None))
    if raw is None or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _card_date(when: date, moment: datetime | None, pinned: bool) -> date:
    """Which day's rate card prices this step."""
    return when if pinned or moment is None else moment.date()


def _resolve_as_of(effective_at: str | date | None) -> tuple[date, bool, str]:
    """Normalize the requested as-of date to `(date, pinned, note)`.

    A date the console could not parse is not silently swapped for a different
    one: the run is priced unpinned and the quote says so, so the number on
    screen always matches the rule named beside it.
    """
    if effective_at is None:
        return _today(), False, ""
    try:
        return as_date(effective_at), True, ""
    except (AttributeError, TypeError, ValueError):
        return _today(), False, f"{_truncate(_oneline(effective_at), 40)} is not a date"


def _today() -> date:
    return datetime.now(UTC).date()


def _as_of_label(when: date, pinned: bool) -> str:
    return when.isoformat() if pinned else "recorded"


def _quote_step(
    step: Any,
    *,
    catalog: PricingCatalog,
    when: date,
    pinned: bool,
    assume_provider: str = "",
) -> StepQuote | None:
    """Price one step, or None when it is not a billable call."""
    kind, provider, model, usage, identifier = _read_record(step)
    if kind not in _PRICEABLE_KINDS:
        return None
    assumed = False
    if not provider and assume_provider:
        provider, assumed = assume_provider, True
    name = _truncate(_oneline(model), _NAME_LIMIT)
    ident = _truncate(_oneline(identifier), _NAME_LIMIT)
    if not usage:
        # Billing an empty usage dict fabricates a "complete" $0, because every
        # rate times zero tokens is zero. A step that reported no usage at all
        # (a streamed or errored span often reports none) is unknown instead.
        return StepQuote(ident, provider, name, _UNKNOWN, None, None,
                         "no billable usage recorded", assumed)
    try:
        result = bill(
            provider,
            model,
            Usage.from_dict(usage),
            catalog=catalog,
            effective_at=_card_date(when, _record_moment(step), pinned),
        )
    except Exception as e:
        # `Usage` rejects a non-integer, negative or unsafely large token count,
        # and a foreign artifact is full of them. That is a fact about the step,
        # not a failure of the console, so it is reported on the step's own row.
        return StepQuote(ident, provider, name, "error", None, None, _reason(e), assumed)
    status = _truncate(_oneline(getattr(result, "status", "")), 24) or _UNKNOWN
    card = getattr(result, "rate_card_id", None)
    card_id = _truncate(_oneline(card), _NAME_LIMIT) if isinstance(card, str) and card else None
    if status not in _PRICED_STATUSES:
        return StepQuote(
            ident, provider, name, status, None, card_id, _first_warning(result), assumed
        )
    amount = _to_float(getattr(result, "known_subtotal_usd", None))
    if amount is None:
        return StepQuote(ident, provider, name, _UNKNOWN, None, card_id,
                         "the catalog returned an amount that cannot be totalled", assumed)
    return StepQuote(
        ident, provider, name, status, amount, card_id, _first_warning(result), assumed
    )


def _needs_recovery(steps: list[Any]) -> bool:
    """Whether any billable step's provider has to be read out of its billing.

    True for every artifact written before `Step.provider` (opentine 0.8.0),
    which is what makes the difference between a priced run and a run reported
    as entirely unknown.
    """
    for step in steps:
        kind, provider, _model, _usage, _id = _read_record(step)
        if kind in _PRICEABLE_KINDS and provider and not getattr(step, "provider", ""):
            return True
    return False


def _upstream_quotes(
    run: Any, *, when: date, pinned: bool, catalog: PricingCatalog
) -> list[StepQuote] | None:
    """Quotes from opentine's own pass, or None if it is absent or refused."""
    if _opentine_price_run is None:
        return None
    try:
        pricing = _opentine_price_run(
            run, effective_at=when if pinned else None, catalog=catalog
        )
        prices = list(getattr(pricing, "steps", None) or ())
    except Exception:
        # The pass is unreleased. If a future shape of it raises on an artifact
        # this console can still read, the local rollup below answers instead of
        # the panel going blank.
        return None
    quotes: list[StepQuote] = []
    for price in prices:
        status = _truncate(_oneline(getattr(price, "status", "")), 24) or _UNKNOWN
        amount = _to_float(getattr(price, "known_subtotal_usd", None))
        answered = status in _PRICED_STATUSES and amount is not None
        card = getattr(price, "rate_card_id", None)
        quotes.append(
            StepQuote(
                _truncate(_oneline(getattr(price, "step_id", "")), _NAME_LIMIT),
                _truncate(_oneline(getattr(price, "provider", "")), _NAME_LIMIT),
                _truncate(_oneline(getattr(price, "model", "")), _NAME_LIMIT),
                status if answered else _UNKNOWN,
                amount if answered else None,
                _truncate(_oneline(card), _NAME_LIMIT) if isinstance(card, str) and card else None,
                _first_warning_of(getattr(price, "billing", None)),
            )
        )
    return quotes


def _roll_up(
    quotes: list[StepQuote],
    *,
    skipped: int,
    catalog: PricingCatalog,
    effective_at: str,
    note: str,
    assumed_provider: str = "",
) -> RunQuote:
    """Total the known amounts, and name what could not be totalled."""
    total = Decimal("0")
    by_model: dict[str, Decimal] = {}
    by_provider: dict[str, Decimal] = {}
    unknown_models: list[str] = []
    priced = unknown = failed = 0
    for quote in quotes:
        if quote.status not in _PRICED_STATUSES or quote.amount_usd is None:
            unknown += 1
            if quote.status == "error":
                failed += 1
            if quote.model and quote.model not in unknown_models:
                unknown_models.append(quote.model)
            continue
        priced += 1
        amount = Decimal(str(quote.amount_usd))
        total += amount
        by_model[quote.model] = by_model.get(quote.model, Decimal("0")) + amount
        # No rate card carries an empty provider, so a step that priced always
        # has one; the guard keeps a nameless subtotal out of an attribution.
        if quote.provider:
            by_provider[quote.provider] = by_provider.get(quote.provider, Decimal("0")) + amount
    return RunQuote(
        available=True,
        total_usd=float(total),
        priced=priced,
        unknown=unknown,
        skipped=skipped,
        by_model={name: float(value) for name, value in by_model.items()},
        by_provider={name: float(value) for name, value in by_provider.items()},
        unknown_models=tuple(unknown_models),
        catalog_id=_truncate(_oneline(getattr(catalog, "id", "")), _NAME_LIMIT),
        catalog_hash=_truncate(_oneline(getattr(catalog, "hash", "")), _NAME_LIMIT),
        effective_at=effective_at,
        steps=tuple(quotes),
        detail=_join(note, _shortfall(priced, unknown, failed)),
        assumed_provider=assumed_provider,
    )


def _shortfall(priced: int, unknown: int, failed: int) -> str:
    """One clause naming how much of the run the catalog could not answer for."""
    if not unknown:
        return ""
    unreadable = f", {_plural(failed, 'step')} unreadable" if failed else ""
    if priced:
        return (f"{unknown} of {priced + unknown} model steps have no price in this "
                f"catalog; the total is a floor{unreadable}")
    return f"this catalog priced none of the {_plural(unknown, 'model step')}{unreadable}"


def _breakdown(heading: str, subtotals: dict[str, float], rows: int) -> list[str]:
    if not subtotals:
        return []
    ordered = sorted(subtotals.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"  {heading}"]
    for name, amount in ordered[:rows]:
        lines.append(f"    {_truncate(name, _COLUMN).ljust(_COLUMN)} {_format_usd(amount)}")
    remaining = len(ordered) - rows
    if remaining > 0:
        lines.append(f"    (+{remaining} more)")
    return lines


def _format_usd(amount: float | None) -> str:
    """Money, or the word for the absence of it. Never a zero standing in."""
    if amount is None:
        return "unknown"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(value):
        return "unknown"
    # A single cheap call rounds to $0.0000 at four places, which is the one
    # rendering indistinguishable from the free reading this module exists to
    # avoid, so a small non-zero amount is given the digits it needs.
    return f"${value:.6f}" if 0 < value < 0.0001 else f"${value:,.4f}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _join(*clauses: str) -> str:
    return _truncate("; ".join(clause for clause in clauses if clause), _DETAIL_LIMIT)


def _reason(exc: Exception) -> str:
    return _truncate(_oneline(f"{type(exc).__name__}: {exc}"), _DETAIL_LIMIT)


def _first_warning(result: Any) -> str:
    warnings = getattr(result, "warnings", None)
    if isinstance(warnings, (list, tuple)) and warnings:
        return _truncate(_oneline(warnings[0]), _DETAIL_LIMIT)
    return ""


def _first_warning_of(billing: Any) -> str:
    """The same, from a serialized billing record rather than a live result."""
    if isinstance(billing, dict):
        warnings = billing.get("warnings")
        if isinstance(warnings, (list, tuple)) and warnings:
            return _truncate(_oneline(warnings[0]), _DETAIL_LIMIT)
    return ""


def _finite(value: Any) -> float | None:
    """A real number read off an untrusted record, or None if it is not one.

    `int` is unbounded in Python and `math.isfinite` *raises* rather than
    answering on one too large to be a float — which is what a hand-edited
    `cost` or `timestamp` of `10**400` is. A magnitude no float can hold is not
    a usable number here, so it reads as "nothing was recorded", the same as a
    missing field, instead of taking the caller down.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ArithmeticError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_float(value: Any) -> float | None:
    """A usable non-negative amount, or None. `Decimal("1e999999")` floats to inf."""
    try:
        number = float(value)
    except (ArithmeticError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None
