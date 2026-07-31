"""Smoke tests for run loading and path safety — headless, no DPG."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import app
from opentine_gui.app import (
    MAX_TINE_BYTES,
    _format_timestamp,
    _format_value,
    _graph_stats,
    _highlight_summary,
    _mapping_lines,
    _matching_steps,
    _node_label,
    _run_matches_filter,
    _safe_run_path,
    _truncate,
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
    runs, errors, sig, paths = load_runs(tmp_path / "missing")
    assert runs == []
    assert errors == []
    assert sig == ()
    assert paths == {}


def test_load_runs_reads_saved(tmp_path: Path) -> None:
    r = _make_run("abc")
    r.save(tmp_path / "abc.tine")
    runs, errors, sig, paths = load_runs(tmp_path)
    assert errors == []
    assert [x.id for x in runs] == ["abc"]
    assert len(runs[0].steps) == 2
    assert len(sig) == 1
    assert sig[0][0] == "abc.tine"
    assert paths == {"abc": tmp_path / "abc.tine"}


def test_load_runs_sorted_newest_first(tmp_path: Path) -> None:
    _make_run("old").save(tmp_path / "old.tine")
    time.sleep(0.05)
    _make_run("new").save(tmp_path / "new.tine")
    runs, _, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["new", "old"]


def test_load_runs_reports_corrupt_files(tmp_path: Path) -> None:
    _make_run("good").save(tmp_path / "good.tine")
    (tmp_path / "bad.tine").write_bytes(b"not a valid msgpack")
    runs, errors, sig, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["good"]
    assert len(errors) == 1
    assert "bad.tine" in errors[0]
    # signature covers both files (it reflects on-disk state, not load success)
    assert {e[0] for e in sig} == {"good.tine", "bad.tine"}


def test_load_runs_skips_oversized(tmp_path: Path) -> None:
    _make_run("ok").save(tmp_path / "ok.tine")
    big = tmp_path / "huge.tine"
    big.write_bytes(b"\0" * (MAX_TINE_BYTES + 1))
    runs, errors, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["ok"]
    assert any("huge.tine" in e and "skipped" in e for e in errors)


def test_load_runs_signature_matches_what_was_loaded(tmp_path: Path) -> None:
    _make_run("a").save(tmp_path / "a.tine")
    _make_run("b").save(tmp_path / "b.tine")
    _, _, sig1, _ = load_runs(tmp_path)
    _, _, sig2, _ = load_runs(tmp_path)
    # Same directory state => identical signatures => no spurious reload loop.
    assert sig1 == sig2


def test_load_runs_keeps_newest_of_duplicate_ids_and_reports_the_shadowed_file(
    tmp_path: Path,
) -> None:
    _make_run("dup").save(tmp_path / "older-copy.tine")
    time.sleep(0.05)
    _make_run("dup").save(tmp_path / "newer-copy.tine")
    runs, errors, _, paths = load_runs(tmp_path)
    # One row per id: a second row with the same id would be an unselectable
    # duplicate, since selection and every action resolve a run by its id.
    assert [r.id for r in runs] == ["dup"]
    # Actions must target the file selection binds to: the newest by mtime.
    assert paths["dup"] == tmp_path / "newer-copy.tine"
    # The collision is surfaced rather than silently dropped.
    assert any("duplicate run id" in e and "older-copy.tine" in e for e in errors)


def test_load_runs_flags_integrity_mismatch_but_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "tampered.tine"
    _make_run("tampered").save(path)
    raw = json.loads(path.read_text())
    step = next(iter(raw["graph"]["steps"].values()))
    step["outputs"]["result"] = "edited after signing"
    path.write_text(json.dumps(raw))
    runs, errors, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["tampered"]
    assert any("integrity" in e for e in errors)


def test_load_runs_migrates_legacy_v1_fixture(tmp_path: Path) -> None:
    legacy = Path(__file__).parent / "fixtures" / "legacy_v1.tine"
    assert json.loads(legacy.read_text())["format_version"] == 1
    shutil.copy2(legacy, tmp_path / "legacy.tine")
    _make_run("modern").save(tmp_path / "modern.tine")
    runs, errors, _, _ = load_runs(tmp_path)
    assert not any("legacy.tine" in e for e in errors)
    migrated = next(r for r in runs if r.id == "demo-complete")
    assert migrated.format_version == 2
    assert migrated.metadata.get("migration"), "v1 load should record migration provenance"


def test_load_runs_reports_unsupported_future_version(tmp_path: Path) -> None:
    (tmp_path / "future.tine").write_text(json.dumps({"format_version": 3}))
    runs, errors, _, _ = load_runs(tmp_path)
    assert runs == []
    assert any("future.tine" in e for e in errors)


def test_a_v3_repository_is_refused_not_half_opened(tmp_path: Path) -> None:
    # Run.load redirects a repository DIRECTORY to Repo.open(...).load_run('heads/main'),
    # so globbing *.tine used to match the repo's own .tine/ directory and show one
    # run out of many — and Pause would have rewritten the repository's branch.
    from opentine.core import Repo

    work = tmp_path / "work"
    work.mkdir()
    Repo.init(work)
    runs, errors, sig, paths = load_runs(work)
    assert runs == [] and paths == {}
    assert any("v3 repository" in e for e in errors)
    assert sig == ()


def test_a_directory_named_like_a_run_is_skipped(tmp_path: Path) -> None:
    _make_run("real").save(tmp_path / "real.tine")
    (tmp_path / "notarun.tine").mkdir()
    runs, errors, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["real"]
    assert not errors, "a directory is not a corrupt run"


def test_is_v3_repository_detects_both_layouts(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / ".tine").mkdir(parents=True)
    (worktree / ".tine" / "config.json").write_text("{}")
    assert app._is_v3_repository(worktree)
    # ...and the object directory itself, which Run.load also redirects on.
    assert app._is_v3_repository(worktree / ".tine")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not app._is_v3_repository(plain)
    assert not app._is_v3_repository(tmp_path / "missing")


def test_verify_cache_is_effective_for_an_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path)
    app._VERIFY_CACHE.clear()
    calls: list[Path] = []

    def counting(p: Path):
        calls.append(p)
        return Run.verify_integrity(p)

    for _ in range(5):
        app._verify_cached(path, path.stat(), "integrity", counting)
    assert len(calls) == 1, "an unchanged file must be verified once, not per refresh"


def test_verify_cache_detects_a_size_and_mtime_preserving_tamper(tmp_path: Path) -> None:
    # An integrity check exists to catch tampering, and os.utime lets a writer put
    # mtime back after a same-length edit. The cache key must not be fooled by that.
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path)
    before = path.stat()
    app._VERIFY_CACHE.clear()
    assert app._verify_integrity_cached(path, before)["ok"]

    raw = path.read_bytes()
    assert b'"ok"' in raw
    path.write_bytes(raw.replace(b'"ok"', b'"XX"', 1))  # byte-for-byte same length
    os.utime(path, (before.st_atime, before.st_mtime))
    after = path.stat()
    assert after.st_size == before.st_size and after.st_mtime == before.st_mtime

    assert not Run.verify_integrity(path).ok, "precondition: the file really is tampered"
    assert not app._verify_integrity_cached(path, after)["ok"], "stale verdict served"


def test_search_cache_invalidates_when_status_changes_in_place(tmp_path: Path) -> None:
    # Run search text is cached per Run for typing responsiveness, but pause()
    # mutates status on the same object — the cache must not keep saying "running".
    run = _make_run("abc")
    run.status = RunStatus.running
    assert _run_matches_filter(run, "running")
    assert not _run_matches_filter(run, "paused")
    run.status = RunStatus.paused
    assert _run_matches_filter(run, "paused")
    assert not _run_matches_filter(run, "running")


def test_filters_tolerate_null_model_info(tmp_path: Path) -> None:
    # Third-party .tine files can carry model_info: null; filters must not raise.
    path = tmp_path / "nullmodel.tine"
    _make_run("nullmodel").save(path)
    raw = json.loads(path.read_text())
    for step in raw["graph"]["steps"].values():
        step["model_info"] = None
    path.write_text(json.dumps(raw))
    runs, _, _, _ = load_runs(tmp_path)
    (run,) = runs
    assert _run_matches_filter(run, "nullmodel")
    assert not _run_matches_filter(run, "no-such-text")
    assert _matching_steps(run, "search") == ["s2"]


def test_format_timestamp_survives_out_of_range_values() -> None:
    assert _format_timestamp(0) == "(unknown)"
    assert _format_timestamp(1e30).startswith("(invalid timestamp")


def test_truncate_sanitizes_lone_surrogates() -> None:
    label = _truncate("run-\ud800-id", 50)
    assert "\ud800" not in label
    label.encode("utf-8")  # must be encodable for DPG's native renderer


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
        "abc\n",
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
