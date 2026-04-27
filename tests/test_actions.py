"""Tests for pause/resume/fork/save business logic — no DPG interaction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from opentine.core import Run, RunStatus, Step, StepKind

from opentine_gui.app import OpentineGUI


def _make_run(run_id: str, status: RunStatus = RunStatus.running) -> Run:
    return Run(
        id=run_id,
        steps=[
            Step(
                id="s1",
                parent_id=None,
                kind=StepKind.think,
                inputs={"text": "plan"},
                outputs={},
                model_info="",
                timestamp=0.0,
                duration=0.0,
                cost=0.0,
            ),
            Step(
                id="s2",
                parent_id="s1",
                kind=StepKind.done,
                inputs={},
                outputs={"answer": "42"},
                model_info="",
                timestamp=0.1,
                duration=0.1,
                cost=0.0,
            ),
        ],
        status=status,
        model_info="m",
        system_prompt="",
        user_prompt="hi",
        created_at=0.0,
        metadata={},
    )


def _gui(tmp_path: Path) -> OpentineGUI:
    gui = OpentineGUI(tmp_path)
    # Stub out DPG/refresh side effects so pure business logic can be exercised.
    gui._refresh = lambda: None  # type: ignore[assignment]
    gui._set_status = lambda msg: None  # type: ignore[assignment]
    return gui


def test_pause_writes_file_with_paused_status(tmp_path: Path) -> None:
    gui = _gui(tmp_path)
    run = _make_run("abc", RunStatus.running)
    gui._selected_run = run
    gui._pause_selected()
    loaded = Run.load(tmp_path / "abc.tine")
    assert loaded.status == RunStatus.paused


def test_pause_noop_when_not_running(tmp_path: Path) -> None:
    gui = _gui(tmp_path)
    gui._selected_run = _make_run("abc", RunStatus.completed)
    gui._pause_selected()
    assert not (tmp_path / "abc.tine").exists()


def test_resume_writes_file_with_running_status(tmp_path: Path) -> None:
    # Seed a paused run on disk
    run = _make_run("abc", RunStatus.paused)
    run.save(tmp_path / "abc.tine")
    gui = _gui(tmp_path)
    gui._selected_run = run
    gui._resume_selected()
    loaded = Run.load(tmp_path / "abc.tine")
    assert loaded.status == RunStatus.running
    assert gui._selected_run is not None
    assert gui._selected_run.status == RunStatus.running


def test_fork_writes_new_file_and_leaves_original(tmp_path: Path) -> None:
    run = _make_run("abc", RunStatus.completed)
    run.save(tmp_path / "abc.tine")
    gui = _gui(tmp_path)
    gui._selected_run = run
    gui._selected_step = run.steps[0]
    gui._fork_selected()
    assert (tmp_path / "abc.tine").exists()
    # A new .tine file has appeared.
    tine_files = sorted(p.name for p in tmp_path.glob("*.tine"))
    assert len(tine_files) == 2
    assert "abc.tine" in tine_files


def test_fork_requires_step_selection(tmp_path: Path) -> None:
    gui = _gui(tmp_path)
    gui._selected_run = _make_run("abc")
    gui._selected_step = None
    gui._fork_selected()
    assert list(tmp_path.glob("*.tine")) == []


def test_select_run_updates_detail_state(tmp_path: Path) -> None:
    gui = _gui(tmp_path)
    run = _make_run("abc")
    gui._runs = [run]
    gui._selected_step = run.steps[0]
    with (
        patch("opentine_gui.app.dpg.set_value"),
        patch.object(gui, "_show_run_detail") as show_detail,
        patch.object(gui, "_rebuild_dag") as rebuild_dag,
        patch.object(gui, "_render_run_table") as render_table,
        patch.object(gui, "_update_action_state") as update_actions,
    ):
        gui._select_run("abc")

    assert gui._selected_run == run
    assert gui._selected_step is None
    show_detail.assert_called_once_with(run)
    rebuild_dag.assert_called_once_with(run)
    render_table.assert_called_once()
    update_actions.assert_called_once()


def test_pause_rejects_unsafe_run_id(tmp_path: Path) -> None:
    gui = _gui(tmp_path)
    evil = _make_run("abc", RunStatus.running)
    evil.id = "../evil"  # type: ignore[misc]
    gui._selected_run = evil
    with patch.object(Run, "pause") as pause_mock:
        gui._pause_selected()
        pause_mock.assert_not_called()
    assert list(tmp_path.glob("*.tine")) == []
