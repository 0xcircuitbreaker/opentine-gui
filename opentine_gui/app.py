"""Dear PyGui application — 3-panel desktop dashboard for opentine runs.

Layout:
  Left:   Run list (table) + actions
  Center: Run detail + selected-step detail
  Right:  DAG node editor (parent -> child)
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import dearpygui.dearpygui as dpg
from opentine.core import Run, RunStatus, Step, StepKind

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")
MAX_TINE_BYTES = 10 * 1024 * 1024  # skip .tine files larger than 10 MiB


def _safe_run_path(runs_dir: Path, run_id: str) -> Path:
    """Return runs_dir/<id>.tine iff run_id is safe and resolves inside runs_dir."""
    if not SAFE_ID.match(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    base = runs_dir.resolve()
    target = (base / f"{run_id}.tine").resolve()
    if base not in target.parents:
        raise ValueError(f"path escapes runs dir: {target}")
    return target

BRAND = [120, 164, 255]
BRAND_DIM = [84, 117, 184]

SURFACE_APP = [31, 30, 27]
SURFACE_SIDEBAR = [25, 24, 20]
SURFACE_PANEL = [35, 34, 31]
SURFACE_CARD = [39, 38, 34]
SURFACE_INPUT = [31, 30, 27]
SURFACE_BUTTON = [52, 50, 45]

TEXT_PRIMARY = [230, 225, 216]
TEXT_SECONDARY = [184, 177, 166]
TEXT_MUTED = [127, 119, 109]
TEXT_FAINT = [98, 91, 83]

BORDER_DEFAULT = [52, 50, 45]
BORDER_STRONG = [70, 67, 59]
STATE_HOVER = [45, 43, 39]
STATE_SELECTED = [30, 52, 76]
STATE_ACTIVE = [32, 58, 85]

ACCENT_GREEN = [121, 216, 157]
ACCENT_ORANGE = [243, 161, 91]
ACCENT_RED = [255, 138, 134]
ACCENT_PURPLE = [182, 156, 255]
ACCENT_TEAL = [100, 209, 200]
ACCENT_YELLOW = [242, 200, 107]

STEP_COLORS: dict[StepKind, list[int]] = {
    StepKind.think: ACCENT_YELLOW,
    StepKind.tool: BRAND,
    StepKind.model: ACCENT_TEAL,
    StepKind.done: ACCENT_GREEN,
    StepKind.error: ACCENT_RED,
}

RUN_STATUS_COLORS: dict[RunStatus, list[int]] = {
    RunStatus.running: BRAND,
    RunStatus.paused: ACCENT_ORANGE,
    RunStatus.completed: ACCENT_GREEN,
    RunStatus.failed: ACCENT_RED,
}

DEFAULT_RUNS_DIR = Path(".tine_runs")
AUTO_REFRESH_SECONDS = 2.0
PREFERENCES_ENV = "OPENTINE_GUI_PREFS"
PREFERENCES_FILE = "preferences.json"


def _preferences_path() -> Path:
    override = os.environ.get(PREFERENCES_ENV)
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "opentine-gui" / PREFERENCES_FILE


def _load_preferences(path: Path | None = None) -> dict[str, str]:
    path = path or _preferences_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _save_preferences(preferences: dict[str, str], path: Path | None = None) -> None:
    path = path or _preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preferences, indent=2, sort_keys=True) + "\n")

_APP_THEME: int | None = None
_BUTTON_THEMES: dict[str, int] = {}


def load_runs(
    runs_dir: Path,
) -> tuple[list[Run], list[str], tuple[tuple[str, float, int], ...]]:
    """Return (runs, errors, signature) computed atomically from one directory scan.

    Oversized files are skipped with an error rather than decoded, to bound
    memory/CPU on the auto-refresh loop.
    """
    runs: list[Run] = []
    errors: list[str] = []
    sig_entries: list[tuple[str, float, int]] = []
    if not runs_dir.exists():
        return runs, errors, ()
    files = []
    for f in runs_dir.glob("*.tine"):
        try:
            st = f.stat()
        except OSError as e:
            errors.append(f"{f.name}: {e}")
            continue
        files.append((f, st))
        sig_entries.append((f.name, st.st_mtime, st.st_size))
    files.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
    for f, st in files:
        if st.st_size > MAX_TINE_BYTES:
            errors.append(f"{f.name}: skipped ({st.st_size} bytes > {MAX_TINE_BYTES})")
            continue
        try:
            runs.append(Run.load(f))
        except Exception as e:
            errors.append(f"{f.name}: {e}")
    sig_entries.sort()
    return runs, errors, tuple(sig_entries)


def _dir_signature(runs_dir: Path) -> tuple[tuple[str, float, int], ...]:
    if not runs_dir.exists():
        return ()
    entries: list[tuple[str, float, int]] = []
    for f in runs_dir.glob("*.tine"):
        try:
            st = f.stat()
        except OSError:
            continue
        entries.append((f.name, st.st_mtime, st.st_size))
    entries.sort()
    return tuple(entries)


class OpentineGUI:
    def __init__(self, runs_dir: Path | None = None) -> None:
        self._preferences = _load_preferences()
        preferred_dir = self._preferences.get("last_runs_dir")
        if runs_dir is None and preferred_dir:
            self._runs_dir = Path(preferred_dir).expanduser()
        else:
            self._runs_dir = runs_dir or DEFAULT_RUNS_DIR
        self._runs: list[Run] = []
        self._errors: list[str] = []
        self._selected_run: Run | None = None
        self._selected_step: Step | None = None
        self._last_signature: tuple = ()
        self._last_check: float = 0.0
        self._run_filter = self._preferences.get("last_filter", "").strip().lower()
        self._step_filter = ""

    def run(self) -> None:
        dpg.create_context()
        dpg.bind_theme(_app_theme())
        dpg.create_viewport(title="opentine - agent run console", width=1440, height=860)

        with dpg.window(tag="primary"):
            with dpg.menu_bar():
                with dpg.menu(label="File"):
                    dpg.add_menu_item(label="Refresh", callback=self._refresh)
                    dpg.add_menu_item(label="Change runs dir…", callback=self._open_dir_picker)
                    dpg.add_separator()
                    dpg.add_menu_item(label="Quit", callback=lambda: dpg.stop_dearpygui())
                with dpg.menu(label="Run"):
                    dpg.add_menu_item(
                        label="Pause", callback=self._pause_selected, tag="menu_pause"
                    )
                    dpg.add_menu_item(
                        label="Resume", callback=self._resume_selected, tag="menu_resume"
                    )
                    dpg.add_menu_item(
                        label="Fork from step",
                        callback=self._fork_selected,
                        tag="menu_fork",
                    )
            self._build_top_bar()
            with dpg.group(horizontal=True):
                self._build_run_list()
                self._build_detail_panel()
                self._build_dag_panel()
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_text("", tag="status_bar", color=TEXT_SECONDARY)
                dpg.add_text("", tag="status_meta", color=TEXT_MUTED)

        with dpg.window(label="Change runs directory", modal=True, show=False,
                        tag="dir_picker", width=500, height=120, no_resize=True):
            dpg.add_input_text(tag="dir_picker_input", default_value=str(self._runs_dir), width=480)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Apply", callback=self._apply_dir)
                dpg.add_button(
                    label="Cancel",
                    callback=lambda: dpg.configure_item("dir_picker", show=False),
                )

        dpg.set_primary_window("primary", True)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        self._refresh()
        while dpg.is_dearpygui_running():
            self._auto_refresh_tick()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

    def _build_top_bar(self) -> None:
        with dpg.group(horizontal=True):
            dpg.add_text("opentine", color=TEXT_PRIMARY)
            dpg.add_text("agent run console", color=TEXT_MUTED)
            dpg.add_spacer(width=20)
            dpg.add_text(str(self._runs_dir), tag="top_runs_dir", color=TEXT_MUTED)
        dpg.add_separator()

    def _build_run_list(self) -> None:
        with dpg.child_window(width=340, border=True):
            _panel_header("Runs", "Search, select, and manage traces")
            dpg.add_input_text(
                hint="Search id, status, model, prompt, steps",
                tag="run_filter",
                default_value=self._run_filter,
                width=-1,
                callback=self._on_filter_change,
            )
            dpg.add_text("", tag="run_summary", wrap=300, color=[180, 180, 180])
            with dpg.group(horizontal=True):
                _action_button("Pause", self._pause_selected, "btn_pause")
                _action_button("Resume", self._resume_selected, "btn_resume")
                _action_button("Fork", self._fork_selected, "btn_fork")
            dpg.add_separator()
            with dpg.table(
                header_row=True,
                borders_innerH=True,
                borders_outerH=True,
                row_background=True,
                resizable=False,
                policy=dpg.mvTable_SizingStretchProp,
                tag="run_table",
            ):
                dpg.add_table_column(label="ID")
                dpg.add_table_column(label="Status")
                dpg.add_table_column(label="Cost")
            dpg.add_separator()
            dpg.add_text("Load errors", color=ACCENT_ORANGE, tag="err_header", show=False)
            dpg.add_text("", tag="err_text", wrap=312, color=ACCENT_ORANGE)

    def _build_detail_panel(self) -> None:
        with dpg.child_window(width=480, border=True):
            _panel_header("Run inspector", "Trace metadata and prompt")
            dpg.add_separator()
            dpg.add_text("Select a run", tag="detail_text", wrap=452, color=TEXT_SECONDARY)
            dpg.add_spacer(height=10)
            _panel_header("Step inspector", "Inputs, outputs, timing, and cost")
            dpg.add_separator()
            dpg.add_text(
                "Select a step in the DAG",
                tag="step_text",
                wrap=452,
                color=TEXT_SECONDARY,
            )

    def _build_dag_panel(self) -> None:
        with dpg.child_window(border=True):
            _panel_header("Step DAG", "Parent-child execution graph")
            dpg.add_text("", tag="dag_summary", wrap=580, color=TEXT_SECONDARY)
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    hint="Highlight step id, kind, tool, payload... press Enter",
                    tag="step_filter",
                    width=360,
                    callback=self._on_step_filter_change,
                    on_enter=True,
                )
                dpg.add_button(label="Clear", callback=self._clear_step_filter, width=70)
            with dpg.group(horizontal=True):
                for kind in StepKind:
                    dpg.add_text(kind.value, color=STEP_COLORS[kind])
            dpg.add_separator()
            with dpg.node_editor(
                tag="dag_editor",
                callback=self._on_link_created,
                delink_callback=self._on_link_deleted,
                minimap=True,
                minimap_location=dpg.mvNodeMiniMap_Location_BottomRight,
            ):
                pass

    def _set_status(self, msg: str) -> None:
        if dpg.does_item_exist("status_bar"):
            dpg.set_value("status_bar", msg)

    def _auto_refresh_tick(self) -> None:
        now = time.monotonic()
        if now - self._last_check < AUTO_REFRESH_SECONDS:
            return
        self._last_check = now
        sig = _dir_signature(self._runs_dir)
        if sig != self._last_signature:
            self._refresh()

    def _refresh(self) -> None:
        self._runs, self._errors, self._last_signature = load_runs(self._runs_dir)
        selected_id = self._selected_run.id if self._selected_run else None
        if selected_id:
            match = next((r for r in self._runs if r.id == selected_id), None)
            self._selected_run = match
            if match is None:
                self._selected_step = None
                dpg.set_value("detail_text", "Select a run")
                dpg.set_value("step_text", "Select a step in the DAG")
                self._clear_dag()
            else:
                self._show_run_detail(match)
                self._rebuild_dag(match)
                if self._selected_step:
                    step = match.get_step(self._selected_step.id)
                    self._selected_step = step
                    if step:
                        self._show_step_detail(step)
                    else:
                        dpg.set_value("step_text", "Step gone")
        self._render_run_table()
        self._render_errors()
        self._update_action_state()
        visible_count = len(self._filtered_runs())
        filter_note = f", {visible_count} shown" if self._run_filter else ""
        if dpg.does_item_exist("top_runs_dir"):
            dpg.set_value("top_runs_dir", str(self._runs_dir))
        if dpg.does_item_exist("status_meta"):
            dpg.set_value("status_meta", time.strftime("%H:%M:%S"))
        self._set_status(
            f"{self._runs_dir} - {len(self._runs)} run(s){filter_note}"
            + (f", {len(self._errors)} error(s)" if self._errors else "")
        )

    def _render_run_table(self) -> None:
        if not dpg.does_item_exist("run_table"):
            return
        for child in dpg.get_item_children("run_table", slot=1) or []:
            dpg.delete_item(child)
        visible_runs = self._filtered_runs()
        for run in visible_runs:
            with dpg.table_row(parent="run_table"):
                color = RUN_STATUS_COLORS.get(run.status, [255, 255, 255])
                selected = self._selected_run is not None and self._selected_run.id == run.id
                label = f"> {run.id}" if selected else run.id
                dpg.add_button(
                    label=label,
                    callback=self._on_run_selected,
                    user_data=run.id,
                    width=130,
                )
                dpg.add_text(run.status.value, color=color)
                dpg.add_text(f"${run.total_cost:.4f}")
        dpg.set_value("run_summary", _run_list_summary(self._runs, visible_runs, self._run_filter))
        if not visible_runs:
            with dpg.table_row(parent="run_table"):
                msg = "No runs match filter" if self._run_filter else "No .tine runs found"
                dpg.add_text(msg, color=[150, 150, 150])
                dpg.add_text("")
                dpg.add_text("")

    def _render_errors(self) -> None:
        if self._errors:
            dpg.configure_item("err_header", show=True)
            shown = self._errors[:10]
            if len(self._errors) > len(shown):
                shown.append(f"...and {len(self._errors) - len(shown)} more")
            dpg.set_value("err_text", "\n".join(shown))
        else:
            dpg.configure_item("err_header", show=False)
            dpg.set_value("err_text", "")

    def _on_run_selected(self, sender, app_data, user_data) -> None:
        self._select_run(user_data)

    def _select_run(self, run_id: str) -> None:
        run = next((r for r in self._runs if r.id == run_id), None)
        if run is None:
            return
        self._selected_run = run
        self._selected_step = None
        self._show_run_detail(self._selected_run)
        dpg.set_value("step_text", "Select a step in the DAG")
        self._rebuild_dag(self._selected_run)
        self._render_run_table()
        self._update_action_state()

    def _on_filter_change(self, sender, app_data) -> None:
        self._run_filter = (app_data or "").strip().lower()
        self._persist_preferences()
        self._render_run_table()
        self._update_action_state()
        shown = len(self._filtered_runs())
        self._set_status(f"{self._runs_dir} - {shown}/{len(self._runs)} run(s) shown")

    def _on_step_filter_change(self, sender, app_data) -> None:
        self._step_filter = (app_data or "").strip().lower()
        matches = (
            _matching_steps(self._selected_run, self._step_filter)
            if self._selected_run
            else []
        )
        if self._selected_run and dpg.does_item_exist("dag_summary"):
            dpg.set_value(
                "dag_summary",
                _dag_summary(self._selected_run, self._step_filter, matches),
            )
        if self._step_filter:
            self._set_status(f"{len(matches)} step(s) highlighted for '{self._step_filter}'")

    def _clear_step_filter(self) -> None:
        self._step_filter = ""
        if dpg.does_item_exist("step_filter"):
            dpg.set_value("step_filter", "")
        if self._selected_run and dpg.does_item_exist("dag_summary"):
            dpg.set_value("dag_summary", _dag_summary(self._selected_run))

    def _filtered_runs(self) -> list[Run]:
        return [run for run in self._runs if _run_matches_filter(run, self._run_filter)]

    def _update_action_state(self) -> None:
        run = self._selected_run
        can_pause = bool(run and run.status == RunStatus.running)
        can_resume = bool(run and run.status == RunStatus.paused)
        can_fork = bool(run and self._selected_step)
        for tag, enabled in (
            ("menu_pause", can_pause),
            ("btn_pause", can_pause),
            ("menu_resume", can_resume),
            ("btn_resume", can_resume),
            ("menu_fork", can_fork),
            ("btn_fork", can_fork),
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=enabled)

    def _show_run_detail(self, run: Run) -> None:
        kind_counts: dict[str, int] = {}
        for step in run.steps:
            kind_counts[step.kind.value] = kind_counts.get(step.kind.value, 0) + 1
        stats = _graph_stats(run)
        lines = [
            f"Run: {run.id}",
            f"Model: {run.model_info or '(none)'}",
            f"Status: {run.status.value}",
            f"Created: {_format_timestamp(run.created_at)}",
            f"Steps: {len(run.steps)}",
            f"Step kinds: {_format_counts(kind_counts)}",
            (
                "Graph: "
                f"{stats['roots']} root(s), {stats['links']} link(s), "
                f"{stats['branches']} branch point(s), depth {stats['max_depth']}"
            ),
            f"Cost: ${run.total_cost:.4f}",
            f"Duration: {run.total_duration:.1f}s",
            "",
            "Prompt:",
            f"  {_truncate(run.user_prompt, 700)}",
        ]
        if run.metadata.get("forked_from"):
            lines.append(f"\nForked from: {run.metadata['forked_from']}")
        dpg.set_value("detail_text", "\n".join(lines))

    def _show_step_detail(self, step: Step) -> None:
        parents = ", ".join(step.parent_ids) if step.parent_ids else "(root)"
        lines = [
            f"ID: {step.id}",
            f"Kind: {step.kind.value}",
            f"Parents: {parents}",
            f"Model: {step.model_info or '(none)'}",
            f"Duration: {step.duration:.3f}s",
            f"Cost: ${step.cost:.6f}",
        ]
        if step.tool_info:
            lines.append("")
            lines.append("Tool:")
            lines.extend(_mapping_lines(step.tool_info))
        lines.append("")
        lines.append("Inputs:")
        lines.extend(_mapping_lines(step.inputs))
        lines.append("")
        lines.append("Outputs:")
        lines.extend(_mapping_lines(step.outputs))
        if step.error:
            lines.append("")
            lines.append("Error:")
            lines.extend(_mapping_lines(step.error))
        dpg.set_value("step_text", "\n".join(lines))

    def _clear_dag(self) -> None:
        for child in dpg.get_item_children("dag_editor", slot=1) or []:
            dpg.delete_item(child)
        if dpg.does_item_exist("dag_summary"):
            dpg.set_value("dag_summary", "Select a run to inspect its opentine step graph.")

    def _rebuild_dag(self, run: Run) -> None:
        self._clear_dag()
        dpg.set_value("dag_summary", _dag_summary(run))
        in_attr: dict[str, int] = {}
        out_attr: dict[str, int] = {}
        depth = _step_depths(run)
        by_depth: dict[int, int] = {}
        for step in run.steps:
            d = depth[step.id]
            col = by_depth.get(d, 0)
            by_depth[d] = col + 1
            pos = [80 + d * 240, 40 + col * 110]
            color = STEP_COLORS.get(step.kind, [255, 255, 255])
            label = _node_label(step)
            node_id = dpg.add_node(
                parent="dag_editor",
                label=label,
                pos=pos,
                user_data=step.id,
            )
            dpg.bind_item_theme(node_id, _node_theme(color))

            in_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Input
            )
            dpg.add_text("in", parent=in_id)
            in_attr[step.id] = in_id

            static_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Static
            )
            dpg.add_text(f"{step.duration:.2f}s  ${step.cost:.4f}", parent=static_id)
            dpg.add_button(
                label="inspect",
                parent=static_id,
                user_data=step.id,
                callback=self._on_step_open,
                width=80,
            )

            out_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Output
            )
            dpg.add_text("out", parent=out_id)
            out_attr[step.id] = out_id

        for step in run.steps:
            if step.id not in in_attr:
                continue
            for parent_id in step.parent_ids:
                if parent_id in out_attr:
                    dpg.add_node_link(
                        out_attr[parent_id], in_attr[step.id], parent="dag_editor"
                    )

    def _on_step_open(self, sender, app_data, user_data) -> None:
        if not self._selected_run:
            return
        step = self._selected_run.get_step(user_data)
        if step:
            self._selected_step = step
            self._show_step_detail(step)
            self._update_action_state()

    def _on_link_created(self, sender, app_data) -> None:
        # read-only DAG: discard user-created links
        pass

    def _on_link_deleted(self, sender, app_data) -> None:
        pass

    def _open_dir_picker(self) -> None:
        dpg.set_value("dir_picker_input", str(self._runs_dir))
        dpg.configure_item("dir_picker", show=True)

    def _apply_dir(self) -> None:
        new_dir = Path(dpg.get_value("dir_picker_input")).expanduser()
        self._runs_dir = new_dir
        self._run_filter = ""
        self._step_filter = ""
        self._selected_run = None
        self._selected_step = None
        if dpg.does_item_exist("run_filter"):
            dpg.set_value("run_filter", "")
        if dpg.does_item_exist("step_filter"):
            dpg.set_value("step_filter", "")
        self._persist_preferences()
        dpg.set_value("detail_text", "Select a run")
        dpg.set_value("step_text", "Select a step in the DAG")
        self._clear_dag()
        dpg.configure_item("dir_picker", show=False)
        self._refresh()

    def _pause_selected(self) -> None:
        run = self._selected_run
        if not run or run.status != RunStatus.running:
            self._set_status("Select a running run to pause")
            return
        try:
            path = _safe_run_path(self._runs_dir, run.id)
        except ValueError as e:
            self._set_status(f"Cannot pause: {e}")
            return
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        run.pause(path)
        self._refresh()
        self._set_status(f"Paused {run.id}")

    def _resume_selected(self) -> None:
        run = self._selected_run
        if not run or run.status != RunStatus.paused:
            self._set_status("Select a paused run to resume")
            return
        try:
            path = _safe_run_path(self._runs_dir, run.id)
        except ValueError as e:
            self._set_status(f"Cannot resume: {e}")
            return
        resumed = Run.resume(path)
        resumed.save(path)
        self._selected_run = resumed
        self._refresh()
        self._set_status(f"Resumed {resumed.id}")

    def _fork_selected(self) -> None:
        run = self._selected_run
        step = self._selected_step
        if not run or not step:
            self._set_status("Select a step to fork from")
            return
        new_run = run.fork(step.id)
        try:
            out_path = _safe_run_path(self._runs_dir, new_run.id)
        except ValueError as e:
            self._set_status(f"Cannot fork: {e}")
            return
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        new_run.save(out_path)
        self._selected_run = new_run
        self._selected_step = None
        self._refresh()
        self._set_status(f"Forked {run.id}@{step.id} -> {new_run.id}")

    def _persist_preferences(self) -> None:
        self._preferences["last_runs_dir"] = str(self._runs_dir)
        self._preferences["last_filter"] = self._run_filter
        try:
            _save_preferences(self._preferences)
        except OSError as e:
            self._set_status(f"Preferences not saved: {e}")


def _rgba(color: list[int], alpha: int = 255) -> list[int]:
    return [color[0], color[1], color[2], alpha]


def _app_theme() -> int:
    global _APP_THEME
    if _APP_THEME is not None:
        return _APP_THEME
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            for target, color in (
                (dpg.mvThemeCol_WindowBg, SURFACE_APP),
                (dpg.mvThemeCol_ChildBg, SURFACE_PANEL),
                (dpg.mvThemeCol_PopupBg, SURFACE_CARD),
                (dpg.mvThemeCol_MenuBarBg, SURFACE_SIDEBAR),
                (dpg.mvThemeCol_Text, TEXT_PRIMARY),
                (dpg.mvThemeCol_TextDisabled, TEXT_FAINT),
                (dpg.mvThemeCol_Border, BORDER_DEFAULT),
                (dpg.mvThemeCol_FrameBg, SURFACE_INPUT),
                (dpg.mvThemeCol_FrameBgHovered, STATE_HOVER),
                (dpg.mvThemeCol_FrameBgActive, STATE_ACTIVE),
                (dpg.mvThemeCol_Button, SURFACE_BUTTON),
                (dpg.mvThemeCol_ButtonHovered, STATE_HOVER),
                (dpg.mvThemeCol_ButtonActive, STATE_ACTIVE),
                (dpg.mvThemeCol_Header, STATE_SELECTED),
                (dpg.mvThemeCol_HeaderHovered, STATE_ACTIVE),
                (dpg.mvThemeCol_HeaderActive, STATE_ACTIVE),
                (dpg.mvThemeCol_TableHeaderBg, SURFACE_SIDEBAR),
                (dpg.mvThemeCol_TableBorderStrong, BORDER_STRONG),
                (dpg.mvThemeCol_TableBorderLight, BORDER_DEFAULT),
                (dpg.mvThemeCol_Separator, BORDER_DEFAULT),
                (dpg.mvThemeCol_ScrollbarBg, SURFACE_APP),
                (dpg.mvThemeCol_ScrollbarGrab, SURFACE_BUTTON),
                (dpg.mvThemeCol_CheckMark, BRAND),
            ):
                dpg.add_theme_color(target, _rgba(color), category=dpg.mvThemeCat_Core)
            for target, x, y in (
                (dpg.mvStyleVar_WindowPadding, 12, 10),
                (dpg.mvStyleVar_FramePadding, 8, 5),
                (dpg.mvStyleVar_ItemSpacing, 8, 7),
                (dpg.mvStyleVar_ItemInnerSpacing, 6, 5),
            ):
                dpg.add_theme_style(target, x, y, category=dpg.mvThemeCat_Core)
            for target, value in (
                (dpg.mvStyleVar_WindowBorderSize, 0),
                (dpg.mvStyleVar_ChildBorderSize, 1),
                (dpg.mvStyleVar_FrameRounding, 6),
                (dpg.mvStyleVar_ChildRounding, 8),
                (dpg.mvStyleVar_GrabRounding, 6),
                (dpg.mvStyleVar_ScrollbarSize, 12),
            ):
                dpg.add_theme_style(target, value, category=dpg.mvThemeCat_Core)
    _APP_THEME = theme
    return theme


def _button_theme(kind: str = "ghost") -> int:
    if kind in _BUTTON_THEMES:
        return _BUTTON_THEMES[kind]

    colors = {
        "ghost": (SURFACE_PANEL, STATE_HOVER, STATE_ACTIVE, TEXT_SECONDARY),
        "primary": (BRAND_DIM, BRAND, STATE_ACTIVE, TEXT_PRIMARY),
    }.get(kind, (SURFACE_BUTTON, STATE_HOVER, STATE_ACTIVE, TEXT_PRIMARY))

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(
                dpg.mvThemeCol_Button,
                _rgba(colors[0]),
                category=dpg.mvThemeCat_Core,
            )
            dpg.add_theme_color(
                dpg.mvThemeCol_ButtonHovered, _rgba(colors[1]), category=dpg.mvThemeCat_Core
            )
            dpg.add_theme_color(
                dpg.mvThemeCol_ButtonActive, _rgba(colors[2]), category=dpg.mvThemeCat_Core
            )
            dpg.add_theme_color(dpg.mvThemeCol_Text, _rgba(colors[3]), category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 4, category=dpg.mvThemeCat_Core)
    _BUTTON_THEMES[kind] = theme
    return theme


def _panel_header(title: str, subtitle: str) -> None:
    dpg.add_text(title, color=TEXT_PRIMARY)
    dpg.add_text(subtitle, color=TEXT_MUTED)


def _action_button(label: str, callback, tag: str) -> int | str:
    item = dpg.add_button(label=label, callback=callback, tag=tag, width=96)
    dpg.bind_item_theme(item, _button_theme("ghost"))
    return item


_NODE_THEMES: dict[tuple[int, int, int], int] = {}


def _node_theme(color: list[int]) -> int:
    key = (color[0], color[1], color[2])
    if key in _NODE_THEMES:
        return _NODE_THEMES[key]
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            dpg.add_theme_color(
                dpg.mvNodeCol_TitleBar, color, category=dpg.mvThemeCat_Nodes
            )
            dpg.add_theme_color(
                dpg.mvNodeCol_TitleBarHovered,
                [min(255, c + 30) for c in color],
                category=dpg.mvThemeCat_Nodes,
            )
            dpg.add_theme_color(
                dpg.mvNodeCol_TitleBarSelected,
                [min(255, c + 50) for c in color],
                category=dpg.mvThemeCat_Nodes,
            )
    _NODE_THEMES[key] = theme
    return theme


def _node_label(step: Step, *, highlighted: bool = False) -> str:
    kind = step.kind.value
    prefix = "* " if highlighted else ""
    if step.kind == StepKind.tool:
        name = step.tool_info.get("name") or step.inputs.get("name", "?")
        return f"{prefix}{kind}: {_truncate(name, 34)}"
    if step.kind == StepKind.error:
        text = (
            step.error.get("message")
            or step.error.get("type")
            or step.inputs.get("message")
            or step.outputs.get("error")
            or step.inputs.get("text")
            or ""
        )
    elif step.kind == StepKind.done:
        text = (
            step.outputs.get("answer")
            or step.outputs.get("text")
            or step.inputs.get("text")
            or ""
        )
    elif step.kind == StepKind.model:
        text = step.outputs.get("text") or step.inputs.get("text") or ""
    else:
        text = step.inputs.get("text") or ""
    if text:
        return f"{prefix}{kind}: {_truncate(text, 40)}"
    return f"{prefix}{kind}: {step.id[:8]}"


def _step_depths(run: Run) -> dict[str, int]:
    by_id = {step.id: step for step in run.steps}
    memo: dict[str, int] = {}

    def depth(step: Step, seen: set[str]) -> int:
        if step.id in memo:
            return memo[step.id]
        parents = [p for p in step.parent_ids if p in by_id and p != step.id]
        if not parents or step.id in seen:
            memo[step.id] = 0
            return 0
        seen.add(step.id)
        memo[step.id] = 1 + max(depth(by_id[p], seen) for p in parents)
        seen.remove(step.id)
        return memo[step.id]

    for step in run.steps:
        depth(step, set())
    return memo


def _graph_stats(run: Run) -> dict[str, int]:
    step_ids = {step.id for step in run.steps}
    child_counts: dict[str, int] = {}
    links = 0
    roots = 0
    for step in run.steps:
        parents = [p for p in step.parent_ids if p in step_ids]
        if parents:
            for parent_id in parents:
                links += 1
                child_counts[parent_id] = child_counts.get(parent_id, 0) + 1
        else:
            roots += 1
    depths = _step_depths(run)
    return {
        "roots": roots,
        "links": links,
        "branches": sum(1 for count in child_counts.values() if count > 1),
        "max_depth": max(depths.values(), default=0),
    }


def _run_list_summary(runs: list[Run], visible_runs: list[Run], query: str) -> str:
    if not runs:
        return "No .tine runs loaded. Change directory or wait for agents to write runs."
    status_counts: dict[str, int] = {}
    for run in visible_runs:
        status_counts[run.status.value] = status_counts.get(run.status.value, 0) + 1
    total_cost = sum(run.total_cost for run in visible_runs)
    shown = f"{len(visible_runs)}/{len(runs)} shown" if query else f"{len(runs)} run(s)"
    counts = _format_counts(status_counts)
    return f"{shown} - {counts} - visible cost ${total_cost:.4f}"


def _dag_summary(run: Run, query: str = "", matches: set[str] | None = None) -> str:
    stats = _graph_stats(run)
    summary = (
        f"{len(run.steps)} step(s), {stats['links']} link(s), "
        f"{stats['branches']} branch point(s), depth {stats['max_depth']}"
    )
    if not query:
        return summary
    matched = matches or set(_matching_steps(run, query))
    return f"{summary} - {len(matched)}/{len(run.steps)} match query '{query}'"


def _matching_steps(run: Run | None, query: str) -> list[str]:
    if not run or not query:
        return []
    return [step.id for step in run.steps if _step_matches_filter(step, query)]


def _highlight_summary(run: Run, matches: set[str]) -> str:
    if not matches:
        return "No matching steps"
    labels = [
        _truncate(_node_label(step).replace("* ", "", 1), 48)
        for step in run.steps
        if step.id in matches
    ]
    return "Matches: " + ", ".join(labels[:6])


def _step_matches_filter(step: Step, query: str) -> bool:
    haystack = [
        step.id,
        step.kind.value,
        " ".join(step.parent_ids),
        step.model_info,
        _format_value(step.inputs, 500),
        _format_value(step.outputs, 500),
        _format_value(step.tool_info, 500),
        _format_value(step.error, 500),
    ]
    return query in "\n".join(haystack).lower()


def _run_matches_filter(run: Run, query: str) -> bool:
    if not query:
        return True
    haystack = [
        run.id,
        run.status.value,
        run.model_info,
        run.user_prompt,
        str(run.metadata.get("forked_from", "")),
    ]
    for step in run.steps:
        haystack.extend(
            [
                step.id,
                step.kind.value,
                " ".join(step.parent_ids),
                step.model_info,
                _format_value(step.inputs, 500),
                _format_value(step.outputs, 500),
                _format_value(step.tool_info, 500),
                _format_value(step.error, 500),
            ]
        )
    return query in "\n".join(haystack).lower()


def _mapping_lines(data: dict, *, limit: int = 700) -> list[str]:
    if not data:
        return ["  (none)"]
    lines: list[str] = []
    for key, value in data.items():
        formatted = _format_value(value, limit)
        if "\n" in formatted:
            lines.append(f"  {key}:")
            lines.extend(f"    {line}" for line in formatted.splitlines())
        else:
            lines.append(f"  {key}: {formatted}")
    return lines


def _format_value(value: object, limit: int) -> str:
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        rendered = str(value)
    return _truncate(rendered, limit)


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return ", ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))


def _format_timestamp(timestamp: float) -> str:
    if not timestamp:
        return "(unknown)"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _truncate(v: object, n: int) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def run_app(runs_dir: Path | str | None = None) -> None:
    gui = OpentineGUI(Path(runs_dir) if runs_dir is not None else None)
    gui.run()
