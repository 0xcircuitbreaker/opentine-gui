"""Run/step inspector rendering and the run-diff view — all headless, no DPG."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.app import (
    OpentineGUI,
    _budget_breach_line,
    _budget_line,
    _cost_attribution_lines,
    _cost_text,
    _fork_lineage_lines,
    _format_compact,
    _format_run_diff,
    _format_version_line,
    _load_problem_header,
    _pricing_incompleteness,
    _pricing_line,
    _run_list_summary,
    _signature_line,
    _split_load_problems,
)


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


# ---- pricing completeness ----

def test_cost_is_plain_when_pricing_is_absent_or_complete() -> None:
    run = _run()
    assert _cost_text(run) == "$0.0120"
    assert _pricing_line(run) == ""
    run.manifest["pricing"] = {"complete": True}
    assert _cost_text(run) == "$0.0120"


def test_incomplete_pricing_marks_cost_as_a_lower_bound() -> None:
    run = _run()
    run.manifest["pricing"] = {
        "complete": False,
        "invocations": [{"status": "complete"}, {"status": "unknown"}],
    }
    # An understated total is worse than no total: say it is a floor.
    assert _cost_text(run) == ">=$0.0120"
    assert "1 of 2 invocation(s) unpriced" in _pricing_line(run)
    assert ">=" in _run_list_summary([run], [run], "")
    assert "partially priced" in _run_list_summary([run], [run], "")


def test_incomplete_pricing_without_counts_still_warns() -> None:
    run = _run()
    run.manifest["pricing"] = {"complete": False}
    assert _pricing_line(run) == "Pricing: incomplete (cost is a lower bound)"


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


def test_diff_marks_each_side_independently() -> None:
    left, right = _run("a"), _run("b")
    right.manifest["pricing"] = {"complete": False, "invocations": [{"status": "unknown"}]}
    line = next(x for x in _format_run_diff(left, right).splitlines() if x.startswith("Cost:"))
    assert line == "Cost: $0.0120 -> >=$0.0120"


# ---- format/migration provenance ----

def test_format_version_line_reports_migration_provenance(tmp_path: Path) -> None:
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
    run = _run()
    path = tmp_path / "abc.tine"
    run.save(path)
    gui = OpentineGUI(tmp_path)
    gui._run_paths = {"abc": path}
    lines = gui._trust_lines(run)
    assert "Integrity: ok" in lines
    # An unsigned run is normal, not an alarm: verify_signature reports ok=False
    # with state 'unsigned', which must not render as INVALID.
    assert "Signature: unsigned" in lines
    assert not any("INVALID" in x for x in lines)


def test_trust_lines_flag_tampered_file(tmp_path: Path) -> None:
    run = _run()
    path = tmp_path / "abc.tine"
    run.save(path)
    raw = json.loads(path.read_text())
    next(iter(raw["graph"]["steps"].values()))["outputs"]["text"] = "tampered"
    path.write_text(json.dumps(raw))
    gui = OpentineGUI(tmp_path)
    gui._run_paths = {"abc": path}
    assert any(x.startswith("Integrity: FAILED") for x in gui._trust_lines(run))


def test_trust_lines_handle_missing_file(tmp_path: Path) -> None:
    gui = OpentineGUI(tmp_path)
    gui._run_paths = {}
    assert gui._trust_lines(_run()) == ["Integrity: (not on disk yet)"]


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
    assert _signature_line(verdict) == expected


def test_only_a_real_mismatch_reads_as_an_alarm() -> None:
    for state in ("unsigned", "no-key", "verified"):
        line = _signature_line({"ok": state == "verified", "state": state, "reason": "r"})
        assert "INVALID" not in line, f"{state} must not alarm the user"


def test_signature_line_survives_an_unknown_state() -> None:
    assert "weird" in _signature_line({"ok": False, "state": "weird", "reason": ""})


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
    text = _format_run_diff(_run("one"), _run("two"))
    assert "Common ancestor:" in text


def test_run_diff_truncates_huge_divergence() -> None:
    left = _run("big")
    right = _run("big2")
    for i in range(60):
        right.add_step(StepKind.think, {"text": f"extra {i}"})
    text = _format_run_diff(right, left, max_steps=5)
    assert "...and" in text and "more" in text


def test_format_compact_is_single_line() -> None:
    rendered = _format_compact({"b": 2, "a": {"nested": True}}, 200)
    assert "\n" not in rendered
    assert rendered.startswith('{"a"')  # sorted keys, compact separators
