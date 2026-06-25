"""Smoke tests for run loading and path safety — headless, no DPG."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.app import (
    MAX_TINE_BYTES,
    _format_value,
    _graph_stats,
    _highlight_summary,
    _mapping_lines,
    _matching_steps,
    _node_label,
    _run_matches_filter,
    _safe_run_path,
    load_runs,
)


def _run_with_steps(run_id: str, steps: list[Step], **fields) -> Run:
    graph = Graph()
    for step in steps:
        graph.add(step)
    return Run(id=run_id, graph=graph, **fields)


def _make_run(run_id: str, prompt: str = "hi") -> Run:
    steps = [
        Step(
            id="s1",
            parent_ids=[],
            kind=StepKind.think,
            inputs={"text": "plan"},
            duration=0.1,
        ),
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.tool,
            inputs={"name": "search", "arguments": {"q": "x"}},
            outputs={"result": "ok"},
            tool_info={"name": "search"},
            timestamp=0.1,
            duration=0.2,
            cost=0.001,
        ),
    ]
    return _run_with_steps(
        run_id,
        steps,
        status=RunStatus.completed,
        model_info="claude-sonnet-4-6",
        user_prompt=prompt,
    )


def test_load_runs_empty_dir(tmp_path: Path) -> None:
    runs, errors, sig = load_runs(tmp_path / "missing")
    assert runs == []
    assert errors == []
    assert sig == ()


def test_load_runs_reads_saved(tmp_path: Path) -> None:
    r = _make_run("abc")
    r.save(tmp_path / "abc.tine")
    runs, errors, sig = load_runs(tmp_path)
    assert errors == []
    assert [x.id for x in runs] == ["abc"]
    assert len(runs[0].steps) == 2
    assert len(sig) == 1
    assert sig[0][0] == "abc.tine"


def test_load_runs_sorted_newest_first(tmp_path: Path) -> None:
    _make_run("old").save(tmp_path / "old.tine")
    time.sleep(0.05)
    _make_run("new").save(tmp_path / "new.tine")
    runs, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["new", "old"]


def test_load_runs_reports_corrupt_files(tmp_path: Path) -> None:
    _make_run("good").save(tmp_path / "good.tine")
    (tmp_path / "bad.tine").write_bytes(b"not a valid msgpack")
    runs, errors, sig = load_runs(tmp_path)
    assert [r.id for r in runs] == ["good"]
    assert len(errors) == 1
    assert "bad.tine" in errors[0]
    # signature covers both files (it reflects on-disk state, not load success)
    assert {e[0] for e in sig} == {"good.tine", "bad.tine"}


def test_load_runs_skips_oversized(tmp_path: Path) -> None:
    _make_run("ok").save(tmp_path / "ok.tine")
    big = tmp_path / "huge.tine"
    big.write_bytes(b"\0" * (MAX_TINE_BYTES + 1))
    runs, errors, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["ok"]
    assert any("huge.tine" in e and "skipped" in e for e in errors)


def test_load_runs_signature_matches_what_was_loaded(tmp_path: Path) -> None:
    _make_run("a").save(tmp_path / "a.tine")
    _make_run("b").save(tmp_path / "b.tine")
    _, _, sig1 = load_runs(tmp_path)
    _, _, sig2 = load_runs(tmp_path)
    # Same directory state => identical signatures => no spurious reload loop.
    assert sig1 == sig2


def test_safe_run_path_accepts_normal_id(tmp_path: Path) -> None:
    p = _safe_run_path(tmp_path, "abc123")
    assert p == (tmp_path / "abc123.tine").resolve()


def test_safe_run_path_accepts_hyphen_underscore_dot(tmp_path: Path) -> None:
    p = _safe_run_path(tmp_path, "run_2026-04-15.v1")
    assert p.name == "run_2026-04-15.v1.tine"


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",
        "../../evil",
        "..\\evil",
        "/abs/path",
        "C:\\Windows\\evil",
        "sub/dir",
        "has space",
        "",
        ".hidden",
        "name\x00null",
    ],
)
def test_safe_run_path_rejects_traversal_and_unsafe(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _safe_run_path(tmp_path, bad)


def test_safe_run_path_resolved_stays_inside(tmp_path: Path) -> None:
    # Even if the regex somehow let something through, the resolve check blocks escape.
    # Confirm legitimate ID resolves under runs_dir.
    p = _safe_run_path(tmp_path, "legit")
    assert tmp_path.resolve() in p.parents


def test_run_filter_matches_status_prompt_and_step_payload() -> None:
    run = _make_run("demo-search", prompt="Build a timeline")
    assert _run_matches_filter(run, "completed")
    assert _run_matches_filter(run, "timeline")
    assert _run_matches_filter(run, "search")
    assert not _run_matches_filter(run, "missing")


def test_format_value_pretty_prints_nested_data() -> None:
    rendered = _format_value({"b": 2, "a": {"nested": True}}, 200)
    assert '"a": {' in rendered
    assert '"nested": true' in rendered


def test_mapping_lines_handles_empty_and_nested_values() -> None:
    assert _mapping_lines({}) == ["  (none)"]
    lines = _mapping_lines({"arguments": {"q": "x"}})
    assert lines[0] == "  arguments:"
    assert any('"q": "x"' in line for line in lines)


def test_graph_stats_describe_branched_run() -> None:
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(id="s2", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "search"}),
        Step(
            id="s3",
            parent_ids=["s1"],
            kind=StepKind.model,
            inputs={"text": "other branch"},
            outputs={"text": "summary"},
            model_info="m",
            duration=0.3,
            cost=0.002,
        ),
    ]
    run = _run_with_steps("branched", steps)
    assert _graph_stats(run) == {"roots": 1, "links": 2, "branches": 1, "max_depth": 1}


def test_graph_stats_counts_multi_parent_merge() -> None:
    # s3 merges two parents -> two links into one node, depth 2.
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(id="s2a", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "a"}),
        Step(id="s2b", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "b"}),
        Step(id="s3", parent_ids=["s2a", "s2b"], kind=StepKind.done, inputs={"text": "merged"}),
    ]
    run = _run_with_steps("merge", steps)
    assert _graph_stats(run) == {"roots": 1, "links": 4, "branches": 1, "max_depth": 2}


def test_step_filter_and_labels_support_graph_search() -> None:
    run = _make_run("demo-search")
    assert _matching_steps(run, "search") == ["s2"]
    assert _highlight_summary(run, {"s2"}) == "Matches: tool: search"
    assert _node_label(run.steps[1], highlighted=True) == "* tool: search"

    done = Step(
        id="s3",
        parent_ids=["s2"],
        kind=StepKind.done,
        inputs={},
        outputs={"answer": "final answer"},
    )
    assert _node_label(done) == "done: final answer"

    # done text may live in inputs (opentine's runtime writes it there)
    done_inputs = Step(id="s4", parent_ids=["s3"], kind=StepKind.done, inputs={"text": "all set"})
    assert _node_label(done_inputs) == "done: all set"

    # error steps carry their message in step.error, not inputs
    err = Step(
        id="e1",
        parent_ids=["s2"],
        kind=StepKind.error,
        inputs={},
        error={"type": "ValueError", "message": "boom"},
    )
    assert _node_label(err) == "error: boom"


def test_step_filter_matches_tool_info_and_error() -> None:
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.tool,
            inputs={"arguments": {"q": "x"}},
            tool_info={"name": "web_search"},
        ),
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.error,
            inputs={},
            error={"type": "TimeoutError", "message": "upstream timed out"},
        ),
    ]
    run = _run_with_steps("searchable", steps)
    assert _matching_steps(run, "web_search") == ["s2"]
    assert _matching_steps(run, "timeout") == ["s3"]
    assert _run_matches_filter(run, "upstream timed out")
