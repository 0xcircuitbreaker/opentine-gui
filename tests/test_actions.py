"""Tests for pause/resume/fork/save business logic — no DPG interaction."""

from __future__ import annotations

import inspect
import re
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import app
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
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        gui._fork_selected()
    assert (tmp_path / "abc.tine").exists()
    # A new .tine file has appeared.
    tine_files = sorted(p.name for p in tmp_path.glob("*.tine"))
    assert len(tine_files) == 2
    assert "abc.tine" in tine_files

    # The forked artifact must be a loadable run with real 0.3.0 lineage.
    fork_path = next(p for p in tmp_path.glob("*.tine") if p.name != "abc.tine")
    forked = Run.load(fork_path)
    assert forked.metadata["forked_from"] == "abc"
    assert forked.metadata["fork_point"] == "s1"
    assert [s.id for s in forked.steps] == ["s1"]
    assert Run.verify_integrity(fork_path).ok
    assert gui._selected_run is not None
    assert gui._selected_run.id == forked.id
    # Original run untouched.
    original = Run.load(tmp_path / "abc.tine")
    assert [s.id for s in original.steps] == ["s1", "s2"]


def test_pause_and_resume_write_to_source_file_not_id_named_file(tmp_path: Path) -> None:
    # A run whose filename differs from its id (renamed/shared file) must be
    # paused/resumed in place, not duplicated as <id>.tine.
    run = _make_run("abc", RunStatus.running)
    source = tmp_path / "descriptive-name.tine"
    run.save(source)
    gui = _gui(tmp_path)
    gui._selected_run = run
    gui._run_paths = {"abc": source}
    gui._pause_selected()
    assert not (tmp_path / "abc.tine").exists()
    assert Run.load(source).status == RunStatus.paused

    paused = Run.load(source)
    gui._selected_run = paused
    gui._resume_selected()
    assert not (tmp_path / "abc.tine").exists()
    assert Run.load(source).status == RunStatus.running


def _stable_source(tmp_path: Path) -> Path:
    """One canonical source artifact: fork ids derive from the source digest."""
    path = tmp_path / "_source.tine"
    if not path.exists():
        _make_run("abc", RunStatus.completed).save(path)
    return path


def _fork_ready_gui(tmp_path: Path, messages: list[str]) -> OpentineGUI:
    path = tmp_path / "abc.tine"
    if not path.exists():
        # Written once: a fork id is derived from the source's digest, and
        # Run() stamps created_at, so re-saving would change the source.
        _make_run("abc", RunStatus.completed).save(path)
    gui = OpentineGUI(tmp_path)
    gui._refresh = lambda: None  # type: ignore[assignment]
    gui._set_status = messages.append  # type: ignore[assignment]
    gui._selected_run = Run.load(path)
    gui._run_paths = {"abc": path}
    gui._selected_step = gui._selected_run.get_step("s1")
    return gui


def test_fork_to_branch_records_branch_and_reason(tmp_path: Path) -> None:
    messages: list[str] = []
    gui = _fork_ready_gui(tmp_path, messages)
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        gui._do_fork(branch="experiment", reason="try a stronger model")
    forked = gui._selected_run
    assert forked is not None
    assert forked.metadata["fork"]["branch"] == "experiment"
    # Matches opentine's own MCP convention: recorded in metadata (so signatures
    # cover it) and folded into the fork identity via intent.
    assert forked.metadata["fork_reason"] == "try a stronger model"
    plain = Run.load(tmp_path / "abc.tine").fork("s1")
    assert forked.metadata["fork"]["intent"] != plain.metadata["fork"]["intent"]


def test_reproducible_fork_yields_a_stable_id(tmp_path: Path) -> None:
    # Same source, same point, same branch/reason => same id. Each fork gets its
    # own directory: forking reproducibly twice into ONE directory is the
    # overwrite case, covered separately below.
    messages: list[str] = []
    ids = []
    for name in ("one", "two"):
        target = tmp_path / name
        target.mkdir()
        shutil.copy2(_stable_source(tmp_path), target / "abc.tine")
        gui = _fork_ready_gui(target, messages)
        with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
            gui._do_fork(reproducible=True)
        ids.append(gui._selected_run.id)
    assert ids[0] == ids[1]

    unique = tmp_path / "three"
    unique.mkdir()
    shutil.copy2(_stable_source(tmp_path), unique / "abc.tine")
    gui = _fork_ready_gui(unique, messages)
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        gui._do_fork()
    assert gui._selected_run.id != ids[0]


def test_reproducible_fork_refuses_to_overwrite_the_earlier_fork(tmp_path: Path) -> None:
    # The failure mode opentine 0.4.0's nonce exists to prevent, which nonce=""
    # deliberately opts out of: two reproducible forks derive one id and one
    # filename, so the second save would destroy the first and everything in it.
    messages: list[str] = []
    gui = _fork_ready_gui(tmp_path, messages)
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        gui._do_fork(reproducible=True)
    fork_path = next(p for p in tmp_path.glob("*.tine") if p.name != "abc.tine")

    # Work happens inside the fork.
    work = Run.load(fork_path)
    work.add_step(StepKind.think, {"text": "hours of debugging live here"})
    work.save(fork_path)
    assert len(Run.load(fork_path).steps) == 2

    gui2 = _fork_ready_gui(tmp_path, messages)
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        gui2._do_fork(reproducible=True)

    assert any("already exists" in m for m in messages)
    assert len(list(tmp_path.glob("*.tine"))) == 2, "no new file, and none destroyed"
    assert len(Run.load(fork_path).steps) == 2, "the earlier fork's work survived"


def test_fork_reason_length_is_capped_like_opentine(tmp_path: Path) -> None:
    messages: list[str] = []
    gui = _fork_ready_gui(tmp_path, messages)
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        gui._do_fork(reason="x" * 5000)
    assert any("at most 4096" in m for m in messages)
    assert list(tmp_path.glob("*.tine")) == [tmp_path / "abc.tine"], "no file written"


def test_sibling_forks_of_one_step_do_not_overwrite(tmp_path: Path) -> None:
    # The 0.4.0 behaviour the GUI depends on: before it, the second fork of the
    # same step reused the first's id and filename and destroyed it.
    messages: list[str] = []
    with patch("opentine_gui.app.dpg.does_item_exist", return_value=False):
        _fork_ready_gui(tmp_path, messages)._do_fork()
        _fork_ready_gui(tmp_path, messages)._do_fork()
    assert len(list(tmp_path.glob("*.tine"))) == 3  # source + two distinct forks


#: Every bound method the app registers as a Dear PyGui callback.
CALLBACK_NAMES = [
    "_apply_dir", "_clear_step_filter", "_compare_runs", "_confirm_fork",
    "_copy_run_id", "_copy_step_id", "_export_otel", "_fork_selected", "_on_ctrl_c", "_on_ctrl_f",
    "_on_ctrl_r", "_on_escape", "_on_filter_change", "_on_link_created",
    "_on_transcript_step", "_open_transcript",
    "_on_link_deleted", "_on_run_selected", "_on_step_filter_change", "_on_step_open",
    "_on_viewport_resize", "_open_diff_dialog", "_open_dir_picker", "_open_fork_dialog",
    "_pause_selected", "_refresh", "_resume_selected",
]


def test_every_callback_survives_manual_callback_dispatch(tmp_path: Path) -> None:
    """The app runs with manual_callback_management, draining the queue itself.

    dpg.run_callbacks builds arguments as job[arg + 1] over a 4-tuple
    (callback, sender, app_data, user_data), so a callback taking more than
    three parameters raises IndexError at click time — never at import. It also
    calls inspect.signature, which some callables reject.
    """
    gui = OpentineGUI(tmp_path)
    for name in CALLBACK_NAMES:
        fn = getattr(gui, name)
        params = len(inspect.signature(fn).parameters)
        assert params <= 3, f"{name} takes {params} params; run_callbacks can supply 3"


def test_callback_inventory_matches_the_source(tmp_path: Path) -> None:
    # Keeps CALLBACK_NAMES honest as callbacks are added.
    source = Path(app.__file__).read_text(encoding="utf-8")
    registered = set(re.findall(r"callback=self\.([_a-zA-Z0-9]+)", source))
    registered |= set(re.findall(r"self\.(_move_selection)\(", source))
    missing = registered - set(CALLBACK_NAMES) - {"_move_selection"}
    assert not missing, f"new callbacks not covered by the arity test: {sorted(missing)}"


def test_otel_export_writes_a_valid_document_and_never_touches_the_artifact(
    tmp_path: Path,
) -> None:
    import json

    source = tmp_path / "abc.tine"
    _make_run("abc", RunStatus.completed).save(source)
    before = source.read_bytes()
    messages: list[str] = []
    gui = OpentineGUI(tmp_path)
    gui._set_status = messages.append  # type: ignore[assignment]
    gui._selected_run = Run.load(source)

    gui._export_otel()

    out = tmp_path / "abc.otel.json"
    assert out.exists(), messages
    document = json.loads(out.read_text(encoding="utf-8"))
    assert "resourceSpans" in document
    assert app._span_count(document) == 2  # one span per step
    assert "span(s)" in messages[-1]
    # Export is read-only: the artifact and its digest are untouched.
    assert source.read_bytes() == before
    assert Run.verify_integrity(source).ok


def test_otel_export_requires_a_selected_run(tmp_path: Path) -> None:
    messages: list[str] = []
    gui = OpentineGUI(tmp_path)
    gui._set_status = messages.append  # type: ignore[assignment]
    gui._export_otel()
    assert "Select a run" in messages[-1]
    assert not list(tmp_path.glob("*.otel.json"))


def test_otel_export_degrades_on_an_older_opentine(tmp_path: Path) -> None:
    # The declared floor is 0.4.0; the exporter arrived in 0.5.0.
    _make_run("abc").save(tmp_path / "abc.tine")
    messages: list[str] = []
    gui = OpentineGUI(tmp_path)
    gui._set_status = messages.append  # type: ignore[assignment]
    gui._selected_run = Run.load(tmp_path / "abc.tine")
    with patch("opentine_gui.app.to_otel_genai_document", None):
        gui._export_otel()
    assert "0.5.0" in messages[-1]
    assert not list(tmp_path.glob("*.otel.json"))


def test_otel_export_path_is_id_safe(tmp_path: Path) -> None:
    # An artifact must not steer the export out of the runs directory.
    assert app._export_path(tmp_path, "abc").name == "abc.otel.json"
    for hostile in ("../escape", "sub/dir", "", "has space"):
        with pytest.raises(ValueError):
            app._export_path(tmp_path, hostile)


def test_span_count_tolerates_any_document_shape() -> None:
    for doc in ("a string", None, {}, {"resourceSpans": "x"}, {"resourceSpans": [{}]}, 42):
        assert isinstance(app._span_count(doc), int)


def _keyboard_gui(tmp_path: Path, run_ids: list[str]) -> OpentineGUI:
    gui = OpentineGUI(tmp_path)
    gui._runs = [_make_run(rid) for rid in run_ids]
    gui._set_status = lambda msg: None  # type: ignore[assignment]
    return gui


def test_arrow_keys_defer_selection_to_the_main_loop(tmp_path: Path) -> None:
    # Key handlers run off the render thread, so navigation must record intent
    # rather than rebuilding the DAG inline.
    gui = _keyboard_gui(tmp_path, ["a", "b", "c"])
    gui._selected_run = gui._runs[0]
    with (
        patch.object(gui, "_typing", return_value=False),
        patch.object(gui, "_modal_open", return_value=None),
        patch.object(gui, "_select_run") as select,
    ):
        gui._move_selection(1)
        assert gui._pending_select == "b"
        select.assert_not_called()  # nothing touched items yet

        gui._apply_pending_input()  # what run()'s loop does each frame
        select.assert_called_once_with("b")
        assert gui._pending_select is None


def test_arrow_keys_clamp_at_both_ends(tmp_path: Path) -> None:
    gui = _keyboard_gui(tmp_path, ["a", "b"])
    with (
        patch.object(gui, "_typing", return_value=False),
        patch.object(gui, "_modal_open", return_value=None),
    ):
        gui._selected_run = gui._runs[0]
        gui._move_selection(-1)
        assert gui._pending_select == "a"  # already at the top
        gui._selected_run = gui._runs[1]
        gui._move_selection(1)
        assert gui._pending_select == "b"  # already at the bottom


def test_arrow_keys_select_the_first_run_when_nothing_is_selected(tmp_path: Path) -> None:
    gui = _keyboard_gui(tmp_path, ["a", "b"])
    with (
        patch.object(gui, "_typing", return_value=False),
        patch.object(gui, "_modal_open", return_value=None),
    ):
        gui._move_selection(1)
    assert gui._pending_select == "a"


def test_arrow_keys_are_inert_while_typing_or_in_a_modal(tmp_path: Path) -> None:
    gui = _keyboard_gui(tmp_path, ["a", "b"])
    gui._selected_run = gui._runs[0]
    with (
        patch.object(gui, "_typing", return_value=True),
        patch.object(gui, "_modal_open", return_value=None),
    ):
        gui._move_selection(1)
    assert gui._pending_select is None, "arrow keys must reach a focused text field"

    with (
        patch.object(gui, "_typing", return_value=False),
        patch.object(gui, "_modal_open", return_value="fork_dialog"),
    ):
        gui._move_selection(1)
    assert gui._pending_select is None, "arrow keys must not move behind a modal"


def test_ctrl_r_forces_an_immediate_reload(tmp_path: Path) -> None:
    # Clearing the directory signature alone leaves the reload behind
    # _auto_refresh_tick's rate gate for up to AUTO_REFRESH_SECONDS.
    gui = _keyboard_gui(tmp_path, ["a"])
    gui._last_check = time.monotonic()  # a tick just ran
    with (
        patch("opentine_gui.app.dpg.is_key_down", return_value=True),
        patch.object(gui, "_typing", return_value=False),
    ):
        gui._on_ctrl_r()

    # tmp_path is empty, so its signature is () — the value a "clear the
    # signature" implementation would have set, reloading nothing.
    refreshed: list[bool] = []
    with patch.object(gui, "_refresh", side_effect=lambda: refreshed.append(True)):
        gui._auto_refresh_tick()
    assert refreshed, "the very next tick must reload, not wait out the interval"

    refreshed.clear()
    with patch.object(gui, "_refresh", side_effect=lambda: refreshed.append(True)):
        gui._auto_refresh_tick()
    assert not refreshed, "the force flag is one-shot"


def test_ctrl_r_is_inert_without_the_modifier(tmp_path: Path) -> None:
    gui = _keyboard_gui(tmp_path, ["a"])
    with (
        patch("opentine_gui.app.dpg.is_key_down", return_value=False),
        patch.object(gui, "_typing", return_value=False),
    ):
        gui._on_ctrl_r()
    assert not gui._force_refresh, "plain 'r' must not trigger a reload"


def test_escape_closes_a_modal_before_clearing_filters(tmp_path: Path) -> None:
    gui = _keyboard_gui(tmp_path, ["a"])
    gui._step_filter = "tool"
    closed: list[str] = []
    with (
        patch.object(gui, "_modal_open", return_value="diff_dialog"),
        patch("opentine_gui.app.dpg.configure_item",
              side_effect=lambda tag, **kw: closed.append(tag)),
    ):
        gui._on_escape()
    assert closed == ["diff_dialog"]
    assert gui._step_filter == "tool", "the modal takes priority over the filter"


def test_escape_clears_the_step_filter_then_the_run_filter(tmp_path: Path) -> None:
    gui = _keyboard_gui(tmp_path, ["a"])
    gui._step_filter = "tool"
    gui._run_filter = "demo"
    with (
        patch.object(gui, "_modal_open", return_value=None),
        patch.object(gui, "_clear_step_filter") as clear_step,
    ):
        gui._on_escape()
    clear_step.assert_called_once()

    gui._step_filter = ""
    with (
        patch.object(gui, "_modal_open", return_value=None),
        patch("opentine_gui.app.dpg.set_value"),
        patch.object(gui, "_on_filter_change") as filter_change,
    ):
        gui._on_escape()
    filter_change.assert_called_once_with(None, "")


def test_resume_refuses_when_disk_status_moved_past_paused(tmp_path: Path) -> None:
    # GUI cache says paused, but the agent completed the run on disk since the
    # last refresh; resume must not rewrite a terminal artifact to running.
    stale_paused = _make_run("abc", RunStatus.paused)
    completed = _make_run("abc", RunStatus.completed)
    path = tmp_path / "abc.tine"
    completed.save(path)
    gui = _gui(tmp_path)
    gui._selected_run = stale_paused
    gui._run_paths = {"abc": path}
    gui._resume_selected()
    assert Run.load(path).status == RunStatus.completed


def test_pause_reloads_disk_state_instead_of_stale_snapshot(tmp_path: Path) -> None:
    # The GUI's cached Run can be a refresh interval old; pausing must not
    # truncate steps an agent wrote to disk in the meantime.
    stale = _make_run("abc", RunStatus.running)  # 2 steps
    fresh = _make_run("abc", RunStatus.running)
    fresh.graph.add(
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.think,
            inputs={"text": "agent wrote this after the GUI's last refresh"},
        )
    )
    path = tmp_path / "abc.tine"
    fresh.save(path)  # disk has 3 steps
    gui = _gui(tmp_path)
    gui._selected_run = stale  # GUI cache has 2
    gui._run_paths = {"abc": path}
    gui._pause_selected()
    on_disk = Run.load(path)
    assert on_disk.status == RunStatus.paused
    assert [s.id for s in on_disk.steps] == ["s1", "s2", "s3"]


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
    rebuild_dag.assert_called_once_with(run, highlight=set())
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
