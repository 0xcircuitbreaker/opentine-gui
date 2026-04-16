"""Dear PyGui application — 3-panel desktop dashboard for opentine runs.

Layout:
  Left:   Run list (table) + actions
  Center: Run detail + selected-step detail
  Right:  DAG node editor (parent -> child)
"""

from __future__ import annotations

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

BRAND = [255, 105, 0]
BRAND_DIM = [204, 85, 0]

STEP_COLORS: dict[StepKind, list[int]] = {
    StepKind.think: [255, 255, 100],
    StepKind.tool: BRAND,
    StepKind.model: [100, 200, 255],
    StepKind.done: [100, 200, 100],
    StepKind.error: [255, 80, 80],
}

RUN_STATUS_COLORS: dict[RunStatus, list[int]] = {
    RunStatus.running: [0, 188, 212],
    RunStatus.paused: [255, 152, 0],
    RunStatus.completed: [100, 200, 100],
    RunStatus.failed: [255, 80, 80],
}

DEFAULT_RUNS_DIR = Path(".tine_runs")
AUTO_REFRESH_SECONDS = 2.0


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

    def run(self) -> None:
        dpg.create_context()
        dpg.create_viewport(title="opentine — agent run console", width=1400, height=800)

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
                dpg.add_text("", tag="status_bar", color=[180, 180, 180])

            with dpg.group(horizontal=True):
                self._build_run_list()
                self._build_detail_panel()
                self._build_dag_panel()

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

    def _build_run_list(self) -> None:
        with dpg.child_window(width=320, border=False):
            dpg.add_text("Runs", color=BRAND)
            dpg.add_separator()
            with dpg.table(
                header_row=True,
                resizable=False,
                policy=dpg.mvTable_SizingStretchProp,
                callback=self._on_run_click,
                tag="run_table",
            ):
                dpg.add_table_column(label="ID")
                dpg.add_table_column(label="Status")
                dpg.add_table_column(label="Cost")
            dpg.add_separator()
            dpg.add_text("Load errors:", color=[255, 180, 80], tag="err_header", show=False)
            dpg.add_text("", tag="err_text", wrap=300, color=[255, 180, 80])

    def _build_detail_panel(self) -> None:
        with dpg.child_window(width=460, border=False):
            dpg.add_text("Run", color=BRAND)
            dpg.add_separator()
            dpg.add_text("Select a run", tag="detail_text", wrap=440)
            dpg.add_spacer(height=10)
            dpg.add_text("Step", color=BRAND)
            dpg.add_separator()
            dpg.add_text("Select a step in the DAG", tag="step_text", wrap=440)

    def _build_dag_panel(self) -> None:
        with dpg.child_window(border=False):
            dpg.add_text("Step DAG", color=BRAND)
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
        self._render_run_table()
        self._render_errors()
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
        self._set_status(
            f"{self._runs_dir} — {len(self._runs)} run(s)"
            + (f", {len(self._errors)} error(s)" if self._errors else "")
        )

    def _render_run_table(self) -> None:
        if not dpg.does_item_exist("run_table"):
            return
        for child in dpg.get_item_children("run_table", slot=1) or []:
            dpg.delete_item(child)
        for run in self._runs:
            with dpg.table_row(parent="run_table"):
                color = RUN_STATUS_COLORS.get(run.status, [255, 255, 255])
                dpg.add_text(run.id)
                dpg.add_text(run.status.value, color=color)
                dpg.add_text(f"${run.total_cost:.4f}")

    def _render_errors(self) -> None:
        if self._errors:
            dpg.configure_item("err_header", show=True)
            dpg.set_value("err_text", "\n".join(self._errors[:10]))
        else:
            dpg.configure_item("err_header", show=False)
            dpg.set_value("err_text", "")

    def _on_run_click(self, sender, app_data) -> None:
        row_idx = app_data[0] if app_data else 0
        if row_idx >= len(self._runs):
            return
        self._selected_run = self._runs[row_idx]
        self._selected_step = None
        self._show_run_detail(self._selected_run)
        dpg.set_value("step_text", "Select a step in the DAG")
        self._rebuild_dag(self._selected_run)

    def _show_run_detail(self, run: Run) -> None:
        lines = [
            f"Run: {run.id}",
            f"Model: {run.model_info}",
            f"Status: {run.status.value}",
            f"Steps: {len(run.steps)}",
            f"Cost: ${run.total_cost:.4f}",
            f"Duration: {run.total_duration:.1f}s",
            "",
            "Prompt:",
            f"  {run.user_prompt[:500]}",
        ]
        if run.metadata.get("forked_from"):
            lines.append(f"\nForked from: {run.metadata['forked_from']}")
        dpg.set_value("detail_text", "\n".join(lines))

    def _show_step_detail(self, step: Step) -> None:
        lines = [
            f"ID: {step.id}",
            f"Kind: {step.kind.value}",
            f"Parent: {step.parent_id or '(root)'}",
            f"Model: {step.model_info or '—'}",
            f"Duration: {step.duration:.3f}s",
            f"Cost: ${step.cost:.6f}",
            "",
            "Inputs:",
        ]
        for k, v in step.inputs.items():
            lines.append(f"  {k}: {_truncate(v, 400)}")
        lines.append("")
        lines.append("Outputs:")
        for k, v in step.outputs.items():
            lines.append(f"  {k}: {_truncate(v, 400)}")
        dpg.set_value("step_text", "\n".join(lines))

    def _clear_dag(self) -> None:
        for child in dpg.get_item_children("dag_editor", slot=1) or []:
            dpg.delete_item(child)

    def _rebuild_dag(self, run: Run) -> None:
        self._clear_dag()
        node_tags: dict[str, int] = {}
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
            node_tags[step.id] = node_id
            dpg.bind_item_theme(node_id, _node_theme(color))

            in_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Input
            )
            dpg.add_text("in", parent=in_id)
            in_attr[step.id] = in_id

            static_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Static
            )
            dpg.add_button(
                label="open",
                parent=static_id,
                user_data=step.id,
                callback=self._on_step_open,
                width=60,
            )

            out_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Output
            )
            dpg.add_text("out", parent=out_id)
            out_attr[step.id] = out_id

        for step in run.steps:
            if step.parent_id and step.parent_id in out_attr and step.id in in_attr:
                dpg.add_node_link(out_attr[step.parent_id], in_attr[step.id], parent="dag_editor")

    def _on_step_open(self, sender, app_data, user_data) -> None:
        if not self._selected_run:
            return
        step = self._selected_run.get_step(user_data)
        if step:
            self._selected_step = step
            self._show_step_detail(step)

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
        self._selected_run = None
        self._selected_step = None
        dpg.set_value("detail_text", "Select a run")
        dpg.set_value("step_text", "Select a step in the DAG")
        self._clear_dag()
        dpg.configure_item("dir_picker", show=False)
        self._refresh()

    def _pause_selected(self) -> None:
        run = self._selected_run
        if not run or run.status != RunStatus.running:
            return
        try:
            path = _safe_run_path(self._runs_dir, run.id)
        except ValueError as e:
            self._set_status(f"Cannot pause: {e}")
            return
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        run.pause(path)
        self._refresh()

    def _resume_selected(self) -> None:
        run = self._selected_run
        if not run or run.status != RunStatus.paused:
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
        self._refresh()


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


def _truncate(v: object, n: int) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def run_app(runs_dir: Path | str = DEFAULT_RUNS_DIR) -> None:
    gui = OpentineGUI(Path(runs_dir))
    gui.run()
