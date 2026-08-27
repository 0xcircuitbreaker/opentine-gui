"""The console must never print a zero it cannot attribute.

Every test here is really one assertion in two halves: that a run opentine can
price shows a number carrying the catalog that produced it, and that a run it
cannot price shows nothing at all rather than `$0.0000`. The imported-shape run
is the reason the module exists — it carries usage and no cost, which is exactly
the artifact the old total reported as free.

The catalog is a signed file on disk that a user may overlay, so the suite pins
the loader to the bundled document (an empty XDG config directory and an empty
working directory) rather than reading whatever the developer has installed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import pricing


@pytest.fixture(autouse=True)
def bundled_catalog_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Price against opentine's own catalog and nothing the developer installed.

    `load_catalogs` layers `$XDG_CONFIG_HOME/opentine/pricing.json` and
    `$CWD/.tine/pricing.json` over the bundled file, so without this the
    subtotals below would depend on the machine running them.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.delenv("TINE_PRICING_CATALOG", raising=False)
    monkeypatch.chdir(tmp_path)
    pricing.load_catalog(refresh=True)
    yield
    pricing.load_catalog(refresh=True)


def today() -> date:
    """The day an unpinned quote cards a timestamp-less step on, in the module's
    own timezone: a local `date.today()` disagrees with it either side of UTC
    midnight, which is one rate-card boundary."""
    return datetime.now(UTC).date()


def priceable_model() -> tuple[str, str] | None:
    """A provider/model the bundled catalog can price today, or None.

    Read off the catalog rather than hard-coded: the rate cards are effective
    dated and are revised on opentine's release cadence, so a literal model name
    here would turn a catalog update into a red suite.
    """
    catalog = pricing.load_catalog()
    if catalog is None:
        return None
    for card in catalog.cards:
        if card.active(today()) and not card.unmetered and card.rates.get("input"):
            return card.provider, card.model
    return None


def model_step(
    identifier: str,
    model: str,
    *,
    provider: str = "",
    parent: str = "",
    usage: dict[str, Any] | None = None,
    cost: float = 0.0,
    billing: Any = None,
    timestamp: float = 0.0,
) -> Step:
    """A model step built field by field: `add_step` cannot set a timestamp.

    0.7.2 has no `Step.provider`, so a step that names its provider names it the
    way a 0.7.x artifact does — inside the billing calculation. Omitting it is
    the shape of a run imported by something that never priced it, and no rate
    card matches an unnamed provider, so it is passed explicitly wherever a test
    means to isolate some *other* reason a step could not be priced.
    """
    if billing is None:
        billing = {"calculation": {"provider": provider}} if provider else {}
    return Step(
        id=identifier,
        parent_ids=[parent] if parent else [],
        kind=StepKind.model,
        inputs={"prompt": "hello"},
        model_info=model,
        usage={"input": 1000, "output": 500} if usage is None else usage,
        cost=cost,
        billing=billing,
        timestamp=timestamp,
    )


def build_run(*steps: Step, run_id: str = "run-under-test") -> Run:
    graph = Graph()
    for step in steps:
        graph.add(step)
    return Run(id=run_id, graph=graph)


def native_billing(provider: str) -> dict[str, Any]:
    """The billing record an opentine-executed step carries."""
    return {
        "status": "complete",
        "amount_usd": "0.0125",
        "known_subtotal_usd": "0.0125",
        "rate_card_id": f"{provider}:priced:2026-01-01",
        "calculation": {"provider": provider},
    }


def test_catalog_loads_or_degrades_honestly() -> None:
    catalog = pricing.load_catalog()
    assert pricing.catalog_available() is (catalog is not None)
    if catalog is None:
        return
    assert catalog.hash and catalog.id


def test_catalog_is_cached_and_refreshable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog is parsed once; loading it per redraw would be felt."""
    first = pricing.load_catalog()
    assert pricing.load_catalog() is first

    calls: list[int] = []
    real = pricing.load_catalogs

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pricing, "load_catalogs", counted)
    pricing.load_catalog()
    assert calls == []
    pricing.load_catalog(refresh=True)
    assert calls == [1]


def test_an_executed_run_prices_and_names_its_catalog() -> None:
    """A run opentine executed re-prices, and every figure is attributable."""
    priceable = priceable_model()
    if priceable is None:
        assert pricing.quote_run(build_run()).available is False
        return
    provider, model = priceable
    run = build_run(
        model_step("a" * 16, model, cost=0.0125, billing=native_billing(provider)),
        model_step("b" * 16, model, parent="a" * 16, cost=0.0125,
                   billing=native_billing(provider)),
    )
    quote = pricing.quote_run(run)

    assert quote.available is True
    assert quote.priced == 2
    assert quote.unknown == 0
    assert quote.total_usd > 0
    assert quote.by_model[model] == pytest.approx(quote.total_usd)
    assert quote.by_provider[provider] == pytest.approx(quote.total_usd)
    assert quote.catalog_hash and quote.catalog_id
    assert all(step.rate_card_id for step in quote.steps)
    # Re-pricing is a report about the artifact, not an edit to it.
    assert [step.cost for step in run.steps] == [0.0125, 0.0125]


def test_an_imported_run_recording_no_cost_still_prices() -> None:
    """The case the module exists for: usage recorded, cost never was.

    An OpenTelemetry import carries `(model, usage)` and `cost = 0.0`, with the
    provider recoverable only from the rate card id the exporter round-tripped.
    Summing the recorded costs calls that run free; the catalog does not.
    """
    priceable = priceable_model()
    if priceable is None:
        assert pricing.quote_run(build_run()).available is False
        return
    provider, model = priceable
    imported = model_step(
        "c" * 16,
        model,
        billing={"rate_card_id": f"{provider}:{model}:2026-01-01"},
    )
    run = build_run(imported)
    assert run.total_cost == 0.0

    quote = pricing.quote_run(run)
    assert quote.available is True
    assert quote.priced == 1
    assert quote.total_usd > 0
    assert quote.by_provider == {provider: pytest.approx(quote.total_usd)}
    assert quote.steps[0].provider == provider
    assert quote.steps[0].status == "complete"


def test_unknown_is_never_reported_as_zero() -> None:
    """A model with no rate card carries no amount, and is named, not zeroed."""
    priceable = priceable_model()
    if priceable is None:
        assert pricing.quote_run(build_run()).available is False
        return
    provider, _ = priceable
    run = build_run(model_step("d" * 16, "no-such-model-9000", provider=provider))
    quote = pricing.quote_run(run)

    assert quote.priced == 0
    assert quote.unknown == 1
    assert quote.total_usd == 0.0
    assert quote.steps[0].amount_usd is None
    assert quote.steps[0].status == "unknown"
    # Named rather than given a 0.0 subtotal, which would read as "free".
    assert quote.unknown_models == ("no-such-model-9000",)
    assert quote.by_model == {}
    assert "no-such-model-9000" not in quote.by_model
    assert quote.detail


def test_usage_that_was_never_recorded_is_unknown_not_free() -> None:
    """Billing an empty usage dict would fabricate a complete $0."""
    priceable = priceable_model()
    if priceable is None:
        return
    provider, model = priceable
    quote = pricing.quote_run(
        build_run(model_step("e" * 16, model, provider=provider, usage={}))
    )
    assert quote.steps[0].status == "unknown"
    assert quote.steps[0].amount_usd is None
    assert quote.total_usd == 0.0


def test_non_model_steps_are_skipped_not_zeroed() -> None:
    """A tool step is not a free call, it is not a billable call."""
    if pricing.load_catalog() is None:
        return
    tool = Step(
        id="f" * 16,
        parent_ids=[],
        kind=StepKind.tool,
        inputs={"cmd": "ls"},
        tool_info={"name": "shell"},
    )
    think = Step(id="g" * 16, parent_ids=["f" * 16], kind=StepKind.think, inputs={"note": "x"})
    quote = pricing.quote_run(build_run(tool, think))

    assert quote.skipped == 2
    assert quote.priced == 0
    assert quote.unknown == 0
    assert quote.steps == ()
    assert quote.by_model == {}
    # Nothing billable was passed over, so there is nothing to caveat.
    assert quote.detail == ""


def test_an_as_of_date_is_pinned_and_reported() -> None:
    """The console offers "price as of <date>", so the quote says which it used."""
    priceable = priceable_model()
    if priceable is None:
        return
    provider, model = priceable
    run = build_run(model_step("h" * 16, model, provider=provider))

    assert pricing.quote_run(run).effective_at == "recorded"
    pinned = pricing.quote_run(run, effective_at=today().isoformat())
    assert pinned.effective_at == today().isoformat()
    assert pinned.total_usd > 0
    assert pricing.quote_run(run, effective_at=today()).total_usd == pinned.total_usd


def test_a_step_recorded_before_the_catalog_existed_is_unknown() -> None:
    """Unpinned, a step is carded on its own day, and an old day has no cards."""
    priceable = priceable_model()
    if priceable is None:
        return
    provider, model = priceable
    ancient = datetime(2015, 6, 1, tzinfo=UTC).timestamp()
    run = build_run(model_step("i" * 16, model, provider=provider, timestamp=ancient))

    assert pricing.quote_run(run).steps[0].amount_usd is None
    # Pinning to a day the catalog covers is what makes the run priceable again.
    priced = pricing.quote_run(run, effective_at=today())
    assert priced.steps[0].amount_usd is not None


def test_an_unparseable_as_of_date_is_not_silently_swapped() -> None:
    quote = pricing.quote_run(build_run(), effective_at="last tuesday")
    assert quote.effective_at == "recorded"
    assert "not a date" in quote.detail


class ForeignStep:
    """A step shape no opentine version produced. `Step` validates usage; a
    trace event, a hand-edited file and a future carrier do not."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


@pytest.mark.parametrize(
    "hostile",
    [
        {"kind": "model", "model_info": "m", "usage": {"input": float("nan")}, "billing": {}},
        {"kind": "model", "model_info": "m", "usage": {"input": 10**30}, "billing": {}},
        {"kind": "model", "model_info": "m", "usage": {"input": -5}, "billing": {}},
        {"kind": "model", "model_info": "m", "usage": "1000 tokens", "billing": "not-a-dict"},
        {"kind": "model", "model_info": {"nested": 1}, "usage": {"input": 1}, "billing": None},
        {"kind": "model", "model_info": "m", "usage": {"input": 1},
         "billing": {"calculation": "a string where a mapping belongs"}},
        {"kind": "model", "model_info": "m", "usage": {"input": 1}, "timestamp": float("inf")},
        {"kind": "model", "model_info": "m", "usage": {"input": 1}, "timestamp": 10**30},
        # `math.isfinite` raises on an int this large rather than answering, so a
        # magnitude no float can hold has to read as "not recorded".
        {"kind": "model", "model_info": "m", "usage": {"input": 1}, "timestamp": 10**400},
        {"kind": "model", "model_info": "m", "usage": {"input": 1}, "cost": 10**400},
        {"kind": "model", "model_info": "m", "usage": {"input": 1}, "cost": float("nan")},
        # A lone surrogate is not encodable UTF-8, and Dear PyGui's text renderer
        # segfaults on one, so it must not survive into a rendered row.
        {"kind": "model", "model_info": "x\ud800y", "usage": {"input": 1}},
        {"kind": {"not": "an enum"}, "usage": {"input": 1}},
        {},
    ],
)
def test_a_hostile_artifact_never_raises_out_of_a_quote(hostile: dict[str, Any]) -> None:
    """An artifact is untrusted input; the price panel must degrade, not crash."""
    step = ForeignStep(id="hostile", **hostile)
    run = ForeignStep(steps=[step])

    quote = pricing.quote_run(run)
    assert isinstance(quote.total_usd, float)
    assert quote.total_usd >= 0.0
    # Nothing hostile is ever counted as a priced zero.
    for step_quote in quote.steps:
        assert step_quote.amount_usd is None or step_quote.amount_usd > 0
        assert step_quote.status != "complete" or step_quote.amount_usd is not None
    lines = pricing.quote_lines(quote)
    assert isinstance(lines, list)
    # Renderable: one row per line, and encodable — a lone surrogate reaching
    # Dear PyGui's native text renderer is a segfault, not a mojibake.
    assert all("\n" not in line and "\r" not in line for line in lines)
    assert all(line.encode("utf-8") for line in lines if line)
    assert isinstance(pricing.unpriced_reason(run), str)


def test_a_hostile_billing_record_does_not_forge_a_price() -> None:
    """`billing` is the one step field opentine does not validate."""
    run = ForeignStep(
        steps=[
            ForeignStep(
                id="j" * 16,
                kind="model",
                model_info="m",
                usage={"input": 1},
                billing={"status": "complete", "known_subtotal_usd": "1e999999",
                         "calculation": {"provider": ["not", "a", "string"]}},
            )
        ]
    )
    quote = pricing.quote_run(run)
    # The forged subtotal is in the artifact, not in the catalog: the quote is
    # derived from the catalog, so the artifact cannot state its own price.
    assert quote.total_usd == 0.0
    assert quote.steps[0].amount_usd is None


def test_a_run_that_raises_on_steps_is_survivable() -> None:
    class Exploding:
        @property
        def steps(self) -> list[Any]:
            raise RuntimeError("boom")

    quote = pricing.quote_run(Exploding())
    assert quote.priced == quote.unknown == quote.skipped == 0
    assert pricing.unpriced_reason(Exploding()) == ""


def test_unpriced_reason_is_silent_on_a_natively_priced_run() -> None:
    run = build_run(
        model_step("k" * 16, "m", cost=0.0125, billing=native_billing("anthropic")),
        model_step("l" * 16, "m", parent="k" * 16, cost=0.0125,
                   billing=native_billing("anthropic")),
    )
    assert pricing.unpriced_reason(run) == ""


def test_unpriced_reason_names_an_uncosted_run() -> None:
    single = build_run(model_step("m" * 16, "m"))
    assert "unknown, not free" in pricing.unpriced_reason(single)

    both = build_run(
        model_step("n" * 16, "m", cost=0.0125, billing=native_billing("anthropic")),
        model_step("o" * 16, "m", parent="n" * 16),
    )
    assert pricing.unpriced_reason(both) == (
        "1 of 2 model steps recorded no price; the total is a floor"
    )


def test_unpriced_reason_trusts_neither_an_unknown_record_nor_a_tool_step() -> None:
    """A recorded cost beside an `unknown` status is what a bare import looks like."""
    unknown_record = build_run(
        model_step("p" * 16, "m", cost=0.0125, billing={"status": "unknown", "amount_usd": None})
    )
    assert pricing.unpriced_reason(unknown_record)

    tool_only = build_run(
        Step(id="q" * 16, parent_ids=[], kind=StepKind.tool, inputs={}, tool_info={"name": "ls"})
    )
    # A run that made no model call really did record no billable cost.
    assert pricing.unpriced_reason(tool_only) == ""


def test_quote_lines_render_one_line_per_row_and_cap_the_breakdown() -> None:
    priceable = priceable_model()
    if priceable is None:
        lines = pricing.quote_lines(pricing.quote_run(build_run()))
        assert lines[0] == "Post-hoc price: unavailable"
        return
    provider, model = priceable
    parent = ""
    steps = []
    for index in range(4):
        identifier = f"{index}" * 16
        steps.append(model_step(identifier, model, provider=provider, parent=parent))
        parent = identifier
    steps.append(
        model_step("z" * 16, "no-such-model-9000", provider=provider, parent=parent)
    )
    quote = pricing.quote_run(build_run(*steps))

    lines = pricing.quote_lines(quote, limit=1)
    assert all(isinstance(line, str) and "\n" not in line for line in lines)
    # The heading names whether the catalog was signed: opentine requires a
    # signature only on its own bundled one, and an unsigned overlay layered
    # over it wins the lookup.
    assert lines[0].startswith(f"Post-hoc price (signed catalog {quote.catalog_hash[:12]}")
    assert any("total" in line and "$" in line for line in lines)
    assert any("unknown" in line for line in lines)

    # A breakdown truncated without saying so reads as the whole run.
    many = pricing.quote_lines(quote, limit=0)
    assert any("more)" in line for line in many)


@pytest.mark.parametrize("limit", [float("inf"), float("nan"), None, "4", -1, 10**400])
def test_quote_lines_survives_a_nonsense_limit(limit: Any) -> None:
    """`limit` is a caller's number, and `int(float("inf"))` raises."""
    quote = pricing.quote_run(build_run(model_step("y" * 16, "no-such-model-9000")))
    lines = pricing.quote_lines(quote, limit=limit)
    assert lines and all(isinstance(line, str) for line in lines)


def test_quote_lines_of_an_unavailable_quote_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "load_catalogs", _refuse)
    pricing.load_catalog(refresh=True)

    assert pricing.catalog_available() is False
    quote = pricing.quote_run(build_run(model_step("r" * 16, "m")))
    assert quote.available is False
    assert quote.total_usd == 0.0
    assert quote.priced == 0
    assert quote.unknown == 1
    assert quote.catalog_id == quote.catalog_hash == ""
    assert "no pricing catalog" in quote.detail

    lines = pricing.quote_lines(quote)
    assert lines[0] == "Post-hoc price: unavailable"
    assert len(lines) == 2


def _refuse(*args: Any, **kwargs: Any) -> Any:
    raise ValueError("no pricing catalog found")


class FakeStepPrice(ForeignStep):
    """The shape `opentine._pricing_pass` returns per step on 0.8.0."""


def test_opentines_own_pass_is_preferred_where_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.8.0 prices a run itself, and there should not be two arithmetics."""
    asked: list[Any] = []

    def fake_price_run(run: Any, *, effective_at: Any = None, catalog: Any = None) -> Any:
        asked.append((run, effective_at, catalog))
        return ForeignStep(
            steps=[
                FakeStepPrice(step_id="s1", provider="anthropic", model="claude-x",
                              status="complete", known_subtotal_usd=0.25,
                              rate_card_id="anthropic:claude-x:2026-01-01", billing={}),
                FakeStepPrice(step_id="s2", provider="openai", model="ghost",
                              status="unknown", known_subtotal_usd=0.0, rate_card_id=None,
                              billing={"warnings": ["no exact provider/model rate card"]}),
            ]
        )

    monkeypatch.setattr(pricing, "_opentine_price_run", fake_price_run)
    run = build_run(
        model_step("s" * 16, "claude-x"),
        Step(id="t" * 16, parent_ids=["s" * 16], kind=StepKind.tool, inputs={}),
        model_step("u" * 16, "ghost", parent="t" * 16),
    )
    quote = pricing.quote_run(run, effective_at=date(2026, 8, 1))

    assert asked and asked[0][1] == date(2026, 8, 1)
    assert quote.priced == 1
    assert quote.unknown == 1
    assert quote.total_usd == pytest.approx(0.25)
    assert quote.by_provider == {"anthropic": pytest.approx(0.25)}
    assert quote.unknown_models == ("ghost",)
    # A step the pass did not answer for was not a billable call.
    assert quote.skipped == 1
    assert quote.steps[1].detail


def test_a_refusing_upstream_pass_falls_back_to_the_local_rollup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass is unreleased; a future shape of it must not blank the panel."""
    priceable = priceable_model()
    if priceable is None:
        return
    provider, model = priceable

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("price_run() got an unexpected keyword argument")

    monkeypatch.setattr(pricing, "_opentine_price_run", exploding)
    quote = pricing.quote_run(build_run(model_step("v" * 16, model, provider=provider)))

    assert quote.available is True
    assert quote.priced == 1
    assert quote.total_usd > 0


def test_a_model_name_cannot_open_a_row_of_its_own() -> None:
    """The price block is flat text, so an artifact-supplied name is one line.

    A model called "x\\n  total  $0.0000" would otherwise render a total row
    indistinguishable from the console's own, beneath the real one.
    """
    if pricing.load_catalog() is None:
        return
    forged = "x\n  total         $9,999.0000\r  note: verified"
    quote = pricing.quote_run(build_run(model_step("w" * 16, forged)))
    lines = pricing.quote_lines(quote)

    assert all("\n" not in line and "\r" not in line for line in lines)
    assert all("\n" not in name for name in quote.unknown_models)
    assert len([line for line in lines if line.lstrip().startswith("total")]) == 1


# ---- opentine 0.8.0: time-of-day cards and unmetered local servers ----------


def _scheduled_run(hour_utc: int, provider: str = "deepseek") -> Any:
    """A run whose one model step ran at a fixed UTC instant.

    01:00-04:00 UTC on a Wednesday is inside the peak window the bundled
    catalog's DeepSeek V4 cards carry; 12:30 is outside it.
    """
    when = datetime(2026, 8, 19, hour_utc, 30, tzinfo=UTC)
    graph = Graph()
    graph.add(
        Step(
            id="m1",
            parent_ids=[],
            kind=StepKind.model,
            inputs={"text": "x"},
            model_info="deepseek-v4-pro",
            provider=provider,
            timestamp=when.timestamp(),
            usage={"input": 1_000_000, "output": 1_000_000},
        )
    )
    return Run(
        id=f"h{hour_utc}",
        graph=graph,
        status=RunStatus.completed,
        created_at=when.timestamp(),
    )


def test_a_scheduled_card_is_priced_at_the_window_the_step_ran_in() -> None:
    # opentine-pricing/2 (0.8.0) gave rate cards peak/off-peak windows, chosen by
    # the instant a step was recorded. Billing from the as-of *date* alone prices
    # every scheduled card at its base rate — which for DeepSeek is the off-peak
    # one, so a peak run reported half of what it cost.
    peak = pricing.quote_run(_scheduled_run(3))
    off_peak = pricing.quote_run(_scheduled_run(12))
    if not peak.available:  # no catalog in this environment
        pytest.skip("no pricing catalog available")
    assert peak.total_usd > off_peak.total_usd
    assert peak.total_usd == pytest.approx(off_peak.total_usd * 2, rel=1e-6)


def test_the_console_and_tine_price_agree_across_a_peak_window() -> None:
    upstream = pytest.importorskip("opentine._pricing_pass")
    for hour in (3, 12):
        run = _scheduled_run(hour)
        quote = pricing.quote_run(run)
        if not quote.available:
            pytest.skip("no pricing catalog available")
        assert quote.total_usd == pytest.approx(upstream.price_run(run).total_cost)


def test_a_recovered_provider_is_also_priced_at_its_window() -> None:
    # The path that has to compute locally: a pre-0.8.0 artifact records no
    # provider, so opentine's own pass answers "unknown" and the console's
    # rollup takes over. It must select the window too.
    peak = pricing.quote_run(_scheduled_run(3, provider=""), assume_provider="deepseek")
    off_peak = pricing.quote_run(_scheduled_run(12, provider=""), assume_provider="deepseek")
    if not peak.available:
        pytest.skip("no pricing catalog available")
    assert peak.total_usd == pytest.approx(off_peak.total_usd * 2, rel=1e-6)
    assert peak.assumed_provider == "deepseek"


def test_a_step_recorded_unmetered_says_so_rather_than_only_unknown() -> None:
    # 0.8.0 made thirteen local model servers nameable, all recorded unmetered.
    # No catalog carries a card for one, so the post-hoc status is "unknown" —
    # a statement about the catalog, which without this reads as a statement
    # about the call.
    graph = Graph()
    graph.add(
        Step(
            id="m1",
            parent_ids=[],
            kind=StepKind.model,
            inputs={"text": "x"},
            model_info="llama-3.3-70b",
            provider="vllm",
            usage={"input": 1000, "output": 500},
            billing={"status": "unmetered", "known_subtotal_usd": 0.0},
        )
    )
    quote = pricing.quote_run(Run(id="local", graph=graph, status=RunStatus.completed))
    if not quote.available:
        pytest.skip("no pricing catalog available")
    assert quote.unmetered_at_capture == 1
    lines = pricing.quote_lines(quote)
    assert any("unmetered" in line and "local server" in line for line in lines)
    # And it is still not counted as priced: the catalog answered nothing.
    assert quote.priced == 0


def test_status_counts_carry_the_catalogs_own_vocabulary() -> None:
    quote = pricing.quote_run(_scheduled_run(3))
    if not quote.available:
        pytest.skip("no pricing catalog available")
    assert quote.status_counts == {"complete": 1}
