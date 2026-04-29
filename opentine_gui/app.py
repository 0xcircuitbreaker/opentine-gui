"""Dear PyGui application — 3-panel desktop dashboard for opentine runs.

Layout:
  Left:   Run list (table) + actions
  Center: Run detail + selected-step detail
  Right:  DAG node editor (parent -> child)
"""

from __future__ import annotations

import json
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
    def __init__(self, runs_dir: Path = DEFAULT_RUNS_DIR) -> None:
        self._runs_dir = runs_dir
        self._runs: list[Run] = []
        self._errors: list[str] = []
        self._selected_run: Run | None = None
        self._selected_step: Step | None = None
        self._last_signature: tuple = ()
        self._last_check: float = 0.0
        self._run_filter = ""

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
                width=-1,
                callback=self._on_filter_change,
            )
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
        if not visible_runs:
            with dpg.table_row(parent="run_table"):
                dpg.add_text("No runs match filter", color=[150, 150, 150])
                dpg.add_text("")
                dpg.add_text("")

    def _render_errors(self) -> None:
        if self._errors:
            dpg.configure_item("err_header", show=True)
            dpg.set_value("err_text", "\n".join(self._errors[:10]))
        else:
            dpg.configure_item("err_header", show=False)
            dpg.set_value("err_text", "")

    def _on_run_click(self, sender, app_data) -> None:
        row_idx = app_data[0] if app_data else 0
        visible_runs = self._filtered_runs()
        if row_idx >= len(visible_runs):
            return
        self._select_run(visible_runs[row_idx].id)

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
        self._render_run_table()
        self._update_action_state()
        shown = len(self._filtered_runs())
        self._set_status(f"{self._runs_dir} - {shown}/{len(self._runs)} run(s) shown")

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
        lines = [
            f"Run: {run.id}",
            f"Model: {run.model_info or '(none)'}",
            f"Status: {run.status.value}",
            f"Created: {_format_timestamp(run.created_at)}",
            f"Steps: {len(run.steps)}",
            f"Step kinds: {_format_counts(kind_counts)}",
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
        lines = [
            f"ID: {step.id}",
            f"Kind: {step.kind.value}",
            f"Parent: {step.parent_id or '(root)'}",
            f"Model: {step.model_info or '(none)'}",
            f"Duration: {step.duration:.3f}s",
            f"Cost: ${step.cost:.6f}",
            "",
            "Inputs:",
        ]
        lines.extend(_mapping_lines(step.inputs))
        lines.append("")
        lines.append("Outputs:")
        lines.extend(_mapping_lines(step.outputs))
        dpg.set_value("step_text", "\n".join(lines))

    def _clear_dag(self) -> None:
        for child in dpg.get_item_children("dag_editor", slot=1) or []:
            dpg.delete_item(child)

    def _rebuild_dag(self, run: Run) -> None:
        self._clear_dag()
        in_attr: dict[str, int] = {}
        out_attr: dict[str, int] = {}
        depth: dict[str, int] = {}
        for step in run.steps:
            d = 0 if step.parent_id is None else depth.get(step.parent_id, 0) + 1
            depth[step.id] = d
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
            if step.parent_id and step.parent_id in out_attr and step.id in in_attr:
                dpg.add_node_link(
                    out_attr[step.parent_id], in_attr[step.id], parent="dag_editor"
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
        self._selected_run = None
        self._selected_step = None
        if dpg.does_item_exist("run_filter"):
            dpg.set_value("run_filter", "")
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


def _node_label(step: Step) -> str:
    kind = step.kind.value
    if step.kind == StepKind.tool:
        name = step.inputs.get("name", "?")
        return f"{kind}: {name}"
    text = step.inputs.get("text") or ""
    if text:
        return f"{kind}: {_truncate(text, 40)}"
    return f"{kind}: {step.id[:8]}"


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
                step.parent_id or "",
                step.model_info,
                _format_value(step.inputs, 500),
                _format_value(step.outputs, 500),
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


def run_app(runs_dir: Path | str = DEFAULT_RUNS_DIR) -> None:
    gui = OpentineGUI(Path(runs_dir))
    gui.run()
