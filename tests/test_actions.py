"""Tests for pause/resume/fork/save business logic — no DPG interaction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.app import OpentineGUI


def _make_run(run_id: str, status: RunStatus = RunStatus.running) -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.done,
            inputs={},
            outputs={"answer": "42"},
            timestamp=0.1,
            duration=0.1,
        )
    )
    return Run(
        id=run_id,
        graph=graph,
        status=status,
        model_info="m",
        user_prompt="hi",
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
    evil.run_id = "../evil"  # Run.id is a read-only property over run_id
    gui._selected_run = evil
    with patch.object(Run, "pause") as pause_mock:
        gui._pause_selected()
        pause_mock.assert_not_called()
    assert list(tmp_path.glob("*.tine")) == []


def test_action_state_matches_run_status_and_step_selection(tmp_path: Path) -> None:
    gui = _gui(tmp_path)
    configured: dict[str, bool] = {}

    def capture_configure(tag: str, *, enabled: bool) -> None:
        configured[tag] = enabled

    gui._selected_run = _make_run("running", RunStatus.running)
    gui._selected_step = None
    with (
        patch("opentine_gui.app.dpg.does_item_exist", return_value=True),
        patch("opentine_gui.app.dpg.configure_item", side_effect=capture_configure),
    ):
        gui._update_action_state()
    assert configured["btn_pause"] is True
    assert configured["btn_resume"] is False
    assert configured["btn_fork"] is False

    configured.clear()
    gui._selected_run = _make_run("paused", RunStatus.paused)
    gui._selected_step = gui._selected_run.steps[0]
    with (
        patch("opentine_gui.app.dpg.does_item_exist", return_value=True),
        patch("opentine_gui.app.dpg.configure_item", side_effect=capture_configure),
    ):
        gui._update_action_state()
    assert configured["btn_pause"] is False
    assert configured["btn_resume"] is True
    assert configured["btn_fork"] is True


def test_refresh_preserves_selected_run_and_step(tmp_path: Path) -> None:
    run = _make_run("abc")
    run.save(tmp_path / "abc.tine")
    gui = OpentineGUI(tmp_path)
    gui._selected_run = run
    gui._selected_step = run.steps[1]

    values: dict[str, str] = {}

    def fake_set_value(tag: str, value: str) -> None:
        values[tag] = value

    with (
        patch("opentine_gui.app.dpg.does_item_exist", return_value=False),
        patch("opentine_gui.app.dpg.get_item_children", return_value=[]),
        patch("opentine_gui.app.dpg.set_value", side_effect=fake_set_value),
        patch("opentine_gui.app.dpg.configure_item"),
        patch.object(gui, "_rebuild_dag"),
    ):
        gui._refresh()

    assert gui._selected_run is not None
    assert gui._selected_run.id == "abc"
    assert gui._selected_step is not None
    assert gui._selected_step.id == "s2"
    assert "Run: abc" in values["detail_text"]
    assert "ID: s2" in values["step_text"]


def test_apply_dir_clears_selection_filters_and_persists(tmp_path: Path) -> None:
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    run = _make_run("abc")
    gui = OpentineGUI(old_dir)
    gui._runs = [run]
    gui._selected_run = run
    gui._selected_step = run.steps[0]
    gui._run_filter = "abc"
    gui._step_filter = "tool"

    values: dict[str, str] = {"dir_picker_input": str(new_dir)}
    configured: dict[str, bool] = {}

    def fake_get_value(tag: str) -> str:
        return values[tag]

    def fake_set_value(tag: str, value: str) -> None:
        values[tag] = value

    def fake_configure(tag: str, *, show: bool) -> None:
        configured[tag] = show

    def fake_refresh() -> None:
        gui._runs = []

    with (
        patch("opentine_gui.app._save_preferences") as save_preferences,
        patch("opentine_gui.app.dpg.get_value", side_effect=fake_get_value),
        patch("opentine_gui.app.dpg.set_value", side_effect=fake_set_value),
        patch("opentine_gui.app.dpg.configure_item", side_effect=fake_configure),
        patch("opentine_gui.app.dpg.does_item_exist", return_value=True),
        patch("opentine_gui.app.dpg.get_item_children", return_value=[]),
        patch.object(gui, "_refresh", side_effect=fake_refresh),
    ):
        gui._apply_dir()

    assert gui._runs_dir == new_dir
    assert gui._selected_run is None
    assert gui._selected_step is None
    assert gui._run_filter == ""
    assert gui._step_filter == ""
    assert values["run_filter"] == ""
    assert values["step_filter"] == ""
    assert configured["dir_picker"] is False
    save_preferences.assert_called()
