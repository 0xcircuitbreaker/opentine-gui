"""Smoke tests for run loading and path safety — headless, no DPG."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from opentine.core import Run, RunStatus, Step, StepKind

from opentine_gui.app import MAX_TINE_BYTES, _safe_run_path, load_runs


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
