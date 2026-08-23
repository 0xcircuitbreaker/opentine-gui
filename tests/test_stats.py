"""Cross-run aggregation.

The two properties worth defending are that a figure nobody collected never
becomes a zero, and that a run nobody could read never becomes a run that cost
nothing. Both are ways of understating what a directory of agent runs spent,
which is the question this module exists to answer.

The hostile cases build shapes `opentine` itself will not construct — a NaN
cost, a string where a number belongs, a property that raises — because a
`.tine` file is written by someone else and the console decodes it anyway.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.sources import RunEntry
from opentine_gui.stats import (
    GROUPINGS,
    MAX_KEYS_PER_RUN,
    MAX_TOKENS,
    Bucket,
    csv_rows,
    rollup,
    rollup_lines,
)

DAY = 1_700_000_000.0


def _run(
    run_id: str,
    *,
    model: str = "model-a",
    costs: tuple[float, ...] = (0.0,),
    step_models: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    status: RunStatus = RunStatus.completed,
    usage: dict[str, int] | None = None,
    durations: tuple[float, ...] = (),
    billings: tuple[dict[str, Any], ...] = (),
    created_at: float = DAY,
    pricing: object = None,
) -> Run:
    """A real run built through opentine's own API, so the fixtures cannot drift."""
    graph = Graph()
    parents: list[str] = []
    for index, cost in enumerate(costs):
        step = Step(
            id=f"{run_id}-s{index}",
            parent_ids=parents,
            kind=StepKind.model,
            inputs={"prompt": "hello"},
            model_info=step_models[index] if index < len(step_models) else model,
            cost=cost,
            duration=durations[index] if index < len(durations) else 0.0,
            usage=dict(usage or {}),
            billing=dict(billings[index]) if index < len(billings) else {},
        )
        graph.add(step)
        parents = [step.id]
    manifest = {"pricing": pricing} if pricing is not None else {}
    run = Run(
        id=run_id,
        graph=graph,
        status=status,
        model_info=model,
        tags=list(tags),
        manifest=manifest,
    )
    # Run.__init__ substitutes "now" for a falsy created_at, so a run with no
    # timestamp has to be made after construction.
    run.created_at = created_at
    return run


@dataclass
class _ForeignStep:
    """A step shape from an opentine newer or stranger than the pinned floor."""

    provider: Any = ""
    usage: Any = field(default_factory=dict)
    duration: Any = 0.0
    model_info: Any = ""


@dataclass
class _ForeignRun:
    """Not a Run at all: whatever a decoded artifact hands the rollup."""

    total_cost: Any = 0.0
    steps: Any = field(default_factory=list)
    status: Any = "completed"
    created_at: Any = DAY
    tags: Any = ()
    model_info: Any = ""
    format_version: Any = 2
    manifest: Any = field(default_factory=dict)


def _labels(result) -> list[str]:
    return [bucket.label for bucket in result.buckets]


def _bucket(result, label: str) -> Bucket:
    return next(bucket for bucket in result.buckets if bucket.label == label)


# --- groupings ---------------------------------------------------------------


def test_every_advertised_grouping_produces_buckets() -> None:
    runs = [_run("a", tags=("release",), costs=(0.5,))]
    for grouping in GROUPINGS:
        result = rollup(runs, group_by=grouping)
        assert result.group_by == grouping
        assert len(result.buckets) == 1
        assert result.total.runs == 1


def test_status_grouping_totals_each_status() -> None:
    runs = [
        _run("a", costs=(1.0,), status=RunStatus.completed),
        _run("b", costs=(0.25,), status=RunStatus.failed),
        _run("c", costs=(0.5,), status=RunStatus.completed),
    ]
    result = rollup(runs, group_by="status")
    assert _labels(result) == ["completed", "failed"]
    assert _bucket(result, "completed").runs == 2
    assert _bucket(result, "completed").cost == 1.5
    assert result.statuses == {"completed": 2, "failed": 1}
    assert result.total.runs == 3
    assert result.total.steps == 3


def test_model_grouping_counts_a_run_under_every_model_it_used() -> None:
    runs = [_run("a", model="declared", step_models=("used-a", "used-b"), costs=(0.1, 0.2))]
    result = rollup(runs, group_by="model")
    assert sorted(_labels(result)) == ["declared", "used-a", "used-b"]
    assert result.total.runs == 1
    assert result.total.models == ("declared", "used-a", "used-b")


def test_model_grouping_splits_the_cost_instead_of_repeating_it() -> None:
    # A run appears under every model it used, but its money does not: adding
    # the whole run cost to each key made the rows sum to more than the run,
    # while `tine stats` (which groups on the declared model alone) never
    # double-counted. The split is opentine's own `cost_breakdown().by_model`.
    run = _run("a", model="", step_models=("claude-opus", "cheap-haiku"), costs=(10.0, 0.0))
    result = rollup([run], group_by="model")
    by_label = {bucket.label: bucket.cost for bucket in result.buckets}
    assert by_label == {"claude-opus": 10.0, "cheap-haiku": 0.0}
    assert math.isclose(sum(by_label.values()), result.total.cost)
    # The dearest model leads, because the ordering is by attributed spend.
    assert _labels(result)[0] == "claude-opus"


def test_a_run_naming_no_model_groups_under_none() -> None:
    result = rollup([_run("a", model="", step_models=("",))], group_by="model")
    assert _labels(result) == ["(none)"]


def test_tag_grouping_counts_a_run_under_each_tag() -> None:
    runs = [
        _run("a", tags=("release", "smoke"), costs=(1.0,)),
        _run("b", costs=(0.5,)),
    ]
    result = rollup(runs, group_by="tag")
    assert _labels(result) == ["release", "smoke", "(untagged)"]
    assert _bucket(result, "release").runs == 1
    assert result.tags == {"release": 1, "smoke": 1}
    # The run count is still two: buckets overlap, the total does not.
    assert result.total.runs == 2


def test_a_run_carrying_absurdly_many_tags_is_capped() -> None:
    run = _ForeignRun(tags=tuple(f"tag-{index:04d}" for index in range(MAX_KEYS_PER_RUN * 4)))
    result = rollup([run], group_by="tag")
    assert len(result.buckets) == MAX_KEYS_PER_RUN
    assert result.total.runs == 1


def test_a_run_carrying_absurdly_many_tags_is_capped_everywhere_it_is_read() -> None:
    # The cap has to hold on the histogram and the export too: bounding only the
    # table still let one run put a thousand tags on the summary line above it.
    run = _ForeignRun(tags=tuple(f"tag-{index:04d}" for index in range(MAX_KEYS_PER_RUN * 4)))
    result = rollup([run], group_by="tag")
    assert len(result.tags) == MAX_KEYS_PER_RUN
    assert all(len(line) < 2_000 for line in rollup_lines(result, limit=0))


def test_the_models_one_run_names_are_capped_but_keep_the_declared_one() -> None:
    run = _ForeignRun(
        total_cost=0.1,
        model_info="zzz-declared",
        steps=[_ForeignStep(model_info=f"m-{index:04d}") for index in range(MAX_KEYS_PER_RUN * 4)],
    )
    result = rollup([run], group_by="model")
    assert len(result.total.models) == MAX_KEYS_PER_RUN
    assert len(csv_rows(result)[1][-1]) < 2_000
    # It sorts last of the thousand, and it is still there: the model the run
    # declares is the one `tine stats` reports, so the cap may not evict it.
    assert "zzz-declared" in result.total.models
    assert "zzz-declared" in _labels(result)


def test_day_grouping_uses_the_local_calendar_day() -> None:
    later = DAY + 86_400 * 3
    result = rollup([_run("a", created_at=DAY), _run("b", created_at=later)], group_by="day")
    expected = {
        time.strftime("%Y-%m-%d", time.localtime(DAY)),
        time.strftime("%Y-%m-%d", time.localtime(later)),
    }
    assert set(_labels(result)) == expected


def test_a_run_without_a_timestamp_groups_under_undated() -> None:
    result = rollup([_run("a", created_at=0.0)], group_by="day")
    assert _labels(result) == ["(undated)"]
    assert result.oldest == 0.0 and result.newest == 0.0


def test_the_window_spans_only_runs_that_carry_a_timestamp() -> None:
    runs = [_run("a", created_at=DAY), _run("b", created_at=DAY + 60), _run("c", created_at=0.0)]
    result = rollup(runs)
    assert result.oldest == DAY
    assert result.newest == DAY + 60


def test_format_version_grouping_and_histogram() -> None:
    result = rollup([_run("a"), _run("b")], group_by="format-version")
    assert _labels(result) == ["2"]
    assert result.formats == {"2": 2}


def test_provider_grouping_is_unrecorded_when_nothing_recorded_one() -> None:
    # No `provider` field (it arrived after the pinned floor) and no billing
    # record to recover it from: this run really does not name a provider.
    result = rollup([_run("a")], group_by="provider")
    assert _labels(result) == ["(unrecorded)"]


def test_provider_is_recovered_from_a_pinned_release_billing_record() -> None:
    # `Step.provider` does not exist in 0.7.2, but its adapter still wrote the
    # provider into the billing calculation and into the rate card id it chose,
    # which is what the run inspector reads. Grouping on a field the pinned
    # release cannot have would report every artifact it writes as unrecorded.
    run = _run(
        "a",
        costs=(0.1, 0.2),
        billings=(
            {"calculation": {"provider": "anthropic"}},
            {"rate_card_id": "openai:gpt-5"},
        ),
    )
    assert not hasattr(run.steps[0], "provider")
    result = rollup([run], group_by="provider")
    assert sorted(_labels(result)) == ["anthropic", "openai"]
    assert result.total.runs == 1
    # And each provider carries what its own steps spent, not the run total.
    assert {b.label: b.cost for b in result.buckets} == {"anthropic": 0.1, "openai": 0.2}


def test_provider_grouping_reads_a_newer_steps_provider() -> None:
    run = _ForeignRun(steps=[_ForeignStep(provider="anthropic"), _ForeignStep(provider="openai")])
    result = rollup([run], group_by="provider")
    assert sorted(_labels(result)) == ["anthropic", "openai"]
    assert result.total.runs == 1


# --- absent is not zero ------------------------------------------------------


def test_tokens_are_absent_not_zero_when_no_step_recorded_usage() -> None:
    result = rollup([_run("a", costs=(0.5,))])
    assert result.total.tokens is None
    assert result.buckets[0].tokens is None


def test_tokens_sum_only_the_runs_that_recorded_them() -> None:
    runs = [
        _run("a", usage={"input": 10, "output": 5}),
        _run("b"),
    ]
    result = rollup(runs)
    assert result.total.tokens == 15


def test_a_declared_total_is_not_double_counted_with_its_dimensions() -> None:
    result = rollup([_run("a", usage={"input": 10, "output": 5, "total": 15})])
    assert result.total.tokens == 15


def test_duration_is_absent_when_nothing_was_timed() -> None:
    result = rollup([_run("a", costs=(0.1, 0.2))])
    assert result.total.duration is None


def test_duration_sums_only_timed_steps() -> None:
    result = rollup([_run("a", costs=(0.1, 0.2), durations=(1.5, 0.0))])
    assert result.total.duration == 1.5


def test_absent_figures_render_as_a_dash() -> None:
    lines = rollup_lines(rollup([_run("a", costs=(0.5,))]))
    assert "Tokens: -" in lines[1]
    assert "Duration: -" in lines[1]
    assert " 0 " not in lines[1]


def test_a_bucket_mixing_recorded_and_unrecorded_reports_the_recorded_sum() -> None:
    runs = [
        _run("a", status=RunStatus.completed, usage={"input": 4}, durations=(2.0,)),
        _run("b", status=RunStatus.completed),
    ]
    bucket = rollup(runs).buckets[0]
    assert bucket.runs == 2
    assert bucket.tokens == 4
    assert bucket.duration == 2.0


# --- pricing -----------------------------------------------------------------


def test_partial_pricing_marks_its_bucket_and_the_total() -> None:
    runs = [
        _run("a", costs=(1.0,), tags=("x",), pricing={"complete": False}),
        _run("b", costs=(0.5,), tags=("y",)),
    ]
    result = rollup(runs, group_by="tag")
    assert _bucket(result, "x").cost_partial is True
    assert _bucket(result, "y").cost_partial is False
    assert result.total.cost_partial is True


def test_only_a_literal_true_counts_as_completely_priced() -> None:
    # opentine's own rule (_runtime_accounting): anything that is not literally
    # True — False, 0, "false", null — is not a proven-complete claim, and it
    # breaches a strict_cost budget on exactly those values. Reading only
    # `is False` let an artifact drop the lower-bound marker by writing "no".
    assert rollup([_run("a", pricing={"complete": True})]).total.cost_partial is False
    assert rollup([_run("c", pricing="not-a-mapping")]).total.cost_partial is False
    assert rollup([_run("d", pricing={})]).total.cost_partial is False
    for claim in ("no", None, 0, "true"):
        result = rollup([_run("b", costs=(1.0,), pricing={"complete": claim})])
        assert result.total.cost_partial is True, claim


def test_a_lower_bound_cost_is_marked_in_the_rendered_block() -> None:
    lines = rollup_lines(rollup([_run("a", costs=(1.0,), pricing={"complete": False})]))
    assert ">=$1.0000" in lines[0]
    assert any("lower bound" in line for line in lines)


# --- hostile artifacts -------------------------------------------------------


def test_a_nan_cost_is_unreadable_and_poisons_no_total() -> None:
    runs = [_run("a", costs=(1.0,)), _ForeignRun(total_cost=float("nan"))]
    result = rollup(runs)
    assert result.unreadable == 1
    assert result.total.runs == 1
    assert result.total.cost == 1.0
    assert not math.isnan(result.total.cost)


def test_a_string_where_a_cost_belongs_is_unreadable() -> None:
    result = rollup([_ForeignRun(total_cost="0.50")])
    assert result.unreadable == 1
    assert result.total.runs == 0
    assert result.buckets == ()


def test_an_infinite_or_absurd_cost_is_unreadable() -> None:
    result = rollup([_ForeignRun(total_cost=float("inf")), _ForeignRun(total_cost=1e18)])
    assert result.unreadable == 2
    # Nothing readable was priced, so there is no total to report. Absent, not
    # zero: a zero here would read as "these runs cost nothing".
    assert result.total.cost is None


def test_a_negative_cost_is_unreadable() -> None:
    assert rollup([_ForeignRun(total_cost=-1.0)]).unreadable == 1


def test_a_run_whose_cost_property_raises_is_unreadable() -> None:
    class _Exploding(Run):
        @property
        def total_cost(self) -> float:
            raise ValueError("boom")

    result = rollup([_Exploding(id="a"), _run("b", costs=(2.0,))])
    assert result.unreadable == 1
    assert result.total.cost == 2.0


def test_a_run_whose_steps_property_raises_is_unreadable() -> None:
    class _Stepless(Run):
        @property
        def steps(self) -> list[Step]:
            raise RuntimeError("boom")

    assert rollup([_Stepless(id="a")]).unreadable == 1


def test_a_run_whose_timestamp_raises_is_undated_rather_than_dropped() -> None:
    class _Undatable:
        """Every figure readable except the clock. Not a Run subclass: Run reads
        its own created_at while constructing, so the raise has to come later."""

        total_cost = 0.5
        steps: list[Any] = []
        status = "completed"
        tags = ()
        model_info = ""
        format_version = 2
        manifest: dict[str, Any] = {}

        @property
        def created_at(self) -> float:
            raise RuntimeError("clock")

    result = rollup([_Undatable()], group_by="day")
    # A timestamp that cannot be read is a missing timestamp, not a missing run:
    # dropping the run would understate the spend the rollup exists to report.
    assert result.unreadable == 0
    assert _labels(result) == ["(undated)"]


def test_hostile_step_fields_are_skipped_rather_than_raising() -> None:
    run = _ForeignRun(
        steps=[
            _ForeignStep(usage="not-a-mapping", duration="soon"),
            _ForeignStep(usage={"input": "many", "output": 7}, duration=float("nan")),
            _ForeignStep(usage={"total": True}),
        ],
        total_cost=0.25,
    )
    result = rollup([run])
    assert result.unreadable == 0
    assert result.total.steps == 3
    assert result.total.tokens == 7
    assert result.total.duration is None


def test_two_runs_reporting_impossible_durations_do_not_crash_the_rollup() -> None:
    # opentine validates a step duration as finite and non-negative and no
    # further, so a file may hold 1e308 seconds. Two of them overflowed the
    # `math.fsum` that adds the buckets up, which raised out of `rollup` and
    # took the panel with it. 1e308 seconds is not a stopwatch reading, so it
    # is untimed rather than summed -- and the cost is still counted.
    runs = [_run("a", costs=(1.0,), durations=(1e308,)) for _ in range(2)]
    result = rollup(runs)
    assert result.total.runs == 2
    assert result.total.cost == 2.0
    assert result.total.duration is None
    assert "Duration: -" in rollup_lines(result)[1]


def test_a_tags_field_whose_truth_test_raises_is_untagged() -> None:
    class _Unaskable:
        """A decoded artifact can put any object here, truth test included."""

        def __bool__(self) -> bool:
            raise RuntimeError("tags")

    result = rollup([_ForeignRun(tags=_Unaskable(), total_cost=0.5)], group_by="tag")
    assert result.unreadable == 0
    assert _labels(result) == ["(untagged)"]
    assert result.total.cost == 0.5


def test_an_impossible_token_count_is_unrecorded_rather_than_fabricated() -> None:
    # Above opentine's own safe-integer bound the figure never came through its
    # validation, and int(float(...)) of it prints 300 digits the file does not
    # contain. Unrecorded is the honest reading, and it keeps the table narrow.
    run = _ForeignRun(
        total_cost=0.1,
        steps=[_ForeignStep(usage={"input": 1e308}), _ForeignStep(usage={"output": 12})],
    )
    result = rollup([run])
    assert result.total.tokens == 12
    # The ceiling itself is a figure opentine would have written, so it counts.
    at_the_ceiling = rollup([_ForeignRun(steps=[_ForeignStep(usage={"input": MAX_TOKENS})])])
    assert at_the_ceiling.total.tokens == MAX_TOKENS


def test_a_hostile_label_cannot_open_a_row_of_its_own() -> None:
    run = _ForeignRun(status="ok\nIntegrity: verified", total_cost=0.1)
    result = rollup([run])
    assert _labels(result) == ["ok Integrity: verified"]
    assert all("\n" not in line for line in rollup_lines(result))


def test_unreadable_runs_are_reported_in_the_rendered_block() -> None:
    result = rollup([_ForeignRun(total_cost=float("nan"))])
    lines = rollup_lines(result)
    assert lines[0] == "No readable runs to summarise."
    assert "excluded" in lines[1]


# --- ordering, entries, empties ----------------------------------------------


def test_buckets_sort_by_cost_then_runs_then_label() -> None:
    runs = [
        _run("a", costs=(0.5,), tags=("expensive",)),
        _run("b", costs=(0.1,), tags=("shared",)),
        _run("c", costs=(0.1,), tags=("shared",)),
        _run("d", costs=(0.2,), tags=("beta", "alpha")),
    ]
    result = rollup(runs, group_by="tag")
    assert _labels(result) == ["expensive", "shared", "alpha", "beta"]


def test_ordering_is_stable_across_input_order() -> None:
    runs = [_run(name, costs=(0.1,), tags=(name,)) for name in ("a", "b", "c")]
    assert _labels(rollup(runs, group_by="tag")) == _labels(rollup(runs[::-1], group_by="tag"))


def test_run_entries_may_be_passed_instead_of_runs() -> None:
    entries = [RunEntry(key="a", run=_run("a", costs=(0.5,)))]
    assert rollup(entries).total.cost == 0.5


def test_an_unknown_grouping_falls_back_to_status_and_says_so() -> None:
    result = rollup([_run("a")], group_by="colour")
    assert result.group_by == "status"
    assert _labels(result) == ["completed"]


def test_a_grouping_name_is_normalised() -> None:
    assert rollup([_run("a")], group_by="Format_Version").group_by == "format-version"


def test_empty_input_produces_an_empty_rollup() -> None:
    result = rollup([])
    assert result.total == Bucket("all", 0, 0, None, False, None, None, ())
    assert result.buckets == ()
    assert result.statuses == {} and result.formats == {} and result.tags == {}
    assert result.oldest == 0.0 and result.newest == 0.0
    assert result.unreadable == 0
    assert rollup_lines(result) == ["No readable runs to summarise."]


# --- rendering ---------------------------------------------------------------


def test_the_table_is_limited_and_says_what_it_hid() -> None:
    runs = [_run(f"r{index}", tags=(f"x{index}",), costs=(index / 10,)) for index in range(10)]
    lines = rollup_lines(rollup(runs, group_by="tag"), limit=3)
    assert any("(7 more group(s) not shown)" in line for line in lines)
    assert sum(1 for line in lines if line.startswith("  x")) == 3


def test_a_limit_of_zero_shows_every_group() -> None:
    runs = [_run(f"r{index}", tags=(f"t{index}",)) for index in range(5)]
    lines = rollup_lines(rollup(runs, group_by="tag"), limit=0)
    assert not any("not shown" in line for line in lines)


def test_overlapping_buckets_are_announced() -> None:
    lines = rollup_lines(rollup([_run("a", tags=("x", "y"))], group_by="tag"))
    assert any("appears under every tag" in line for line in lines)
    assert not any("appears under every" in line for line in rollup_lines(rollup([_run("a")])))


# --- csv ---------------------------------------------------------------------


def test_csv_rows_lead_with_a_header_and_the_total() -> None:
    rows = csv_rows(rollup([_run("a", costs=(1.0,), usage={"input": 3}, durations=(2.0,))]))
    assert rows[0][:5] == ["group", "label", "runs", "steps", "cost"]
    assert rows[1][:6] == ["total", "all", "1", "1", "1.0000", "false"]
    assert rows[2][0] == "status"


def test_csv_leaves_an_unrecorded_figure_empty_rather_than_zero() -> None:
    rows = csv_rows(rollup([_run("a", costs=(1.0,))]))
    header = rows[0]
    total = dict(zip(header, rows[1], strict=True))
    assert total["tokens"] == ""
    assert total["duration"] == ""


def test_csv_marks_a_partially_priced_bucket() -> None:
    rows = csv_rows(rollup([_run("a", costs=(1.0,), pricing={"complete": False})]))
    assert dict(zip(rows[0], rows[1], strict=True))["cost_partial"] == "true"


def test_csv_neutralises_a_label_a_spreadsheet_would_execute() -> None:
    run = _ForeignRun(status='=HYPERLINK("http://evil","click")', total_cost=0.1)
    rows = csv_rows(rollup([run]))
    assert rows[2][1].startswith("'=HYPERLINK")


def test_csv_carries_the_unreadable_count() -> None:
    rows = csv_rows(rollup([_ForeignRun(total_cost="free"), _run("a")]))
    assert rows[-1][0] == "unreadable"
    assert rows[-1][2] == "1"
    assert all(len(row) == len(rows[0]) for row in rows)
