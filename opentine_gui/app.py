"""Dear PyGui application — 3-panel desktop dashboard for opentine runs.

Layout:
  Left:   Run list (table)
  Center: Step detail (text)
  Right:  DAG visualization (imnodes)
"""

from __future__ import annotations

from pathlib import Path

import dearpygui.dearpygui as dpg

from opentine.core import Run, StepKind

BRAND = [255, 105, 0]
BRAND_DIM = [204, 85, 0]

STEP_COLORS = {
    StepKind.think: [255, 255, 100],
    StepKind.tool: BRAND,
    StepKind.model: [100, 200, 255],
    StepKind.done: [100, 200, 100],
    StepKind.error: [255, 80, 80],
}

RUNS_DIR = Path(".tine_runs")


def _load_runs() -> list[Run]:
    runs = []
    if RUNS_DIR.exists():
        for f in sorted(RUNS_DIR.glob("*.tine"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                runs.append(Run.load(f))
            except Exception:
                pass
    return runs


class OpentineGUI:
    def __init__(self) -> None:
        self._runs: list[Run] = []
        self._selected: Run | None = None

    def run(self) -> None:
        dpg.create_context()
        dpg.create_viewport(title="opentine — agent run console", width=1200, height=700)
        dpg.set_global_font_scale(1.0)

        with dpg.window(tag="primary"):
            with dpg.menu_bar():
                dpg.add_menu_item(label="Refresh", callback=self._refresh)
                dpg.add_menu_item(label="Quit", callback=lambda: dpg.stop_dearpygui())

            with dpg.group(horizontal=True):
                self._build_run_list()
                self._build_detail_panel()
                self._build_dag_panel()

        self._refresh()
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.start_dearpygui()
        dpg.destroy_context()

    def _build_run_list(self) -> None:
        with dpg.group(width=300):
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

    def _build_detail_panel(self) -> None:
        with dpg.group(width=400):
            dpg.add_text("Details", color=BRAND)
            dpg.add_separator()
            dpg.add_text("Select a run", tag="detail_text", wrap=380)

    def _build_dag_panel(self) -> None:
        with dpg.group(width=500):
            dpg.add_text("Step DAG", color=BRAND)
            dpg.add_separator()
            dpg.add_text("Select a run to view DAG", tag="dag_text", wrap=480)

    def _refresh(self) -> None:
        self._runs = _load_runs()
        if dpg.does_item_exist("run_table"):
            for child in dpg.get_item_children("run_table", slot=1):
                dpg.delete_item(child)
            for run in self._runs:
                with dpg.table_row(parent="run_table"):
                    status_color = STEP_COLORS.get(
                        StepKind.done if run.status.value == "completed" else StepKind.error,
                        [255, 255, 255],
                    )
                    if run.status.value == "running":
                        status_color = [0, 188, 212]
                    elif run.status.value == "paused":
                        status_color = [255, 152, 0]
                    dpg.add_text(run.id)
                    dpg.add_text(run.status.value, color=status_color)
                    dpg.add_text(f"${run.total_cost:.4f}")

    def _on_run_click(self, sender, app_data) -> None:
        row_idx = app_data[0]
        if row_idx >= len(self._runs):
            return
        self._selected = self._runs[row_idx]
        self._show_detail(self._selected)
        self._show_dag(self._selected)

    def _show_detail(self, run: Run) -> None:
        lines = [
            f"Run: {run.id}",
            f"Model: {run.model_info}",
            f"Status: {run.status.value}",
            f"Steps: {len(run.steps)}",
            f"Cost: ${run.total_cost:.4f}",
            f"Duration: {run.total_duration:.1f}s",
            "",
            "Prompt:",
            f"  {run.user_prompt[:300]}",
        ]
        if run.metadata.get("forked_from"):
            lines.append(f"\nForked from: {run.metadata['forked_from']}")
        dpg.set_value("detail_text", "\n".join(lines))

    def _show_dag(self, run: Run) -> None:
        lines = []
        for i, step in enumerate(run.steps):
            color = STEP_COLORS.get(step.kind, [255, 255, 255])
            kind = step.kind.value
            text = step.inputs.get("text", "")
            name = step.inputs.get("name", "")

            if step.kind == StepKind.tool:
                args = step.inputs.get("arguments", {})
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
                label = f"{kind}: {name}({args_str})"
            elif text:
                label = f"{kind}: {text[:50]}"
            else:
                label = f"{kind}: {step.id}"

            parent_info = f" <- {step.parent_id[:8]}" if step.parent_id else ""
            lines.append(f"[{i}] {label}{parent_info}")

        dpg.set_value("dag_text", "\n".join(lines) if lines else "(no steps)")


def run_app() -> None:
    gui = OpentineGUI()
    gui.run()
