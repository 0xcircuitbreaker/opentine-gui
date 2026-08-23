"""Run/step inspector rendering and the run-diff view — all headless, no DPG.

Every line here is assembled from artifact-controlled data and rendered into a
flat text panel, so two properties are asserted over and over: the line says
something true about the artifact (a cost the file did not record is not
printed as `$0.0000`), and no field an artifact controls can open a row of its
own beside the console's trust verdicts.

The renderers moved out of `app.py` into `opentine_gui.inspectors`, so they are
imported from the module that defines them; only the two tests that check what
the widgets were actually told go through the console.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.inspectors import (
    _budget_breach_line,
    _budget_line,
    _cost_attribution_lines,
    _cost_cell,
    _cost_text,
    _dag_summary,
    _fork_lineage_lines,
    _format_run_diff,
    _format_version_line,
    _load_problem_header,
    _pricing_incompleteness,
    _pricing_line,
    _run_detail_lines,
    _run_list_summary,
    _split_load_problems,
    _step_detail_lines,
    _trust_lines,
)
from opentine_gui.text import _format_compact, _oneline
from opentine_gui.trust import HMAC_KEY_PREF, load_trust_config, signature_line


def _run(run_id: str = "abc", **fields) -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.model,
            inputs={"text": "ask"},
            outputs={"text": "answer"},
            model_info="claude-sonnet-4-6",
            duration=1.5,
            cost=0.01,
            usage={"input": 100, "output": 20, "total": 120},
        )
    )
    graph.add(
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.tool,
            inputs={"name": "search"},
            tool_info={"name": "search"},
            cost=0.002,
        )
    )
    fields.setdefault("status", RunStatus.completed)
    fields.setdefault("model_info", "claude-sonnet-4-6")
    fields.setdefault("user_prompt", "hi")
    return Run(id=run_id, graph=graph, **fields)


def _billed_run(*billings: dict, costs: tuple[float, ...] = (), run_id: str = "billed") -> Run:
    """A chain of model steps carrying the billing records given, and nothing else.

    Model steps are what make a run priceable at all: a run without them sums to
    zero for a reason that says nothing about whether anyone was charged.
    """
    graph = Graph()
    parent = ""
    for index, billing in enumerate(billings, 1):
        step = Step(
            id=f"m{index}",
            parent_ids=[parent] if parent else [],
            kind=StepKind.model,
            inputs={"text": "ask"},
            model_info="claude-sonnet-4-6",
            cost=costs[index - 1] if index <= len(costs) else 0.0,
            billing=dict(billing),
            usage={"input": 100, "output": 20},
        )
        graph.add(step)
        parent = step.id
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="hi")


def _causal_run(*causal_ids: str, run_id: str = "causal") -> Run:
    """The three-step run again, with the tool step additionally naming its causes."""
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(Step(id="s2", parent_ids=["s1"], kind=StepKind.model, inputs={"text": "ask"}))
    graph.add(
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.tool,
            inputs={"name": "search"},
            causal_ids=list(causal_ids),
        )
    )
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="hi")


@dataclass(frozen=True)
class _FutureStep(Step):
    """A step from an opentine newer than the pinned floor, which has `provider`.

    0.7.2's `Step` has no such field — the console reads it because the field
    arrives after 0.7.2 and the artifacts that carry it must not be rendered as
    unattributed. Subclassing the real `Step` keeps the object a `Step` as far
    as `Graph`, `Run.diff` and the inspectors are concerned, which is the whole
    point: the console's own reader is the only thing that sees the new field.
    """

    provider: str = ""


def _rows(lines: list[str]) -> list[str]:
    """The rows a panel actually shows: the widget is handed one joined string,
    so a list element that still contains a newline is two rows on screen."""
    return "\n".join(lines).splitlines()


def _provider_run(provider: str, run_id: str = "served") -> Run:
    graph = Graph()
    graph.add(
        _FutureStep(
            id="s1",
            parent_ids=[],
            kind=StepKind.model,
            inputs={"text": "ask"},
            model_info="claude-sonnet-4-6",
            provider=provider,
        )
    )
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="hi")


# ---- budget ----

def test_budget_line_absent_when_no_budget_set() -> None:
    assert _budget_line(_run()) == ""


def test_budget_line_shows_incurred_against_each_limit() -> None:
    run = _run()
    run.set_budget(max_cost=0.5, max_steps=10, on_breach="stop")
    line = _budget_line(run)
    assert line.startswith("Budget:")
    assert "cost $0.0120/$0.5000" in line
    assert "steps 3/10" in line
    assert "on breach: stop" in line


# Regression guard. Before this was fixed:
# opentine_gui/inspectors.py:173 — _budget_line interpolates _cost_text(run) into an
    # `incurred/limit` pair, and _cost_text returns the prose 'no cost recorded' when
    # nothing was priced. The budget row then reads 'cost no cost recorded/$0.5000',
    # where a number belongs; _cost_cell (which renders '-') keeps the slash pair
    # scannable.
def test_the_budget_row_keeps_a_number_beside_its_limit() -> None:
    # A run of model steps with no billing anywhere: honest as a Cost: line,
    # but the budget row puts it where a figure is read against a limit.
    run = _billed_run({}, {})
    run.set_budget(max_cost=0.5, on_breach="stop")
    line = _budget_line(run)
    assert "$0.5000" in line  # the limit is still reported
    assert "no cost recorded" not in line, f"prose in the numeric slot: {line!r}"


# ---- cost attribution ----

def test_cost_attribution_lists_multiple_spenders_highest_first() -> None:
    lines = _cost_attribution_lines(_run())
    by_model = next(x for x in lines if x.startswith("Cost by model:"))
    by_kind = next(x for x in lines if x.startswith("Cost by kind:"))
    assert "claude-sonnet-4-6 $0.0100" in by_model
    # The tool step carries no model, so its spend is explicitly unattributed.
    assert "(unattributed) $0.0020" in by_model
    assert by_kind.index("model") < by_kind.index("tool")  # descending by cost


def test_cost_attribution_silent_when_one_spender() -> None:
    graph = Graph()
    graph.add(
        Step(id="only", parent_ids=[], kind=StepKind.model, inputs={}, cost=0.01,
             model_info="m")
    )
    run = Run(id="single", graph=graph, status=RunStatus.completed, model_info="m",
              user_prompt="p")
    # One model and one kind add nothing the Cost line does not already say.
    assert _cost_attribution_lines(run) == []


# ---- cost honesty ----

def test_cost_is_plain_when_pricing_is_absent_or_complete() -> None:
    run = _run()
    assert _cost_text(run) == "$0.0120"
    assert _cost_cell(run) == "$0.0120"
    assert _pricing_line(run) == ""
    run.manifest["pricing"] = {"complete": True}
    assert _cost_text(run) == "$0.0120"


def test_a_natively_priced_run_shows_the_number_it_recorded() -> None:
    run = _billed_run({"status": "complete", "amount_usd": "0.0100"}, costs=(0.01,))
    assert _cost_text(run) == "$0.0100"
    assert _cost_cell(run) == "$0.0100"
    assert _pricing_line(run) == ""
    assert "no recorded cost" not in _run_list_summary([run], [run], "")


def test_incomplete_pricing_marks_cost_as_a_lower_bound() -> None:
    run = _run()
    run.manifest["pricing"] = {
        "complete": False,
        "invocations": [{"status": "complete"}, {"status": "unknown"}],
    }
    # An understated total is worse than no total: say it is a floor.
    assert _cost_text(run) == ">=$0.0120"
    assert _cost_cell(run) == ">=$0.0120"
    assert "1 of 2 invocation(s) unpriced" in _pricing_line(run)
    assert ">=" in _run_list_summary([run], [run], "")
    assert "partially priced" in _run_list_summary([run], [run], "")


def test_incomplete_pricing_without_counts_still_warns() -> None:
    run = _run()
    run.manifest["pricing"] = {"complete": False}
    assert _pricing_line(run) == "Pricing: incomplete (cost is a lower bound)"


def test_a_run_nothing_ever_priced_says_so_instead_of_zero() -> None:
    # The shape an importer writes: model calls, usage, and no billing record
    # anywhere. Summing that to $0.0000 states a spend the artifact never
    # claimed, and it is indistinguishable from a run that really was free.
    run = _billed_run({}, {})
    assert _cost_text(run) == "no cost recorded"
    assert _cost_cell(run) == "-"
    assert "nothing priced at capture" in _pricing_line(run)
    assert "Run > Price this run" in _pricing_line(run)
    assert "1 with no recorded cost" in _run_list_summary([run], [run], "")


def test_a_billed_run_that_genuinely_cost_nothing_still_shows_zero() -> None:
    # An unmetered local model is priced *and* free. "no cost recorded" would be
    # the wrong claim about it, because this one was recorded.
    run = _billed_run({"status": "unmetered", "amount_usd": "0"})
    assert _cost_text(run) == "$0.0000"
    assert _cost_cell(run) == "$0.0000"
    assert _pricing_line(run) == ""


def test_the_unpriced_caveat_reaches_the_run_inspector() -> None:
    lines = _run_detail_lines(_billed_run({}))
    assert "Cost: no cost recorded" in lines
    assert any(x.startswith("Pricing: nothing priced at capture") for x in lines)


@pytest.mark.parametrize(
    "pricing",
    ["a string", [1, 2], {"complete": False, "invocations": 7},
     {"complete": "no"}, {"invocations": [{"status": "unknown"}]}, None],
)
def test_pricing_manifest_shapes_never_raise(pricing: object) -> None:
    # Nothing validates manifest.pricing, and this runs inside the run-table
    # render: an exception here would blank the whole list on auto-refresh.
    run = _run()
    run.manifest["pricing"] = pricing
    incomplete, unpriced, total = _pricing_incompleteness(run)
    assert isinstance(incomplete, bool)
    assert isinstance(unpriced, int) and isinstance(total, int)
    assert isinstance(_cost_text(run), str) and isinstance(_cost_cell(run), str)


def test_diff_marks_each_side_independently() -> None:
    left, right = _run("a"), _run("b")
    right.manifest["pricing"] = {"complete": False, "invocations": [{"status": "unknown"}]}
    line = next(x for x in _format_run_diff(left, right).splitlines() if x.startswith("Cost:"))
    assert line == "Cost: $0.0120 -> >=$0.0120"


# ---- provider ----

def test_step_inspector_names_who_served_the_call() -> None:
    lines = _step_detail_lines(_provider_run("anthropic").steps[0])
    # Beside the model it served: "claude-sonnet-4-6" alone does not say whether
    # the call went to Anthropic, Bedrock or Vertex, and the bill differs.
    assert lines.index("Provider: anthropic") == lines.index("Model: claude-sonnet-4-6") + 1


def test_provider_is_recovered_from_a_pinned_release_billing_record() -> None:
    # 0.7.2 has no `Step.provider` at all, but its adapter wrote the provider
    # into the billing calculation. Reading only the field would report every
    # artifact the supported release writes as unattributed.
    step = _billed_run({"calculation": {"provider": "anthropic"}}).steps[0]
    assert "Provider: anthropic" in _step_detail_lines(step)


def test_provider_is_recovered_from_the_rate_card_id() -> None:
    # An exporter that dropped the calculation still round-trips the card id,
    # and the card id is prefixed with the provider whose card it is.
    step = _billed_run({"rate_card_id": "openai:gpt-5:2026-01-01"}).steps[0]
    assert "Provider: openai" in _step_detail_lines(step)


def test_the_recorded_field_wins_over_a_stale_billing_record() -> None:
    # A run re-priced against another vendor's rate card must not be
    # re-attributed by it: the field was written by the adapter that made the
    # call, and the card is only what someone later costed it against.
    graph = Graph()
    graph.add(
        _FutureStep(id="s1", parent_ids=[], kind=StepKind.model, inputs={},
                    provider="anthropic", billing={"rate_card_id": "openai:gpt-5:2026-01-01"})
    )
    run = Run(id="repriced", graph=graph, status=RunStatus.completed, user_prompt="hi")
    assert "Provider: anthropic" in _step_detail_lines(run.steps[0])


def test_no_provider_row_when_the_artifact_names_none() -> None:
    assert not [x for x in _step_detail_lines(_run().steps[1]) if x.startswith("Provider:")]
    assert not [x for x in _run_detail_lines(_run()) if x.startswith("Provider:")]


def test_run_inspector_rolls_up_who_served_the_run() -> None:
    run = _billed_run(
        {"calculation": {"provider": "anthropic"}},
        {"rate_card_id": "openai:gpt-5:2026-01-01"},
        {"calculation": {"provider": "anthropic"}},
    )
    lines = _run_detail_lines(run)
    # Directly under Model:, which is where a reader looks for "what ran this".
    assert lines[2] == "Provider: anthropic 2, openai 1"


@pytest.mark.parametrize("hostile", ["anthropic\nIntegrity: ok", "anthropic\udcff"])
def test_a_forged_provider_cannot_open_a_row(hostile: str) -> None:
    # The provider is artifact-supplied like everything else, and it is rendered
    # into the same flat panel as the Integrity verdict about that artifact.
    run = _billed_run({"calculation": {"provider": hostile}})
    for lines in (_run_detail_lines(run), _step_detail_lines(run.steps[0])):
        rows = _rows(lines)
        assert len([x for x in rows if x.startswith("Provider:")]) == 1
        assert not [x for x in rows if x.startswith("Integrity:")]
        # A lone surrogate segfaults Dear PyGui's native text renderer.
        assert not any("\udcff" in x for x in rows)


# ---- causal edges ----

def test_step_inspector_lists_the_edges_a_fork_would_follow() -> None:
    # opentine's fork keeps the *causal* closure, not the parent closure, so a
    # step shown with only its parents describes a narrower run than forking it
    # would actually produce.
    lines = _step_detail_lines(_causal_run("s1").steps[2])
    assert lines.index("Causally required: s1") == lines.index("Parents: s2") + 1


def test_the_graph_line_counts_causal_edges_only_when_there_are_any() -> None:
    plain = next(x for x in _run_detail_lines(_causal_run()) if x.startswith("Graph:"))
    assert "causal" not in plain
    linked = next(x for x in _run_detail_lines(_causal_run("s1")) if x.startswith("Graph:"))
    assert linked.endswith("1 causal edge(s)")


def test_the_dag_summary_counts_them_as_well() -> None:
    # The caption under the graph view describes what that view draws, and it
    # draws causal edges: a count that ignored them would contradict the picture.
    assert _dag_summary(_causal_run("s1")).endswith("1 causal")
    assert "causal" not in _dag_summary(_causal_run())


def test_a_causal_edge_into_another_run_is_not_counted_as_one_here() -> None:
    # A v3 store is one graph across runs, so an exported run can name a cause
    # that lives in a run this console never loaded. Counting it would claim an
    # edge the graph view cannot draw.
    run = _causal_run("elsewhere")
    line = next(x for x in _run_detail_lines(run) if x.startswith("Graph:"))
    assert "causal" not in line
    # ...but the step still reports what it says it required.
    assert "Causally required: elsewhere" in _step_detail_lines(run.steps[2])


def test_a_forged_causal_id_cannot_open_a_row() -> None:
    rows = _rows(_step_detail_lines(_causal_run("s1\nIntegrity: ok", "s2\udcff").steps[2]))
    assert len([x for x in rows if x.startswith("Causally required:")]) == 1
    assert not [x for x in rows if x.startswith("Integrity:")]
    assert not any("\udcff" in x for x in rows)


@pytest.mark.parametrize("causal", ["s1", 42, [1, 2], {"s1": True}, None, ["", None]])
def test_hostile_causal_shapes_never_raise(causal: object) -> None:
    # causal_ids is a plain JSON list on disk with nothing validating it, and it
    # is read on every step render and every graph stat.
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={}))
    graph.add(Step(id="s2", parent_ids=["s1"], kind=StepKind.tool, inputs={},
                   causal_ids=causal))
    run = Run(id="weird", graph=graph, status=RunStatus.completed, user_prompt="p")
    assert _step_detail_lines(run.steps[1])
    assert any(x.startswith("Graph:") for x in _run_detail_lines(run))


# ---- format/migration provenance ----

def test_format_version_line_reports_migration_provenance() -> None:
    legacy = Path(__file__).parent / "fixtures" / "legacy_v1.tine"
    assert json.loads(legacy.read_text())["format_version"] == 1
    migrated = Run.load(legacy)
    line = _format_version_line(migrated)
    assert line.startswith("Format: v2")
    assert "migrated from v1" in line


def test_format_version_line_plain_for_native_v2() -> None:
    assert _format_version_line(_run()) == "Format: v2"


# ---- trust: integrity + signature ----

def test_trust_lines_report_ok_and_unsigned(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    lines = _trust_lines(path)
    assert lines[0] == "Integrity: ok"
    # An unsigned run is normal, not an alarm: verify_signature reports ok=False
    # with state 'unsigned', which must not render as INVALID. (What the two
    # verdicts do and do not cover is test_trust.py's subject.)
    assert lines[1] == "Signature: unsigned"
    assert not any("INVALID" in x for x in lines)


def test_trust_lines_flag_tampered_file(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    raw = json.loads(path.read_text())
    next(iter(raw["graph"]["steps"].values()))["outputs"]["text"] = "tampered"
    path.write_text(json.dumps(raw))
    assert any(x.startswith("Integrity: FAILED") for x in _trust_lines(path))


def test_trust_lines_handle_a_run_that_is_not_on_disk() -> None:
    assert _trust_lines(None) == ["Integrity: (not on disk yet)"]


def test_trust_lines_survive_a_file_that_vanished(tmp_path: Path) -> None:
    # The scan and the render are not the same instant: a run can be deleted (or
    # its directory unmounted) in between, and the panel must degrade to a line.
    lines = _trust_lines(tmp_path / "gone.tine")
    assert len(lines) == 1 and lines[0].startswith("Integrity: unreadable")


def test_a_key_that_could_not_be_loaded_is_reported_beside_the_verdicts(tmp_path: Path) -> None:
    # Without this row a mistyped key path is invisible: every artifact keeps
    # reporting "no key", which reads as a problem with the artifacts.
    path = tmp_path / "abc.tine"
    _run().save(path)
    config = load_trust_config({HMAC_KEY_PREF: str(tmp_path / "missing.key")})
    assert config.problem and not config.configured
    rows = _rows(_trust_lines(path, config=config))
    assert len([x for x in rows if x.startswith("Signing key:")]) == 1
    # ...and it names the path that failed: a row that merely exists leaves the
    # mistyped path exactly as invisible as no row at all.
    row = next(x for x in rows if x.startswith("Signing key:"))
    assert "missing.key" in row and "no such file" in row


def test_a_key_path_cannot_forge_a_verdict_either(tmp_path: Path) -> None:
    # The path comes out of a preferences file, which is JSON in the user's
    # config directory that anything on the machine can write.
    path = tmp_path / "abc.tine"
    _run().save(path)
    config = load_trust_config({HMAC_KEY_PREF: "/keys/hmac\nIntegrity: FAILED - digest mismatch"})
    rows = _rows(_trust_lines(path, config=config))
    assert [x for x in rows if x.startswith("Integrity:")] == ["Integrity: ok"]


def test_the_console_verifies_the_file_a_run_was_loaded_from(gui_factory, tmp_path: Path) -> None:
    _run().save(tmp_path / "abc.tine")
    gui = gui_factory(tmp_path)
    assert gui._trust_lines(_run())[0] == "Integrity: ok"
    # A run the console never loaded has no file behind it, and must not borrow
    # the verdict of one that does.
    assert gui._trust_lines(_run("never-loaded")) == ["Integrity: (not on disk yet)"]


# ---- fork lineage (opentine 0.4.0 fork identity) ----

def test_fork_lineage_absent_for_a_root_run() -> None:
    assert _fork_lineage_lines(_run()) == []


def test_fork_lineage_shows_origin_branch_and_act() -> None:
    forked = _run("base").fork("s1")
    lines = _fork_lineage_lines(forked)
    assert lines[0] == "Forked from: base at step s1"
    # 0.4.0 puts a random nonce in the id, so sibling forks of the same point are
    # different runs; the console has to say which kind of act this was.
    assert "Fork: branch main, unique act" in lines


def test_fork_lineage_marks_a_reproducible_fork() -> None:
    forked = _run("base").fork("s1", nonce="")
    assert any("reproducible" in x for x in _fork_lineage_lines(forked))


def test_fork_lineage_reports_a_non_default_branch() -> None:
    forked = _run("base").fork("s1", branch="experiment")
    assert any("branch experiment" in x for x in _fork_lineage_lines(forked))


def test_sibling_forks_are_distinguishable(tmp_path: Path) -> None:
    # The behaviour the display exists for: before 0.4.0 these collided and the
    # second save destroyed the first.
    base = _run("base")
    a, b = base.fork("s1"), base.fork("s1")
    assert a.id != b.id
    assert _fork_lineage_lines(a)[0] == _fork_lineage_lines(b)[0]  # same origin line
    a.save(tmp_path / f"{a.id}.tine")
    b.save(tmp_path / f"{b.id}.tine")
    assert len(list(tmp_path.glob("*.tine"))) == 2


def test_fork_lineage_survives_a_pre_040_artifact() -> None:
    # Legacy forks carry forked_from/fork_point but no metadata.fork.
    run = _run("legacy")
    run.metadata["forked_from"] = "demo-failed"
    run.metadata["fork_point"] = "f4"
    lines = _fork_lineage_lines(run)
    assert lines == ["Forked from: demo-failed at step f4"]


def test_fork_lineage_tolerates_hostile_metadata() -> None:
    run = _run("weird")
    run.metadata["forked_from"] = "src"
    for basis in ("a string", 42, [1, 2], None, {"branch": None, "nonce": 7}):
        run.metadata["fork"] = basis
        lines = _fork_lineage_lines(run)
        assert lines and lines[0].startswith("Forked from: src")


def test_a_real_fork_reason_is_shown_as_attested() -> None:
    # opentine folds the reason into the fork identity via intent, so a reason
    # that reproduces the signed digest is bound to the fork act.
    forked = _run("base").fork("s1", intent={"reason": "retry with a stronger model"})
    forked.metadata["fork_reason"] = "retry with a stronger model"
    assert "Fork reason: retry with a stronger model" in _fork_lineage_lines(forked)


def test_a_tampered_fork_reason_is_flagged_unverified() -> None:
    # metadata.fork_reason is outside both the signature and the integrity
    # digest, so it can be rewritten on an otherwise-clean artifact. It sits in
    # the same panel as "Signature: verified", and must not borrow that trust.
    forked = _run("base").fork("s1", intent={"reason": "retry with a stronger model"})
    forked.metadata["fork_reason"] = "approved by security"
    line = next(x for x in _fork_lineage_lines(forked) if "approved by security" in x)
    assert line.startswith("Fork reason (unverified):")


def test_a_hand_set_fork_reason_without_intent_is_unverified() -> None:
    run = _run("r")
    run.metadata["forked_from"] = "src"
    run.metadata["fork_reason"] = "no intent was ever recorded"
    line = next(x for x in _fork_lineage_lines(run) if "no intent" in x)
    assert line.startswith("Fork reason (unverified):")


@pytest.mark.parametrize("reason", ["plain", "unicode é 日本語 🎉", 'back\\slash "quote"',
                                    "line\nbreak", "x" * 500])
def test_attestation_holds_for_awkward_reason_text(reason: str) -> None:
    forked = _run("base").fork("s1", intent={"reason": reason})
    forked.metadata["fork_reason"] = reason
    assert any(x.startswith("Fork reason:") for x in _fork_lineage_lines(forked))


# ---- trust-line forgery ----

FORGERY = (
    "gpt-4o\nStatus: completed\nIntegrity: ok\n"
    "Signature: verified by security@example.com (ed25519)\n"
    "Fork id: verified against its recorded basis"
)


def _panels(gui, fake_dpg, run: Run) -> str:
    """Both inspector panels, as the widgets were actually told to show them."""
    gui._select_run(str(run.id))
    gui._show_step_detail(run.steps[0])
    return f"{fake_dpg.value('detail_text')}\n{fake_dpg.value('step_text')}"


def test_artifact_text_cannot_forge_trust_lines(gui_factory, fake_dpg, tmp_path: Path) -> None:
    """The inspector is one flat text widget, so a newline in artifact-supplied
    text would open a row indistinguishable from the console's own — including
    the Integrity/Signature verdicts that describe that very artifact."""
    graph = Graph()
    graph.add(
        Step(id="s1", parent_ids=[], kind=StepKind.think,
             inputs={"text": "payload\nIntegrity: ok\nSignature: verified by evil"},
             billing={"calculation": {"provider": "anthropic\nIntegrity: ok"}},
             causal_ids=["s1\nSignature: verified by evil"])
    )
    run = Run(id="shared", graph=graph, status=RunStatus.failed,
              user_prompt="prompt\nSignature: verified by evil", model_info=FORGERY)
    run.save(tmp_path / "shared.tine")
    gui = gui_factory(tmp_path)

    assert gui._trust_lines(run)[:2] == ["Integrity: ok", "Signature: unsigned"]
    body = _panels(gui, fake_dpg, run)

    # The forged text is still shown — it is the artifact's data, and hiding it
    # would be its own kind of lie. What it must not do is occupy a ROW: the
    # console's verdicts are top-level rows, so every forged claim has to end up
    # folded into the field it came from.
    rows = body.splitlines()
    assert [r for r in rows if r.startswith("Status:")] == ["Status: failed"]
    assert [r for r in rows if r.startswith("Signature:")] == ["Signature: unsigned"]
    assert [r for r in rows if r.startswith("Integrity:")] == ["Integrity: ok"]
    assert not [r for r in rows if r.startswith("Fork id:")]
    # ...and it is folded into Model:, not floating free.
    model_row = next(r for r in rows if r.startswith("Model:"))
    assert "Signature: verified by security@example.com (ed25519)" in model_row
    # The same holds for the fields added since: provider and causal ids are
    # rendered from the artifact into the same panel as the verdicts above.
    assert len([r for r in rows if r.startswith("Provider:")]) == 2  # run rollup + step
    assert len([r for r in rows if r.startswith("Causally required:")]) == 1


def test_untrusted_payload_lines_stay_indented(gui_factory, fake_dpg, tmp_path: Path) -> None:
    # Step payloads render under Inputs:/Outputs: headings; every line they
    # produce must be indented so none can pose as a top-level field.
    graph = Graph()
    graph.add(
        Step(id="s1", parent_ids=[], kind=StepKind.think,
             inputs={"text": "a\nIntegrity: ok\nb", "k\nInjected: yes": "v"})
    )
    run = Run(id="r", graph=graph, status=RunStatus.completed, user_prompt="p")
    run.save(tmp_path / "r.tine")
    rows = _panels(gui_factory(tmp_path), fake_dpg, run).splitlines()
    # The payload says exactly what the console's own verdict says, which is the
    # point: the only thing separating them is the indent, so the trust block
    # must remain the single unindented Integrity row on the panel.
    assert rows.count("Integrity: ok") == 1
    for row in rows:
        if "Injected: yes" in row or row.strip() in {"a", "b"}:
            assert row.startswith(" "), f"payload row not indented: {row!r}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("a\nb", "a b"),
        ("a\r\nb", "a b"),
        ("a b", "a b"),      # LINE SEPARATOR
        ("a\x85b", "a b"),        # NEL
        ("a\tb", "a b"),
        ("a\x00b", "ab"),         # C0 control dropped
        ("  padded  ", "padded"),
    ],
)
def test_oneline_collapses_every_line_break_form(raw: str, expected: str) -> None:
    assert _oneline(raw) == expected


# ---- budget breach ----

def test_no_breach_line_for_a_healthy_run() -> None:
    assert _budget_breach_line(_run()) == ""


def test_budget_breach_names_the_dimension_and_the_overage() -> None:
    # The most common non-obvious reason an agent run dies. Without this the
    # console shows "Status: failed" and the user hunts for a crash.
    run = _run()
    run.metadata["budget_state"] = {
        "breached": True, "dimension": "cost", "incurred": 0.75, "limit": 0.5,
    }
    assert _budget_breach_line(run) == "Budget BREACHED: cost 0.75 > 0.5"


def test_budget_breach_degrades_without_numbers() -> None:
    run = _run()
    run.metadata["budget_state"] = {"breached": True, "dimension": "steps"}
    assert _budget_breach_line(run) == "Budget BREACHED: steps"


@pytest.mark.parametrize(
    "state",
    ["a string", 42, [1], None, {"breached": False}, {}, {"breached": True}],
)
def test_budget_state_shapes_never_raise(state: object) -> None:
    # metadata is untrusted and outside the integrity digest.
    run = _run()
    run.metadata["budget_state"] = state
    assert isinstance(_budget_breach_line(run), str)


# ---- fork provenance verification ----

def test_fork_id_is_reported_as_verified_for_a_real_fork() -> None:
    forked = _run("base").fork("s1")
    assert "Fork id: verified against its recorded basis" in _fork_lineage_lines(forked)


def test_an_edited_fork_record_is_flagged() -> None:
    # metadata sits outside the integrity digest, so this still verifies "ok";
    # the fork-id check is the only thing that catches it.
    forked = _run("base").fork("s1")
    forked.metadata["fork"]["branch"] = "not-the-real-branch"
    lines = _fork_lineage_lines(forked)
    assert any("DOES NOT MATCH" in x for x in lines)


def test_lineage_falls_back_to_the_fork_record_when_forked_from_is_stripped() -> None:
    forked = _run("base").fork("s1")
    del forked.metadata["forked_from"]
    lines = _fork_lineage_lines(forked)
    assert lines and lines[0].startswith("Forked from: base")


# ---- signature rendering ----

@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ({"ok": True, "state": "verified", "signer": "alice", "algorithm": "ed25519"},
         "Signature: verified by alice (ed25519)"),
        ({"ok": False, "state": "unsigned", "reason": "no signature present"},
         "Signature: unsigned"),
        # ok=False but NOT an alarm: the file is signed, we simply hold no key.
        ({"ok": False, "state": "no-key", "signer": "alice",
          "reason": "HMAC signature present but no key supplied"},
         "Signature: present by alice, not verified here (no key)"),
        ({"ok": False, "state": "mismatch", "signer": "mallory",
          "reason": "signature mismatch"},
         "Signature: INVALID by mallory - signature mismatch"),
        ({"ok": False, "state": "error", "reason": "unsupported signature scheme"},
         "Signature: unsupported signature scheme"),
    ],
)
def test_signature_line_renders_each_state(verdict: dict, expected: str) -> None:
    assert signature_line(verdict) == expected


def test_only_a_real_mismatch_reads_as_an_alarm() -> None:
    for state in ("unsigned", "no-key", "verified"):
        line = signature_line({"ok": state == "verified", "state": state, "reason": "r"})
        assert "INVALID" not in line, f"{state} must not alarm the user"


def test_signature_line_survives_an_unknown_state() -> None:
    assert "weird" in signature_line({"ok": False, "state": "weird", "reason": ""})


# ---- load-problem classification ----

def test_fatal_load_errors_sort_before_warnings() -> None:
    problems = [
        "a.tine: integrity digest mismatch",
        "b.tine: Expecting value: line 1 column 1",
        "c.tine: duplicate run id 'x', shadowed by d.tine",
    ]
    fatal, warnings = _split_load_problems(problems)
    # A run that still loaded must not push a run that did not out of view.
    assert fatal == ["b.tine: Expecting value: line 1 column 1"]
    assert len(warnings) == 2


def test_load_problem_header_counts_each_kind() -> None:
    assert _load_problem_header(2, 0) == "2 load error(s)"
    assert _load_problem_header(0, 3) == "3 warning(s)"
    assert _load_problem_header(1, 1) == "1 load error(s) / 1 warning(s)"


# ---- run diff ----

def test_run_diff_reports_ancestor_divergence_and_field_deltas() -> None:
    # Re-running the model step differently: opentine pairs the same-kind steps
    # that follow the fork point, so this surfaces as a field-level change.
    left = _run("base")
    right = left.fork("s1")
    right.add_step(
        StepKind.model,
        {"text": "alternative"},
        outputs={"text": "other answer"},
        cost=0.05,
        model_info="claude-opus-5",
    )
    text = _format_run_diff(left, right)

    assert "Common ancestor: s1" in text
    assert "Only in A (1):" in text  # the tool step the fork never reached
    assert "s3" in text
    assert "Changed (1):" in text
    # Field-level deltas the user needs when comparing two attempts.
    assert "model_info" in text
    assert "claude-sonnet-4-6" in text and "claude-opus-5" in text
    assert "cost" in text
    # Values render on one line so the diff stays scannable.
    assert '{"text": "alternative"}' in text


def test_run_diff_lists_differing_kinds_as_added_and_removed() -> None:
    # A step of a different kind is not paired with one it cannot correspond to;
    # it must show up on both sides rather than as a confusing field change.
    left = _run("base")
    right = left.fork("s2")
    right.add_step(StepKind.think, {"text": "reconsider"})
    text = _format_run_diff(left, right)
    assert "Only in A (1):" in text  # the tool step
    assert "Only in B (1):" in text  # the new think step
    assert "Changed (0):" in text


def test_run_diff_of_identical_runs_is_empty() -> None:
    run = _run("same")
    text = _format_run_diff(run, run)
    assert "Only in A (0):" in text
    assert "Only in B (0):" in text
    assert "Changed (0):" in text


def test_run_diff_of_unrelated_runs_says_so() -> None:
    # Two runs that share no step id at all. (_run("one") vs _run("two") does
    # NOT test this: those are step-for-step identical, so opentine reports s3
    # as their ancestor and the "unrelated" branch never renders.)
    def unrelated(run_id: str, prefix: str) -> Run:
        graph = Graph()
        graph.add(Step(id=f"{prefix}1", parent_ids=[], kind=StepKind.think, inputs={"t": "a"}))
        graph.add(
            Step(id=f"{prefix}2", parent_ids=[f"{prefix}1"], kind=StepKind.model, inputs={"t": "b"})
        )
        return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="p")

    text = _format_run_diff(unrelated("one", "x"), unrelated("two", "y"))
    # Naming a step as the ancestor of two runs that share none would invent a
    # relationship, so the absence has to be stated rather than left blank.
    assert "Common ancestor: (none - unrelated runs)" in text.splitlines()
    # ...and a real shared ancestor is still named, so the line is not a constant.
    assert "Common ancestor: s3" in _format_run_diff(_run("one"), _run("two")).splitlines()


def test_run_diff_truncates_huge_divergence() -> None:
    left = _run("big")
    right = _run("big2")
    for i in range(60):
        right.add_step(StepKind.think, {"text": f"extra {i}"})
    text = _format_run_diff(right, left, max_steps=5)
    rows = text.splitlines()
    # 60 added think steps are only in A: max_steps of them are listed and the
    # rest are counted, so the panel cannot be blown out by a runaway run.
    start = rows.index("Only in A (60):")
    listed = rows[start + 1 : start + 6]
    assert [r.split("  ")[-1] for r in listed] == [f"think: extra {i}" for i in range(5)]
    # The remainder has to be the real one: a wrong count is worse than none,
    # because the user reads it as "how much am I not seeing".
    assert rows[start + 6] == "  ...and 55 more"


def test_run_diff_reports_a_provider_change_opentine_cannot_see() -> None:
    left, right = _provider_run("anthropic", "a"), _provider_run("openai", "b")
    # The reason the section exists: `provider` is in neither Run.diff's field
    # list nor the step id hash, so opentine's own verdict on two runs served by
    # different vendors is that nothing changed at all.
    assert left.diff(right).changed == []
    text = _format_run_diff(left, right)
    assert "Also differs (fields opentine's diff does not compare):" in text
    assert "provider: anthropic -> openai" in text


def test_run_diff_reports_changed_causal_edges() -> None:
    left, right = _causal_run(run_id="a"), _causal_run("s1", run_id="b")
    assert left.diff(right).changed == []
    text = _format_run_diff(left, right)
    assert "causal edges: 0 -> 1" in text
    assert "causal edges in the graph: 0 -> 1" in text


# Regression guard. Before this was fixed:
# opentine_gui/inspectors.py:664-666 — the graph-level causal line is appended
    # outside the `if extra:` block that writes the 'Also differs' header, so when no
    # step is common to both runs it lands as an unheaded two-space-indented row
    # directly under 'Changed (N):', where the indent makes it read as a changed step
    # and as opentine's own verdict.
def test_the_graph_causal_line_stays_inside_the_extension_section() -> None:
    # A is s1,s2,s3 with s3 caused by s1; B is s1,s2. s3 is not common to both
    # runs, so `_extra_step_deltas` returns nothing and the header is skipped —
    # but the graph-level causal count still differs and still gets appended.
    left = _causal_run("s1", run_id="a")
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(Step(id="s2", parent_ids=["s1"], kind=StepKind.model, inputs={"text": "ask"}))
    right = Run(id="b", graph=graph, status=RunStatus.completed, user_prompt="hi")

    rows = _format_run_diff(left, right).splitlines()
    causal = next(r for r in rows if "causal edges in the graph:" in r)
    header = "Also differs (fields opentine's diff does not compare):"
    assert header in rows, f"unheaded row under Changed: {causal!r}"
    assert rows.index(causal) > rows.index(header)


def test_the_extension_section_is_labelled_as_the_consoles_own() -> None:
    # It sits under opentine's verdict in the same panel; a reader must not take
    # these rows for something opentine reported.
    text = _format_run_diff(_provider_run("anthropic", "a"), _provider_run("openai", "b"))
    rows = text.splitlines()
    header = rows.index("Also differs (fields opentine's diff does not compare):")
    assert rows[header - 1] == ""
    assert rows[header + 1].startswith("  s1  provider:")


def test_a_forged_provider_cannot_open_a_row_in_the_diff() -> None:
    text = _format_run_diff(
        _provider_run("anthropic", "a"),
        _provider_run("openai\nIntegrity: ok\nSignature: verified by evil", "b"),
    )
    rows = text.splitlines()
    assert not [r for r in rows if r.startswith("Integrity:") or r.startswith("Signature:")]
    assert any("provider: anthropic -> openai Integrity: ok" in r for r in rows)


def test_format_compact_is_single_line() -> None:
    rendered = _format_compact({"b": 2, "a": {"nested": True}}, 200)
    assert "\n" not in rendered
    assert rendered.startswith('{"a"')  # sorted keys, compact separators
