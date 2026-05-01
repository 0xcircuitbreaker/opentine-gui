"""Smoke tests for run loading and path safety — headless, no DPG."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from opentine.core import Run, RunStatus, Step, StepKind

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


def _make_run(run_id: str, prompt: str = "hi") -> Run:
    steps = [
        Step(
            id="s1",
            parent_id=None,
            kind=StepKind.think,
            inputs={"text": "plan"},
            outputs={},
            model_info="",
            timestamp=0.0,
            duration=0.1,
            cost=0.0,
        ),
        Step(
            id="s2",
            parent_id="s1",
            kind=StepKind.tool,
            inputs={"name": "search", "arguments": {"q": "x"}},
            outputs={"result": "ok"},
            model_info="",
            timestamp=0.1,
            duration=0.2,
            cost=0.001,
        ),
    ]
    return Run(
        id=run_id,
        steps=steps,
        status=RunStatus.completed,
        model_info="claude-sonnet-4-6",
        system_prompt="",
        user_prompt=prompt,
        created_at=0.0,
        metadata={},
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
    run = _make_run("branched")
    run.steps.append(
        Step(
            id="s3",
            parent_id="s1",
            kind=StepKind.model,
            inputs={"text": "other branch"},
            outputs={"text": "summary"},
            model_info="m",
            timestamp=0.2,
            duration=0.3,
            cost=0.002,
        )
    )
    assert _graph_stats(run) == {"roots": 1, "links": 2, "branches": 1, "max_depth": 1}


def test_step_filter_and_labels_support_graph_search() -> None:
    run = _make_run("demo-search")
    assert _matching_steps(run, "search") == ["s2"]
    assert _highlight_summary(run, {"s2"}) == "Matches: tool: search"
    assert _node_label(run.steps[1], highlighted=True) == "* tool: search"

    done = Step(
        id="s3",
        parent_id="s2",
        kind=StepKind.done,
        inputs={},
        outputs={"answer": "final answer"},
        model_info="",
        timestamp=0.2,
        duration=0.0,
        cost=0.0,
    )
    assert _node_label(done) == "done: final answer"
