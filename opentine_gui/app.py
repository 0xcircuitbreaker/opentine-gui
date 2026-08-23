"""Dear PyGui application — the opentine run console.

Layout:
  Left:   Run list (search, table) + actions
  Center: Run detail + selected-step detail
  Right:  Step DAG node editor (lineage and causal edges)
  Bottom: Status line and a message log that does not scroll away

Two invariants shape the code below.

*Only the render thread touches Dear PyGui.* `manual_callback_management` puts
every widget callback on a queue that this module's own loop drains between
frames, so callbacks may build and delete items freely. The directory scan and
artifact parsing run on a worker thread instead, and hand back plain data.

*Everything an artifact says is untrusted.* Text from a run reaches a widget
only through `opentine_gui.text`, and the console's own verdicts (integrity,
signature, fork identity) are rendered so that no artifact-supplied string can
be mistaken for one.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import dearpygui.dearpygui as dpg
from opentine.core import Run, RunStatus, Step, StepKind

from opentine_gui import __version__ as _GUI_VERSION
from opentine_gui import otelio, pricing, stats, trust
from opentine_gui.desktop import (
    EXTRA_GLYPH_RANGES,
    FONT_SIZE,
    _detect_ui_scale,
    _expand_user,
    _find_ui_font,
    _load_preferences,
    _preferences_path,
    _px,
    _recent_dirs,
    _remember_dir,
    _save_preferences,
    _viewport_geometry,
    _windows_set_dpi_aware,
    set_ui_scale,
)
from opentine_gui.graphmodel import (
    _matching_steps,
    _node_label,
    _node_subtitle,
    _step_depths,
    causal_edges,
    retained_slice,
)
from opentine_gui.inspectors import (
    _cost_cell,
    _cost_text,
    _dag_summary,
    _format_run_diff,
    _highlight_summary,
    _load_problem_header,
    _run_detail_lines,
    _run_list_summary,
    _split_load_problems,
    _step_detail_lines,
    _transcript_heading,
    _transcript_summary,
    _transcript_turns,
    _trust_lines,
)
from opentine_gui.query import (
    _query_error,
    _run_matches_filter,
)
from opentine_gui.sources import (
    RunEntry,
    Snapshot,
    _export_path,
    _forget_run,
    _safe_run_path,
    _short_oid,
    _verify_cached,
    _verify_integrity_cached,
    open_source,
)
from opentine_gui.text import (
    _elide_middle,
    _format_age,
    _format_bytes,
    _format_timestamp,
    _format_value,
    _indent_block,
    _oneline,
    _sanitize,
    _truncate,
)
from opentine_gui.theme import (
    ACCENT_ORANGE,
    ACCENT_PURPLE,
    ACCENT_RED,
    BORDER_DEFAULT,
    BORDER_STRONG,
    BRAND,
    BRAND_DIM,
    LEVEL_COLORS,
    RUN_STATUS_COLORS,
    STATE_ACTIVE,
    STATE_HOVER,
    STATE_SELECTED,
    STEP_COLORS,
    SURFACE_APP,
    SURFACE_BUTTON,
    SURFACE_CARD,
    SURFACE_INPUT,
    SURFACE_PANEL,
    SURFACE_SIDEBAR,
    TEXT_FAINT,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TRANSCRIPT_ROLE_COLORS,
    _brighten,
    _dim,
    _rgba,
)

try:  # the installed opentine's version, for the About box and the status bar
    from opentine import __version__ as _OPENTINE_VERSION
except Exception:  # pragma: no cover - depends on the installed opentine
    _OPENTINE_VERSION = "unknown"

try:
    #: opentine 0.5.0+, so the 0.7.2 floor guarantees it. Kept as a name here so
    #: a future opentine that reshapes it costs the export action, not the app.
    from opentine import to_otel_genai_document
except Exception:  # pragma: no cover - depends on the installed opentine
    to_otel_genai_document = None

DEFAULT_RUNS_DIR = Path(".tine_runs")
AUTO_REFRESH_SECONDS = 2.0
MAX_FORK_REASON = 4096
#: Kept in one place so a modal and its centering maths cannot drift apart.
FORK_DIALOG_SIZE = (600, 400)
DIFF_DIALOG_SIZE = (820, 600)
TRANSCRIPT_DIALOG_SIZE = (860, 640)
TEXT_DIALOG_SIZE = (820, 600)
PANEL_DIALOG_SIZE = (760, 560)
NODE_PITCH_X = 250
NODE_PITCH_Y = 170
#: Roughly five Dear PyGui items go into every node. A legal run can hold
#: ~15,900 steps inside MAX_TINE_BYTES, which would be ~80,000 items built in
#: one frame; the graph is drawn up to this many steps and says what it left out.
MAX_DAG_NODES = 400
#: The run table is rebuilt from scratch on every render, so it is bounded too.
MAX_TABLE_ROWS = 500
#: One assembled row of a text panel. Five different metadata fields can each
#: carry a 9-million-character string out of a file well under MAX_TINE_BYTES,
#: and bounding them one at a time closes one hole at a time. The panel bounds
#: the row instead: the prompt blocks are already capped at 700/400, so nothing
#: legitimate reaches this.
MAX_PANEL_ROW = 2000
#: A transcript is artifact-supplied and unbounded: one turn becomes a heading,
#: a button and a text block, so a 20,000-turn conversation would build ~60,000
#: widgets in the frame that opens the dialog.
MAX_TRANSCRIPT_TURNS = 500
#: Messages kept in the log panel. Old ones scroll off, they do not vanish
#: behind the next auto-refresh the way a single status line does.
MAX_MESSAGES = 200
#: A filter keystroke should not re-scan every payload of every run.
FILTER_DEBOUNCE_SECONDS = 0.15
#: Preferences are written to disk, so they are flushed on a pause in typing
#: rather than on every character.
PREFERENCES_FLUSH_SECONDS = 1.5

TABLE_COLUMNS = ("id", "status", "model", "steps", "cost", "age")

#: The pricing panel's "use what the artifact recorded" option, as opposed to
#: assuming a provider for steps that recorded none.
RECORDED_PROVIDER = "(as recorded)"

_APP_THEME: int | None = None
_BUTTON_THEMES: dict[str, int] = {}
_NODE_THEMES: dict[tuple[int, int, int, bool], int] = {}
_LINK_THEMES: dict[str, int] = {}


def _reset_theme_caches() -> None:
    """Theme ids die with their DPG context; a second run() must not reuse them."""
    global _APP_THEME
    _APP_THEME = None
    _BUTTON_THEMES.clear()
    _NODE_THEMES.clear()
    _LINK_THEMES.clear()


# --------------------------------------------------------------------- themes


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
                (dpg.mvThemeCol_TableRowBgAlt, SURFACE_CARD),
                (dpg.mvThemeCol_Separator, BORDER_DEFAULT),
                (dpg.mvThemeCol_ScrollbarBg, SURFACE_APP),
                (dpg.mvThemeCol_ScrollbarGrab, SURFACE_BUTTON),
                (dpg.mvThemeCol_CheckMark, BRAND),
                (dpg.mvThemeCol_Tab, SURFACE_PANEL),
                (dpg.mvThemeCol_TabHovered, STATE_HOVER),
                (dpg.mvThemeCol_TabActive, STATE_SELECTED),
            ):
                dpg.add_theme_color(target, _rgba(color), category=dpg.mvThemeCat_Core)
            # Padding, spacing and scrollbars are sizes too: leaving them at 100%
            # while text and panels scale makes a HiDPI window look cramped.
            for target, x, y in (
                (dpg.mvStyleVar_WindowPadding, 12, 10),
                (dpg.mvStyleVar_FramePadding, 8, 5),
                (dpg.mvStyleVar_ItemSpacing, 8, 7),
                (dpg.mvStyleVar_ItemInnerSpacing, 6, 5),
                (dpg.mvStyleVar_CellPadding, 6, 4),
            ):
                dpg.add_theme_style(target, _px(x), _px(y), category=dpg.mvThemeCat_Core)
            for target, value, scaled in (
                (dpg.mvStyleVar_WindowBorderSize, 0, False),
                (dpg.mvStyleVar_ChildBorderSize, 1, False),
                (dpg.mvStyleVar_FrameRounding, 6, True),
                (dpg.mvStyleVar_ChildRounding, 8, True),
                (dpg.mvStyleVar_GrabRounding, 6, True),
                (dpg.mvStyleVar_ScrollbarSize, 12, True),
            ):
                dpg.add_theme_style(
                    target, _px(value) if scaled else value, category=dpg.mvThemeCat_Core
                )
        with dpg.theme_component(dpg.mvButton, enabled_state=False):
            dpg.add_theme_color(dpg.mvThemeCol_Button, _rgba(SURFACE_PANEL))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _rgba(SURFACE_PANEL))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _rgba(SURFACE_PANEL))
            dpg.add_theme_color(dpg.mvThemeCol_Text, _rgba(TEXT_FAINT))
        with dpg.theme_component(dpg.mvMenuItem, enabled_state=False):
            dpg.add_theme_color(dpg.mvThemeCol_Text, _rgba(TEXT_FAINT))
    _APP_THEME = theme
    return theme


def _button_theme(kind: str = "ghost") -> int:
    if kind in _BUTTON_THEMES:
        return _BUTTON_THEMES[kind]

    colors = {
        "ghost": (SURFACE_BUTTON, STATE_HOVER, STATE_ACTIVE, TEXT_PRIMARY),
        "primary": (BRAND_DIM, BRAND, STATE_ACTIVE, TEXT_PRIMARY),
        "danger": (SURFACE_BUTTON, ACCENT_RED, STATE_ACTIVE, ACCENT_RED),
    }.get(kind, (SURFACE_BUTTON, STATE_HOVER, STATE_ACTIVE, TEXT_PRIMARY))

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvButton):
            for target, color in (
                (dpg.mvThemeCol_Button, colors[0]),
                (dpg.mvThemeCol_ButtonHovered, colors[1]),
                (dpg.mvThemeCol_ButtonActive, colors[2]),
                (dpg.mvThemeCol_Text, colors[3]),
            ):
                dpg.add_theme_color(target, _rgba(color), category=dpg.mvThemeCat_Core)
            # Scaled like _app_theme's: an item theme overrides the global one,
            # so leaving these raw would un-scale every action button at HiDPI.
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, _px(6), category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(
                dpg.mvStyleVar_FramePadding, _px(8), _px(4), category=dpg.mvThemeCat_Core
            )
        with dpg.theme_component(dpg.mvButton, enabled_state=False):
            dpg.add_theme_color(dpg.mvThemeCol_Button, _rgba(SURFACE_PANEL))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _rgba(SURFACE_PANEL))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _rgba(SURFACE_PANEL))
            dpg.add_theme_color(dpg.mvThemeCol_Text, _rgba(TEXT_FAINT))
    _BUTTON_THEMES[kind] = theme
    return theme


def _node_theme(color: list[int], *, highlighted: bool = False) -> int:
    key = (color[0], color[1], color[2], highlighted)
    if key in _NODE_THEMES:
        return _NODE_THEMES[key]
    # Title bars use a dimmed accent so the light text stays readable; the
    # full-brightness accent is reserved for the highlight outline.
    title_factor = 0.62 if highlighted else 0.42
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNode):
            dpg.add_theme_color(
                dpg.mvNodeCol_TitleBar, _dim(color, title_factor), category=dpg.mvThemeCat_Nodes
            )
            dpg.add_theme_color(
                dpg.mvNodeCol_TitleBarHovered,
                _dim(color, title_factor + 0.14),
                category=dpg.mvThemeCat_Nodes,
            )
            dpg.add_theme_color(
                dpg.mvNodeCol_TitleBarSelected,
                _dim(color, title_factor + 0.26),
                category=dpg.mvThemeCat_Nodes,
            )
            if highlighted:
                dpg.add_theme_color(
                    dpg.mvNodeCol_NodeOutline,
                    _brighten(color, 40),
                    category=dpg.mvThemeCat_Nodes,
                )
                dpg.add_theme_style(
                    dpg.mvNodeStyleVar_NodeBorderThickness, 2, category=dpg.mvThemeCat_Nodes
                )
    _NODE_THEMES[key] = theme
    return theme


def _link_theme(kind: str) -> int:
    """Lineage links and causal links must not read as the same relationship.

    A causal edge says "this step needed that one", not "that one ran before
    this one". opentine's fork follows both, so both are drawn, in different
    colours and weights.
    """
    if kind in _LINK_THEMES:
        return _LINK_THEMES[kind]
    color = TEXT_MUTED if kind == "parent" else ACCENT_PURPLE
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvNodeLink):
            dpg.add_theme_color(dpg.mvNodeCol_Link, _rgba(color), category=dpg.mvThemeCat_Nodes)
            dpg.add_theme_color(
                dpg.mvNodeCol_LinkHovered,
                _rgba(_brighten(color, 40)),
                category=dpg.mvThemeCat_Nodes,
            )
            dpg.add_theme_style(
                dpg.mvNodeStyleVar_LinkThickness,
                2.0 if kind == "parent" else 1.0,
                category=dpg.mvThemeCat_Nodes,
            )
    _LINK_THEMES[kind] = theme
    return theme


def _panel_header(title: str, subtitle: str, subtitle_tag: str | None = None) -> None:
    dpg.add_text(title, color=TEXT_PRIMARY)
    if subtitle_tag:
        dpg.add_text(subtitle, color=TEXT_MUTED, tag=subtitle_tag)
    else:
        dpg.add_text(subtitle, color=TEXT_MUTED)


def _action_button(label: str, callback, tag: str, width: int = 96, kind: str = "ghost"):
    # DPG 2.x buttons ignore enabled= for click-blocking; a disabled wrapping
    # group both swallows clicks and applies the disabled styling.
    with dpg.group(tag=f"{tag}_wrap"):
        # width=0 lets Dear PyGui size the button to its label.
        item = dpg.add_button(
            label=label, callback=callback, tag=tag, width=_px(width) if width else 0
        )
    dpg.bind_item_theme(item, _button_theme(kind))
    return item


def _hint(parent: int | str, text: str) -> None:
    """A hover explanation. Used where a control's own label cannot say enough."""
    with dpg.tooltip(parent):
        dpg.add_text(text, wrap=_px(360))


# ------------------------------------------------------------ background load


@dataclass
class _Message:
    level: str
    text: str
    at: float
    #: How many times in a row this same line was reported.
    repeats: int = 1


class _Loader:
    """Scans the source on a worker thread and hands back finished snapshots.

    Reading a directory means parsing every artifact and hashing every file, and
    a v3 repository read is dearer still. Doing that inside the frame loop made
    the whole console stall on every refresh tick; doing it here keeps the UI at
    frame rate. The worker never touches Dear PyGui: it only puts data on a
    queue that the render thread drains.
    """

    def __init__(self, source, interval: float = AUTO_REFRESH_SECONDS) -> None:
        self.source = source
        self.interval = interval
        self.results: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_signature: tuple | None = None
        self.auto = True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="opentine-gui-loader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def request(self, *, force: bool = True) -> None:
        """Ask for a scan now. `force` rescans even if nothing looks changed."""
        with self._lock:
            self._force = self._force or force
        self._wake.set()

    def retarget(self, source) -> None:
        with self._lock:
            self.source = source
            self._last_signature = None
            self._force = True
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                source = self.source
                force = self._force
                self._force = False
            if not force and not self.auto:
                continue
            try:
                signature = source.signature()
            except Exception:
                signature = ()
            if not force and signature == self._last_signature:
                continue
            try:
                snapshot = source.scan()
            except Exception as e:  # a source must never take the console down
                snapshot = Snapshot(
                    kind=getattr(source, "kind", "directory"),
                    root=getattr(source, "root", Path(".")),
                    errors=[f"scan failed: {e}"],
                )
            self._last_signature = snapshot.signature or signature
            self.results.put(snapshot)

    def drain(self) -> Snapshot | None:
        """The newest finished snapshot, or None. Older ones are discarded."""
        latest = None
        while True:
            try:
                latest = self.results.get_nowait()
            except queue.Empty:
                return latest


class OpentineGUI:
    def __init__(self, runs_dir: Path | None = None) -> None:
        self._preferences = _load_preferences()
        preferred_dir = self._preferences.get("last_runs_dir")
        if runs_dir is None and preferred_dir:
            self._runs_dir = _expand_user(preferred_dir)
        else:
            self._runs_dir = runs_dir or DEFAULT_RUNS_DIR
        self._source = open_source(self._runs_dir)
        self._snapshot = Snapshot(root=self._runs_dir)
        self._entries: list[RunEntry] = []
        self._errors: list[str] = []
        self._selected_key: str | None = None
        self._selected_run: Run | None = None
        self._selected_step: Step | None = None
        self._run_filter = self._preferences.get("last_filter", "").strip().lower()
        self._step_filter = ""
        self._sort_column = self._preferences.get("sort_column", "age")
        self._sort_ascending = self._preferences.get("sort_ascending", "0") == "1"
        self._layout_left = 0
        self._layout_dag_cols = 0
        self._relayout_pending = False
        self._pending_select: str | None = None
        self._messages: list[_Message] = []
        self._trust = trust.load_trust_config(self._preferences)
        self._loader = _Loader(self._source)
        self._filter_dirty_at: float | None = None
        self._preferences_dirty_at: float | None = None
        self._node_ids: dict[str, int | str] = {}
        #: What the table last drew, and which revision of the selected run the
        #: graph was built from. Both are compared before doing the work again:
        #: rebuilding the graph throws away the reader's pan, zoom and node
        #: positions, and rebuilding the table costs six widgets per row.
        #: None until the table has been drawn once. An empty directory has an
        #: empty state, and treating "nothing yet" as "same as last time" left
        #: the empty-state row undrawn.
        self._table_fingerprint: tuple | None = None
        self._graph_fingerprint: tuple = ()
        self._quote: pricing.RunQuote | None = None
        self._quote_key: str | None = None
        #: Label -> entry key for the comparison picker, and the pending action
        #: a confirmation is guarding. Both are set by the dialog that owns them.
        self._diff_choices: dict[str, str] = {}
        self._confirm_action = None
        #: A run id to select as soon as a scan reports it. A write action knows
        #: what it made before the source has been re-read, and the row it should
        #: land on does not exist until then.
        self._select_after_scan: str | None = None

    # ------------------------------------------------------------------ setup

    def run(self) -> None:
        _windows_set_dpi_aware()  # before any window, and whatever the scale is
        scale = set_ui_scale(_detect_ui_scale())
        _reset_theme_caches()
        dpg.create_context()
        # Dear PyGui dispatches every callback on a dedicated non-main thread.
        # Selecting a run, typing in the DAG filter or confirming a fork all
        # delete and recreate hundreds of node-editor items, which races the
        # renderer mid-frame and crashes natively. Draining the queue ourselves
        # runs every callback on the render thread, between frames.
        dpg.configure_app(manual_callback_management=True)
        dpg.bind_theme(_app_theme())
        if not self._bind_ui_font() and scale != 1.0:
            # No TTF available: scale the built-in bitmap font instead.
            dpg.set_global_font_scale(scale)
        width, height, min_width, min_height = _viewport_geometry()
        dpg.create_viewport(
            title="opentine - agent run console",
            width=width,
            height=height,
            min_width=min_width,
            min_height=min_height,
        )

        self._build_ui()

        dpg.set_primary_window("primary", True)
        dpg.set_viewport_resize_callback(self._on_viewport_resize)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        self._on_viewport_resize()
        self._loader.start()
        self._loader.request(force=True)
        self._note(
            "info",
            f"opentine-gui {_GUI_VERSION} reading with opentine {_OPENTINE_VERSION}",
        )
        try:
            while dpg.is_dearpygui_running():
                try:
                    dpg.run_callbacks(dpg.get_callback_queue())
                    self._apply_pending_input()
                    self._apply_pending_relayout()
                    self._apply_snapshot()
                    self._apply_deferred_writes()
                except Exception as e:  # one bad .tine must not take down the console
                    self._note("error", f"Refresh failed: {e}")
                dpg.render_dearpygui_frame()
        finally:
            self._loader.stop()
            self._flush_preferences()
            dpg.destroy_context()

    def _build_ui(self) -> None:
        """Create every widget. Separate from run() so a test can build the
        console into a Dear PyGui stand-in without a graphics context."""
        with dpg.window(tag="primary"):
            self._build_menu_bar()
            self._build_top_bar()
            with dpg.group(horizontal=True):
                self._build_run_list()
                self._build_detail_panel()
                self._build_dag_panel()
            self._build_status_bar()

        self._build_dir_picker()
        self._build_diff_dialog()
        self._build_fork_dialog()
        self._build_transcript_dialog()
        self._build_text_dialog()
        self._build_panel_dialog()
        self._build_confirm_dialog()
        self._build_help_dialog()
        self._build_key_bindings()

    def _scan_now(self) -> None:
        """Scan the source on this thread and apply the result immediately.

        The console normally scans on the loader thread; this is the path a
        test drives, and the one an action takes when it needs the panels to
        reflect a write it just made rather than a snapshot from before it.
        """
        try:
            snapshot = self._source.scan()
        except Exception as e:
            snapshot = Snapshot(
                kind=getattr(self._source, "kind", "directory"),
                root=self._runs_dir,
                errors=[f"scan failed: {e}"],
            )
        self._loader.results.put(snapshot)
        self._apply_snapshot()

    def _build_menu_bar(self) -> None:
        with dpg.menu_bar():
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="Refresh   Ctrl+R", callback=self._force_refresh)
                dpg.add_menu_item(
                    label="Change runs dir...   Ctrl+O", callback=self._open_dir_picker
                )
                dpg.add_separator()
                dpg.add_menu_item(
                    label="Import a trace...",
                    callback=self._open_import_dialog,
                    tag="menu_import",
                )
                dpg.add_separator()
                dpg.add_menu_item(label="Quit", callback=self._quit)
            with dpg.menu(label="Run"):
                dpg.add_menu_item(label="Pause", callback=self._pause_selected, tag="menu_pause")
                dpg.add_menu_item(label="Resume", callback=self._resume_selected, tag="menu_resume")
                dpg.add_separator()
                dpg.add_menu_item(
                    label="Fork from step", callback=self._fork_selected, tag="menu_fork"
                )
                dpg.add_menu_item(
                    label="Fork to branch...",
                    callback=self._open_fork_dialog,
                    tag="menu_fork_branch",
                )
                dpg.add_separator()
                dpg.add_menu_item(
                    label="Transcript...", callback=self._open_transcript, tag="menu_transcript"
                )
                dpg.add_menu_item(
                    label="Price this run...", callback=self._open_pricing, tag="menu_pricing"
                )
                dpg.add_menu_item(
                    label="Compare with...", callback=self._open_diff_dialog, tag="menu_diff"
                )
                dpg.add_separator()
                dpg.add_menu_item(
                    label="Export as OpenTelemetry JSON",
                    callback=self._export_otel,
                    tag="menu_export_otel",
                )
            with dpg.menu(label="View"):
                dpg.add_menu_item(
                    label="Statistics...", callback=self._open_stats, tag="menu_stats"
                )
                dpg.add_menu_item(
                    label="Repository refs...", callback=self._open_refs, tag="menu_refs"
                )
                dpg.add_separator()
                dpg.add_menu_item(
                    label="Auto-refresh",
                    callback=self._toggle_auto_refresh,
                    check=True,
                    default_value=True,
                    tag="menu_auto_refresh",
                )
                dpg.add_menu_item(
                    label="Message log",
                    callback=self._toggle_messages,
                    check=True,
                    default_value=True,
                    tag="menu_messages",
                )
            with dpg.menu(label="Help"):
                dpg.add_menu_item(label="Keyboard and features   F1", callback=self._open_help)
                dpg.add_menu_item(label="About", callback=self._open_about)

    def _build_top_bar(self) -> None:
        with dpg.group(horizontal=True):
            dpg.add_text("opentine", color=TEXT_PRIMARY)
            dpg.add_text("agent run console", color=TEXT_MUTED)
            dpg.add_spacer(width=_px(16))
            dpg.add_text("", tag="source_badge", color=BRAND)
            dpg.add_spacer(width=_px(8))
            dpg.add_text(_sanitize(str(self._runs_dir)), tag="top_runs_dir", color=TEXT_MUTED)
        dpg.add_separator()

    def _build_run_list(self) -> None:
        with dpg.child_window(width=_px(400), height=-_px(76), border=True, tag="panel_runs"):
            _panel_header("Runs", "Search, select, and manage traces")
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    hint="Search, or status:failed model:opus cost:>0.01 tag:bug",
                    tag="run_filter",
                    default_value=self._run_filter,
                    width=-_px(52),
                    callback=self._on_filter_change,
                )
                dpg.add_button(label="x", width=_px(28), callback=self._clear_run_filter)
                _hint(dpg.last_item(), "Clear the run filter (Esc)")
            dpg.add_text("", tag="run_summary", wrap=_px(300), color=TEXT_SECONDARY)
            with dpg.group(horizontal=True):
                _action_button("Pause", self._pause_selected, "btn_pause")
                _action_button("Resume", self._resume_selected, "btn_resume")
                _action_button("Fork", self._fork_selected, "btn_fork")
                _action_button("Diff", self._open_diff_dialog, "btn_diff")
            dpg.add_separator()
            # `resizable` and `hideable` are not decoration: Dear PyGui ignores
            # `configure_item(column, show=...)` entirely, so the only way a
            # reader can trade one column for another in a narrow sidebar is the
            # table's own header menu. `Age` starts hidden for the same reason —
            # it is the least load-bearing column, and the row tooltip has it.
            with dpg.table(
                header_row=True,
                borders_innerH=True,
                borders_outerH=True,
                row_background=True,
                resizable=True,
                hideable=True,
                reorderable=True,
                sortable=True,
                context_menu_in_body=True,
                callback=self._on_table_sort,
                policy=dpg.mvTable_SizingStretchProp,
                tag="run_table",
            ):
                dpg.add_table_column(label="Run", tag="col_id", init_width_or_weight=2.2)
                dpg.add_table_column(label="State", tag="col_status", init_width_or_weight=1.2)
                dpg.add_table_column(label="Model", tag="col_model", init_width_or_weight=1.7)
                dpg.add_table_column(label="Steps", tag="col_steps", init_width_or_weight=0.7)
                dpg.add_table_column(label="Cost", tag="col_cost", init_width_or_weight=1.2)
                dpg.add_table_column(
                    label="Age", tag="col_age", init_width_or_weight=0.7, default_hide=True
                )
            dpg.add_separator()
            dpg.add_text("Load errors", color=ACCENT_ORANGE, tag="err_header", show=False)
            dpg.add_text("", tag="err_text", wrap=_px(312), color=ACCENT_ORANGE)

    def _build_detail_panel(self) -> None:
        with dpg.child_window(width=_px(500), height=-_px(76), border=True, tag="panel_detail"):
            with dpg.group(horizontal=True):
                _panel_header("Run inspector", "Trace metadata", "panel_detail_subtitle")
                dpg.add_spacer(width=_px(8))
                # Ids are hashes and get elided on screen; the CLI needs them whole.
                _action_button("Copy id", self._copy_run_id, "btn_copy_run", width=0)
                _action_button("Copy all", self._copy_run_detail, "btn_copy_run_detail", width=0)
                _action_button("Expand", self._expand_run_detail, "btn_expand_run", width=0)
            dpg.add_separator()
            dpg.add_text("Select a run", tag="detail_text", wrap=_px(452), color=TEXT_SECONDARY)
            dpg.add_spacer(height=_px(10))
            with dpg.group(horizontal=True):
                _panel_header("Step inspector", "Inputs, outputs, cost", "panel_step_subtitle")
                dpg.add_spacer(width=_px(8))
                _action_button("Copy id", self._copy_step_id, "btn_copy_step", width=0)
                _action_button("Copy all", self._copy_step_detail, "btn_copy_step_detail", width=0)
                _action_button("Expand", self._expand_step_detail, "btn_expand_step", width=0)
            dpg.add_separator()
            dpg.add_text(
                "Select a step in the DAG",
                tag="step_text",
                wrap=_px(452),
                color=TEXT_SECONDARY,
            )

    def _build_dag_panel(self) -> None:
        with dpg.child_window(border=True, height=-_px(76), tag="panel_dag"):
            _panel_header("Step DAG", "Parent-child execution graph")
            dpg.add_text(
                "Select a run to inspect its opentine step graph.",
                tag="dag_summary",
                wrap=_px(580),
                color=TEXT_SECONDARY,
            )
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    hint="Highlight: id, kind, tool, provider, payload (Enter)",
                    tag="step_filter",
                    width=_px(330),
                    callback=self._on_step_filter_change,
                    on_enter=True,
                )
                dpg.add_button(label="Clear", callback=self._clear_step_filter, width=_px(64))
                dpg.add_button(label="Fit", callback=self._fit_dag, width=_px(48))
                _hint(dpg.last_item(), "Scroll the graph back to its first node")
                dpg.add_button(label="Next match", callback=self._focus_next_match, width=_px(96))
                _hint(dpg.last_item(), "Select and scroll to the next highlighted step")
            with dpg.group(horizontal=True):
                for kind in StepKind:
                    dpg.add_text(kind.value, color=STEP_COLORS[kind])
                dpg.add_spacer(width=_px(12))
                dpg.add_text("causal edge", color=ACCENT_PURPLE)
                _hint(
                    dpg.last_item(),
                    "A non-parent step this one required. A fork keeps these too.",
                )
            dpg.add_separator()
            with dpg.node_editor(
                tag="dag_editor",
                callback=self._on_link_created,
                delink_callback=self._on_link_deleted,
                minimap=True,
                minimap_location=dpg.mvNodeMiniMap_Location_BottomRight,
            ):
                pass

    def _build_status_bar(self) -> None:
        dpg.add_separator()
        with dpg.child_window(height=_px(62), border=False, tag="panel_messages"):
            with dpg.group(horizontal=True):
                dpg.add_text("", tag="status_bar", color=TEXT_SECONDARY)
                dpg.add_spacer(width=_px(8))
                dpg.add_text("", tag="status_meta", color=TEXT_MUTED)
            dpg.add_child_window(height=-1, border=False, tag="message_log")

    def _build_key_bindings(self) -> None:
        """Global shortcuts.

        Every handler runs on the render thread, like all other callbacks, since
        the loop drains the callback queue itself. Selection is still recorded
        and applied in `_apply_pending_input` so that holding a key down cannot
        rebuild the graph more than once per frame.
        """
        with dpg.handler_registry(tag="global_keys"):
            dpg.add_key_press_handler(dpg.mvKey_Down, callback=lambda: self._move_selection(1))
            dpg.add_key_press_handler(dpg.mvKey_Up, callback=lambda: self._move_selection(-1))
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self._on_escape)
            dpg.add_key_press_handler(dpg.mvKey_F, callback=self._on_ctrl_f)
            dpg.add_key_press_handler(dpg.mvKey_C, callback=self._on_ctrl_c)
            dpg.add_key_press_handler(dpg.mvKey_R, callback=self._on_ctrl_r)
            dpg.add_key_press_handler(dpg.mvKey_O, callback=self._on_ctrl_o)
            dpg.add_key_press_handler(dpg.mvKey_F1, callback=self._on_help_key)

    def _bind_ui_font(self) -> bool:
        """Load a real font so non-ASCII agent output is legible. False if none."""
        path = _find_ui_font()
        if path is None:
            return False
        try:
            with dpg.font_registry():
                with dpg.font(str(path), _px(FONT_SIZE)) as font:
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                    # Latin-1 accents plus the punctuation and symbols that
                    # actually show up in model output (dashes, arrows, checks).
                    for first, last in EXTRA_GLYPH_RANGES:
                        dpg.add_font_range(first, last)
                    if os.environ.get("OPENTINE_GUI_FONT"):
                        # The user pointed us at a specific face; assume they did
                        # so for a script the default cannot draw. These hints
                        # are large, so they are not loaded by default.
                        for hint in (
                            dpg.mvFontRangeHint_Cyrillic,
                            dpg.mvFontRangeHint_Japanese,
                            dpg.mvFontRangeHint_Chinese_Simplified_Common,
                            dpg.mvFontRangeHint_Korean,
                        ):
                            dpg.add_font_range_hint(hint)
            dpg.bind_font(font)
        except Exception:
            return False  # a broken/unsupported face must not stop the console
        return True

    # --------------------------------------------------------------- plumbing

    def _typing(self) -> bool:
        """True while a text field has focus, so keys reach the field, not us."""
        return any(
            dpg.does_item_exist(tag) and dpg.is_item_focused(tag)
            for tag in (
                "run_filter",
                "step_filter",
                "dir_picker_input",
                "fork_branch",
                "fork_reason",
                "import_path",
                "text_body",
            )
        )

    MODALS = (
        "fork_dialog",
        "diff_dialog",
        "dir_picker",
        "transcript_dialog",
        "text_dialog",
        "panel_dialog",
        "confirm_dialog",
        "help_dialog",
    )

    def _modal_open(self) -> str | None:
        for tag in self.MODALS:
            if dpg.does_item_exist(tag) and dpg.is_item_shown(tag):
                return tag
        return None

    def _command_down(self) -> bool:
        """The platform's command modifier: Ctrl everywhere, Cmd on macOS."""
        if dpg.is_key_down(dpg.mvKey_ModCtrl):
            return True
        return sys.platform == "darwin" and dpg.is_key_down(dpg.mvKey_ModSuper)

    def _note(self, level: str, message: str) -> None:
        """Say something, in a place that the next refresh will not overwrite."""
        # Bounded as well as flattened: a run id is artifact-controlled and
        # unbounded, and it reaches this line through several action messages.
        text = _truncate(_oneline(message), MAX_PANEL_ROW)
        if self._messages and self._messages[-1].text == text:
            # A failure inside the frame loop repeats at frame rate. One row
            # saying it happened 400 times is information; 400 identical rows
            # are a scrollback that has pushed everything else out of reach.
            self._messages[-1].repeats += 1
            if dpg.does_item_exist("message_log"):
                children = dpg.get_item_children("message_log", slot=1) or []
                if children:
                    dpg.set_value(
                        children[-1],
                        f"{time.strftime('%H:%M:%S')}  {text}"
                        f"  (x{self._messages[-1].repeats})",
                    )
            return
        self._messages.append(_Message(level=level, text=text, at=time.time()))
        del self._messages[:-MAX_MESSAGES]
        if dpg.does_item_exist("status_bar"):
            dpg.set_value("status_bar", text)
        if dpg.does_item_exist("message_log"):
            dpg.add_text(
                f"{time.strftime('%H:%M:%S')}  {text}",
                parent="message_log",
                color=LEVEL_COLORS.get(level, TEXT_SECONDARY),
                wrap=_px(1100),
            )
            children = dpg.get_item_children("message_log", slot=1) or []
            for stale in children[:-MAX_MESSAGES]:
                dpg.delete_item(stale)
            dpg.set_y_scroll("message_log", -1.0)

    def _set_status(self, msg: str) -> None:
        """A transient line. Anything a user may need to act on goes to _note."""
        if dpg.does_item_exist("status_bar"):
            dpg.set_value("status_bar", _oneline(msg))

    def _quit(self) -> None:
        dpg.stop_dearpygui()

    def _toggle_auto_refresh(self) -> None:
        enabled = bool(dpg.get_value("menu_auto_refresh"))
        self._loader.auto = enabled
        self._note("info", f"Auto-refresh {'on' if enabled else 'off'}")

    def _toggle_messages(self) -> None:
        show = bool(dpg.get_value("menu_messages"))
        if dpg.does_item_exist("panel_messages"):
            dpg.configure_item("panel_messages", height=_px(62) if show else _px(24))
        if dpg.does_item_exist("message_log"):
            dpg.configure_item("message_log", show=show)

    def _force_refresh(self) -> None:
        self._loader.request(force=True)
        self._set_status("Refreshing...")

    # ------------------------------------------------------------- key events

    def _move_selection(self, delta: int) -> None:
        if self._typing() or self._modal_open():
            return
        # Only what the table actually drew: stepping past the cap would select
        # a run with no row on screen, leaving the inspector describing a run the
        # reader cannot see or point at.
        visible = self._visible_entries()[:MAX_TABLE_ROWS]
        if not visible:
            return
        keys = [entry.key for entry in visible]
        if self._selected_key is None or self._selected_key not in keys:
            index = 0
        else:
            index = min(max(keys.index(self._selected_key) + delta, 0), len(keys) - 1)
        self._pending_select = keys[index]

    def _on_escape(self) -> None:
        modal = self._modal_open()
        if modal == "confirm_dialog":
            self._confirm_cancel()
        elif modal:
            dpg.configure_item(modal, show=False)
        elif self._step_filter:
            self._clear_step_filter()
        elif self._run_filter:
            self._clear_run_filter()

    def _on_ctrl_f(self) -> None:
        if self._command_down() and not self._modal_open() and dpg.does_item_exist("run_filter"):
            dpg.focus_item("run_filter")

    def _on_ctrl_c(self) -> None:
        if self._command_down() and not self._typing() and not self._modal_open():
            self._copy_run_id()

    def _on_ctrl_r(self) -> None:
        if self._command_down() and not self._typing():
            self._force_refresh()

    def _on_ctrl_o(self) -> None:
        if self._command_down() and not self._typing() and not self._modal_open():
            self._open_dir_picker()

    def _on_help_key(self) -> None:
        if not self._typing():
            self._open_help()

    def _apply_pending_input(self) -> None:
        """Consume a keyboard-requested selection between frames."""
        key, self._pending_select = self._pending_select, None
        if key is not None and key != self._selected_key:
            self._select_entry(key)

    def _apply_pending_relayout(self) -> None:
        """Rebuild the table/DAG a resize invalidated, between frames.

        Dear PyGui delivers resize callbacks on its own thread while the render
        thread is mid-frame; creating and deleting hundreds of node items from
        there races the renderer. The callback only records what changed.
        """
        if not self._relayout_pending:
            return
        self._relayout_pending = False
        if dpg.does_item_exist("run_table"):
            self._render_run_table()
        if self._selected_run is not None and dpg.does_item_exist("dag_editor"):
            self._rebuild_dag(
                self._selected_run, highlight=self._current_matches(self._selected_run)
            )

    def _apply_deferred_writes(self) -> None:
        """Debounced work: the filter, and the preferences file."""
        now = time.monotonic()
        filtering = self._filter_dirty_at
        if filtering is not None and now - filtering >= FILTER_DEBOUNCE_SECONDS:
            self._filter_dirty_at = None
            self._render_run_table()
            self._update_action_state()
            problem = _query_error(self._run_filter)
            if problem:
                self._set_status(f"{problem} - falling back to a plain text search")
            else:
                shown = len(self._visible_entries())
                self._set_status(
                    f"{self._source.label} - {shown}/{len(self._entries)} run(s) shown"
                )
        if (
            self._preferences_dirty_at is not None
            and now - self._preferences_dirty_at >= PREFERENCES_FLUSH_SECONDS
        ):
            self._flush_preferences()

    # ---------------------------------------------------------------- refresh

    def _apply_snapshot(self) -> None:
        snapshot = self._loader.drain()
        if snapshot is None:
            return
        if snapshot.root != self._runs_dir:
            # A scan of the directory the user has just left. Applying it would
            # repopulate the panels with runs from somewhere else.
            return
        self._snapshot = snapshot
        self._entries = snapshot.entries
        self._errors = snapshot.errors
        selected = self._selected_key
        entry = snapshot.entry(selected) if selected else None
        if selected and entry is None:
            self._selected_key = None
            self._selected_run = None
            self._selected_step = None
            dpg.set_value("detail_text", "Select a run")
            dpg.set_value("step_text", "Select a step in the DAG")
            self._clear_dag()
        elif entry is not None:
            self._selected_run = entry.run
            self._show_run_detail(entry)
            # Keyed on the file revision rather than on object identity: the run
            # cache hands back the same object for an unchanged file only while
            # it holds it, so past its cap identity alone rebuilt every graph on
            # every tick.
            fingerprint = (entry.key, entry.mtime, entry.size, len(entry.run.steps))
            if fingerprint != self._graph_fingerprint:
                self._graph_fingerprint = fingerprint
                self._rebuild_dag(entry.run, highlight=self._current_matches(entry.run))
            if self._selected_step is not None:
                step = entry.run.get_step(self._selected_step.id)
                self._selected_step = step
                if step is not None:
                    self._show_step_detail(step)
                else:
                    dpg.set_value("step_text", "That step is no longer in this run")
        pending, self._select_after_scan = self._select_after_scan, None
        if pending is not None:
            target = next((e for e in snapshot.entries if str(e.run.id) == pending), None)
            if target is not None:
                self._select_entry(target.key)
            else:
                self._select_after_scan = pending  # not written yet; try the next scan
        self._render_run_table()
        self._render_errors()
        self._update_action_state()
        self._render_source_badge()
        if dpg.does_item_exist("status_meta"):
            dpg.set_value("status_meta", f"updated {time.strftime('%H:%M:%S')}")
        shown = len(self._visible_entries())
        filter_note = f", {shown} shown" if self._run_filter else ""
        summary = f"{self._source.label} - {len(self._entries)} run(s){filter_note}"
        if self._errors:
            summary += f", {len(self._errors)} problem(s)"
        self._set_status(summary)
        if snapshot.note:
            self._note("warn", snapshot.note)

    def _render_source_badge(self) -> None:
        if not dpg.does_item_exist("source_badge"):
            return
        if self._snapshot.kind == "repository":
            dpg.configure_item("source_badge", color=ACCENT_PURPLE)
            dpg.set_value("source_badge", "v3 repository (read-only)")
        else:
            dpg.configure_item("source_badge", color=BRAND)
            dpg.set_value("source_badge", ".tine directory")
        dir_str = _sanitize(str(self._runs_dir))
        if len(dir_str) > 64:
            dir_str = "..." + dir_str[-61:]
        dpg.set_value("top_runs_dir", dir_str)

    @property
    def _runs(self) -> list[Run]:
        """Every loaded run, in snapshot order."""
        return [entry.run for entry in self._entries]

    @property
    def _run_paths(self) -> dict[str, Path]:
        """run id -> the file it came from, for the sources that have files."""
        return {str(e.run.id): e.path for e in self._entries if e.path is not None}

    def _refresh(self) -> None:
        """Scan now and render the result, rather than waiting for the loader."""
        self._scan_now()

    def _select_run(self, run_id: str) -> None:
        """Select by run id. Repository rows are keyed by object id instead."""
        entry = next((e for e in self._entries if str(e.run.id) == str(run_id)), None)
        if entry is not None:
            self._select_entry(entry.key)

    def _trust_lines(self, run: Run) -> list[str]:
        """The trust block for a loaded run, through the configured key material."""
        entry = next((e for e in self._entries if str(e.run.id) == str(run.id)), None)
        if entry is None or entry.path is None:
            entry_of_selection = self._selected_entry()
            if entry_of_selection is not None and entry_of_selection.run is run:
                entry = entry_of_selection
        if entry is None or entry.path is None:
            return _trust_lines(None, config=self._trust)
        return _trust_lines(entry.path, config=self._trust)

    def _visible_entries(self) -> list[RunEntry]:
        entries = [e for e in self._entries if _run_matches_filter(e.run, self._run_filter)]
        return self._sorted(entries)

    def _sorted(self, entries: list[RunEntry]) -> list[RunEntry]:
        column = self._sort_column
        reverse = not self._sort_ascending

        def key(entry: RunEntry):
            run = entry.run
            try:
                if column == "id":
                    return str(run.id).lower()
                if column == "status":
                    return run.status.value
                if column == "model":
                    return str(run.model_info or "").lower()
                if column == "steps":
                    return len(run.steps)
                if column == "cost":
                    return float(run.total_cost)
            except Exception:
                return ""
            return entry.mtime or getattr(run, "created_at", 0.0) or 0.0

        try:
            return sorted(entries, key=key, reverse=reverse)
        except TypeError:  # mixed types from a hostile artifact
            return entries

    def _filtered_runs(self) -> list[Run]:
        return [entry.run for entry in self._visible_entries()]

    def _render_run_table(self, *, force: bool = False) -> None:
        if not dpg.does_item_exist("run_table"):
            return
        fingerprint = self._table_state()
        if not force and fingerprint == self._table_fingerprint:
            return
        self._table_fingerprint = fingerprint
        for child in dpg.get_item_children("run_table", slot=1) or []:
            dpg.delete_item(child)
        visible = self._visible_entries()
        shown = visible[:MAX_TABLE_ROWS]
        for entry in shown:
            run = entry.run
            selected = entry.key == self._selected_key
            with dpg.table_row(parent="run_table"):
                label = _elide_middle(_oneline(run.id), 18)
                dpg.add_selectable(
                    label=label,
                    default_value=selected,
                    span_columns=True,
                    callback=self._on_run_selected,
                    user_data=entry.key,
                )
                _hint(dpg.last_item(), self._row_tooltip(entry))
                dpg.add_text(
                    run.status.value, color=RUN_STATUS_COLORS.get(run.status, TEXT_PRIMARY)
                )
                dpg.add_text(_truncate(_oneline(run.model_info) or "-", 22))
                dpg.add_text(str(len(run.steps)))
                dpg.add_text(_cost_cell(run))
                dpg.add_text(_format_age(entry.mtime or getattr(run, "created_at", 0.0)))
        if len(visible) > len(shown):
            with dpg.table_row(parent="run_table"):
                dpg.add_text(f"...{len(visible) - len(shown)} more not shown", color=TEXT_MUTED)
                for _ in range(5):
                    dpg.add_text("")
        dpg.set_value(
            "run_summary",
            _run_list_summary(
                [e.run for e in self._entries],
                [e.run for e in visible],
                self._run_filter,
            ),
        )
        if not visible:
            with dpg.table_row(parent="run_table"):
                if self._run_filter:
                    msg = "No runs match this filter"
                elif self._errors:
                    msg = "Nothing loaded - see the problems below"
                else:
                    msg = "No .tine runs here yet"
                dpg.add_text(msg, color=TEXT_MUTED)
                for _ in range(5):
                    dpg.add_text("")

    def _row_tooltip(self, entry: RunEntry) -> str:
        """What the row's own columns are too narrow to say."""
        run = entry.run
        parts = [
            _oneline(run.id),
            f"{run.status.value}  {len(run.steps)} step(s)  {_cost_cell(run)}",
            f"model {_oneline(run.model_info) or '(none)'}",
            f"created {_format_timestamp(getattr(run, 'created_at', 0.0))}",
        ]
        if entry.location:
            parts.append(f"stored {_oneline(entry.location)}")
        if entry.refs:
            parts.append("refs " + ", ".join(_oneline(r) for r in entry.refs))
        if run.tags:
            parts.append("tags " + ", ".join(_oneline(tag) for tag in sorted(run.tags)))
        return "\n".join(parts)

    def _table_state(self) -> tuple:
        """Everything the table draws, so an unchanged list is not redrawn."""
        try:
            return tuple(
                (
                    entry.key,
                    self._selected_key == entry.key,
                    entry.run.status.value,
                    len(entry.run.steps),
                    _cost_cell(entry.run),
                    _format_age(entry.mtime or getattr(entry.run, "created_at", 0.0)),
                )
                for entry in self._visible_entries()[:MAX_TABLE_ROWS]
            )
        except Exception:
            # Never equal to a real state, so an artifact that raises here costs
            # a redraw rather than a frozen table.
            return ("<unreadable>",)

    def _on_table_sort(self, sender, sort_specs) -> None:
        """Dear PyGui hands back [[column_id, direction], ...], or None."""
        try:
            if not sort_specs:
                return
            column_id, direction = sort_specs[0][0], sort_specs[0][1]
            for name in TABLE_COLUMNS:
                tag = f"col_{name}"
                if dpg.does_item_exist(tag) and dpg.get_alias_id(tag) == column_id:
                    self._sort_column = name
                    break
            self._sort_ascending = direction > 0
        except Exception:
            return
        self._preferences["sort_column"] = self._sort_column
        self._preferences["sort_ascending"] = "1" if self._sort_ascending else "0"
        self._touch_preferences()
        self._render_run_table()

    def _render_errors(self) -> None:
        if self._errors:
            fatal, warnings = _split_load_problems(self._errors)
            dpg.configure_item(
                "err_header",
                show=True,
                default_value=_load_problem_header(len(fatal), len(warnings)),
            )
            # Files that did not load at all come first: a warning about a run
            # the user can still open must not push a missing run out of view.
            ordered = fatal + warnings
            shown = [_oneline(_truncate(e, 160)) for e in ordered[:10]]
            if len(ordered) > len(shown):
                shown.append(f"...and {len(ordered) - len(shown)} more")
            dpg.set_value("err_text", _sanitize("\n".join(shown)))
        else:
            dpg.configure_item("err_header", show=False)
            dpg.set_value("err_text", "")

    # -------------------------------------------------------------- selection

    def _on_run_selected(self, sender, app_data, user_data) -> None:
        self._select_entry(user_data)

    def _selected_entry(self) -> RunEntry | None:
        if self._selected_key is None:
            return None
        return self._snapshot.entry(self._selected_key)

    def _select_entry(self, key: str) -> None:
        entry = self._snapshot.entry(key)
        if entry is None:
            return
        self._selected_key = key
        self._selected_run = entry.run
        self._selected_step = None
        self._quote = None
        self._show_run_detail(entry)
        dpg.set_value("step_text", "Select a step in the DAG")
        self._graph_fingerprint = (entry.key, entry.mtime, entry.size, len(entry.run.steps))
        self._rebuild_dag(entry.run, highlight=self._current_matches(entry.run))
        self._render_run_table()
        self._update_action_state()

    def _update_action_state(self) -> None:
        entry = self._selected_entry()
        run = entry.run if entry else None
        writable = bool(self._snapshot.writable and entry is not None and entry.path is not None)
        can_pause = bool(writable and run and run.status == RunStatus.running)
        can_resume = bool(writable and run and run.status == RunStatus.paused)
        can_fork = bool(writable and run and self._selected_step)
        can_diff = bool(run and any(e.key != self._selected_key for e in self._entries))
        for tag, enabled in (
            ("menu_pause", can_pause),
            ("btn_pause", can_pause),
            ("btn_pause_wrap", can_pause),
            ("menu_resume", can_resume),
            ("btn_resume", can_resume),
            ("btn_resume_wrap", can_resume),
            ("menu_fork", can_fork),
            ("menu_fork_branch", can_fork),
            ("btn_fork", can_fork),
            ("btn_fork_wrap", can_fork),
            ("menu_transcript", run is not None),
            ("menu_pricing", run is not None),
            ("menu_export_otel", run is not None and otelio.export_available()),
            ("menu_import", self._snapshot.writable and otelio.import_available()),
            ("menu_refs", self._snapshot.kind == "repository"),
            ("menu_diff", can_diff),
            ("btn_diff", can_diff),
            ("btn_diff_wrap", can_diff),
            ("btn_copy_run", run is not None),
            ("btn_copy_run_wrap", run is not None),
            ("btn_copy_run_detail", run is not None),
            ("btn_copy_run_detail_wrap", run is not None),
            ("btn_expand_run", run is not None),
            ("btn_expand_run_wrap", run is not None),
            ("btn_copy_step", self._selected_step is not None),
            ("btn_copy_step_wrap", self._selected_step is not None),
            ("btn_copy_step_detail", self._selected_step is not None),
            ("btn_copy_step_detail_wrap", self._selected_step is not None),
            ("btn_expand_step", self._selected_step is not None),
            ("btn_expand_step_wrap", self._selected_step is not None),
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=enabled)

    # -------------------------------------------------------------- rendering

    @staticmethod
    def _panel_text(lines: list[str]) -> str:
        """Rows, bounded and made safe, as one string for a flat text widget."""
        return _sanitize("\n".join(_truncate(line, MAX_PANEL_ROW) for line in lines))

    def _show_run_detail(self, entry: RunEntry) -> None:
        run = entry.run
        extra: list[str] = []
        if entry.location:
            extra.append(f"Stored: {_oneline(entry.location)}")
        if entry.refs:
            extra.append(f"Repository refs: {', '.join(_oneline(r) for r in entry.refs)}")
        if entry.size:
            extra.append(f"File: {_format_bytes(entry.size)}")
        if self._quote is not None and self._quote_key == entry.key:
            extra.extend(pricing.quote_lines(self._quote, limit=4))
        lines = _run_detail_lines(
            run,
            trust=(
                _trust_lines(entry.path, config=self._trust)
                if entry.path
                else self._repo_trust_lines()
            ),
            extra=extra,
        )
        dpg.set_value("detail_text", self._panel_text(lines))

    def _repo_trust_lines(self) -> list[str]:
        """A v3 run's trust story is the store's, not a file's."""
        return [
            "Integrity: every object in a v3 repository is content-addressed and "
            "verified on read",
            "Signature: v3 attestations are not read by this console "
            "(use `tine fsck` / `tine attest`)",
        ]

    def _show_step_detail(self, step: Step) -> None:
        dpg.set_value("step_text", self._panel_text(_step_detail_lines(step)))

    def _clear_dag(self) -> None:
        # Links (slot 0) must go before nodes (slot 1): deleting a node that a
        # live link still references segfaults Dear PyGui's native layer.
        for link in dpg.get_item_children("dag_editor", slot=0) or []:
            dpg.delete_item(link)
        for child in dpg.get_item_children("dag_editor", slot=1) or []:
            dpg.delete_item(child)
        self._node_ids.clear()
        if dpg.does_item_exist("dag_summary"):
            dpg.set_value("dag_summary", "Select a run to inspect its opentine step graph.")

    def _dag_avail_width(self) -> int:
        if dpg.does_item_exist("panel_dag"):
            w = dpg.get_item_rect_size("panel_dag")[0]
            if w > _px(100):
                return int(w) - _px(40)
        vw = dpg.get_viewport_client_width()
        left = center = 0
        if dpg.does_item_exist("panel_runs"):
            left = dpg.get_item_configuration("panel_runs")["width"]
        if dpg.does_item_exist("panel_detail"):
            center = dpg.get_item_configuration("panel_detail")["width"]
        return max(_px(260), vw - (left or _px(360)) - (center or _px(500)) - _px(80))

    def _rebuild_dag(self, run: Run, highlight: set[str] | None = None) -> None:
        highlight = highlight or set()
        self._clear_dag()
        summary = (
            _dag_summary(run, self._step_filter, highlight)
            if self._step_filter
            else _dag_summary(run)
        )
        steps = run.steps
        if len(steps) > MAX_DAG_NODES:
            steps = steps[:MAX_DAG_NODES]
            summary += f" - drawing the first {MAX_DAG_NODES} step(s)"
        dpg.set_value("dag_summary", summary)
        drawn = {step.id for step in steps}
        in_attr: dict[str, int] = {}
        out_attr: dict[str, int] = {}
        depth = _step_depths(run)
        # Wrap depth columns into horizontal bands sized to the visible panel,
        # so whole graphs stay on screen instead of running off to the right.
        rows_at_depth: dict[int, int] = {}
        for step in steps:
            rows_at_depth[depth.get(step.id, 0)] = rows_at_depth.get(depth.get(step.id, 0), 0) + 1
        max_depth = max(rows_at_depth, default=0)
        pitch_x, pitch_y = _px(NODE_PITCH_X), _px(NODE_PITCH_Y)
        cols = max(1, self._dag_avail_width() // pitch_x)
        # Bucket depths by band once. Rescanning every depth for every band is
        # quadratic, and a legal run can hold thousands of steps.
        depths_by_band: dict[int, list[int]] = {}
        for d in rows_at_depth:
            depths_by_band.setdefault(d // cols, []).append(d)
        band_y: dict[int, int] = {}
        y_cursor = _px(20)
        for band in range(max_depth // cols + 1):
            band_y[band] = y_cursor
            band_rows = max((rows_at_depth[d] for d in depths_by_band.get(band, ())), default=1)
            y_cursor += band_rows * pitch_y + _px(30)
        col_fill: dict[int, int] = {}
        for step in steps:
            d = depth.get(step.id, 0)
            row = col_fill.get(d, 0)
            col_fill[d] = row + 1
            band, cx = divmod(d, cols)
            pos = [_px(20) + cx * pitch_x, band_y.get(band, _px(20)) + row * pitch_y]
            color = STEP_COLORS.get(step.kind, TEXT_PRIMARY)
            is_match = step.id in highlight
            node_id = dpg.add_node(
                parent="dag_editor",
                label=_node_label(step, highlighted=is_match),
                pos=pos,
                user_data=step.id,
            )
            dpg.bind_item_theme(node_id, _node_theme(color, highlighted=is_match))
            self._node_ids[step.id] = node_id

            in_id = dpg.add_node_attribute(parent=node_id, attribute_type=dpg.mvNode_Attr_Input)
            dpg.add_text("in", parent=in_id)
            in_attr[step.id] = in_id

            static_id = dpg.add_node_attribute(
                parent=node_id, attribute_type=dpg.mvNode_Attr_Static
            )
            dpg.add_text(_node_subtitle(step), parent=static_id)
            dpg.add_button(
                label="inspect",
                parent=static_id,
                user_data=step.id,
                callback=self._on_step_open,
                width=_px(80),
            )

            out_id = dpg.add_node_attribute(parent=node_id, attribute_type=dpg.mvNode_Attr_Output)
            dpg.add_text("out", parent=out_id)
            out_attr[step.id] = out_id

        for step in steps:
            if step.id not in in_attr:
                continue
            for parent_id in step.parent_ids:
                if parent_id in out_attr:
                    link = dpg.add_node_link(
                        out_attr[parent_id], in_attr[step.id], parent="dag_editor"
                    )
                    dpg.bind_item_theme(link, _link_theme("parent"))
        for cause, effect in causal_edges(run):
            if cause in out_attr and effect in in_attr and cause in drawn and effect in drawn:
                link = dpg.add_node_link(out_attr[cause], in_attr[effect], parent="dag_editor")
                dpg.bind_item_theme(link, _link_theme("causal"))

    def _on_step_open(self, sender, app_data, user_data) -> None:
        if not self._selected_run:
            return
        step = self._selected_run.get_step(user_data)
        if step:
            self._selected_step = step
            self._show_step_detail(step)
            self._update_action_state()

    def _on_link_created(self, sender, app_data) -> None:
        # A read-only picture of a recorded graph: dragging an edge cannot mean
        # anything, so say so rather than silently doing nothing.
        self._set_status("The step graph is a recording; it cannot be edited here")

    def _on_link_deleted(self, sender, app_data) -> None:
        self._set_status("The step graph is a recording; it cannot be edited here")

    def _fit_dag(self) -> None:
        """Scroll the editor back to the top-left, where the roots are drawn."""
        try:
            dpg.set_x_scroll("dag_editor", 0.0)
            dpg.set_y_scroll("dag_editor", 0.0)
        except Exception:
            pass

    def _focus_next_match(self) -> None:
        """Select the next highlighted step and scroll the editor onto it."""
        run = self._selected_run
        if run is None:
            self._set_status("Select a run first")
            return
        matches = _matching_steps(run, self._step_filter)
        if not matches:
            self._set_status("No matching steps to jump to")
            return
        current = self._selected_step.id if self._selected_step else None
        index = (matches.index(current) + 1) % len(matches) if current in matches else 0
        self._reveal_step(matches[index])

    def _reveal_step(self, step_id: str) -> None:
        run = self._selected_run
        if run is None:
            return
        step = run.get_step(step_id)
        if step is None:
            self._set_status("That step is not in this run")
            return
        self._selected_step = step
        self._show_step_detail(step)
        self._update_action_state()
        node = self._node_ids.get(step.id)
        if node is not None:
            try:
                dpg.clear_selected_nodes("dag_editor")
                position = dpg.get_item_pos(node)
                dpg.set_x_scroll("dag_editor", max(0.0, float(position[0]) - _px(120)))
                dpg.set_y_scroll("dag_editor", max(0.0, float(position[1]) - _px(120)))
            except Exception:
                pass

    # ----------------------------------------------------------------- filters

    def _current_matches(self, run: Run) -> set[str]:
        if not self._step_filter:
            return set()
        return set(_matching_steps(run, self._step_filter))

    def _on_filter_change(self, sender, app_data) -> None:
        self._run_filter = (app_data or "").strip().lower()
        self._preferences["last_filter"] = self._run_filter
        self._touch_preferences()
        self._filter_dirty_at = time.monotonic()

    def _clear_run_filter(self) -> None:
        if dpg.does_item_exist("run_filter"):
            dpg.set_value("run_filter", "")
        self._on_filter_change(None, "")

    def _on_step_filter_change(self, sender, app_data) -> None:
        self._step_filter = (app_data or "").strip().lower()
        run = self._selected_run
        matches = _matching_steps(run, self._step_filter) if run else []
        if run and dpg.does_item_exist("dag_summary"):
            dpg.set_value("dag_summary", _dag_summary(run, self._step_filter, set(matches)))
        if run:
            self._rebuild_dag(run, highlight=set(matches))
        if self._step_filter:
            if run:
                self._set_status(_highlight_summary(run, set(matches)))
            else:
                self._set_status("Select a run to highlight its steps")

    def _clear_step_filter(self) -> None:
        self._step_filter = ""
        if dpg.does_item_exist("step_filter"):
            dpg.set_value("step_filter", "")
        if self._selected_run:
            if dpg.does_item_exist("dag_summary"):
                dpg.set_value("dag_summary", _dag_summary(self._selected_run))
            self._rebuild_dag(self._selected_run)

    # ------------------------------------------------------------- clipboard

    def _copy_to_clipboard(self, value: str, label: str) -> None:
        try:
            dpg.set_clipboard_text(value)
        except Exception as e:
            self._note("warn", f"Could not copy {label}: {e}")
            return
        self._set_status(f"Copied {label}: {_truncate(value, 60)}")

    def _copy_run_id(self) -> None:
        if self._selected_run is None:
            self._set_status("Select a run first")
            return
        self._copy_to_clipboard(str(self._selected_run.id), "run id")

    def _copy_step_id(self) -> None:
        if self._selected_step is None:
            self._set_status("Select a step first")
            return
        self._copy_to_clipboard(str(self._selected_step.id), "step id")

    def _copy_run_detail(self) -> None:
        if not dpg.does_item_exist("detail_text"):
            return
        self._copy_to_clipboard(str(dpg.get_value("detail_text")), "run inspector")

    def _copy_step_detail(self) -> None:
        if not dpg.does_item_exist("step_text"):
            return
        self._copy_to_clipboard(str(dpg.get_value("step_text")), "step inspector")

    def _expand_run_detail(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._set_status("Select a run first")
            return
        run = entry.run
        body = [
            *_run_detail_lines(
                run,
                trust=(
                _trust_lines(entry.path, config=self._trust)
                if entry.path
                else self._repo_trust_lines()
            ),
            ),
            "",
            "Prompt (full):",
            *_indent_block(run.user_prompt or ""),
        ]
        if run.system_prompt:
            body.extend(["", "System prompt (full):", *_indent_block(run.system_prompt)])
        self._show_text(f"Run {_oneline(_truncate(run.id, 40))}", "\n".join(body))

    def _expand_step_detail(self) -> None:
        step = self._selected_step
        if step is None:
            self._set_status("Select a step first")
            return
        body = [
            *_step_detail_lines(step),
            "",
            "Inputs (full):",
            *_indent_block(_format_value(step.inputs, 200_000)),
            "",
            "Outputs (full):",
            *_indent_block(_format_value(step.outputs, 200_000)),
        ]
        self._show_text(f"Step {_oneline(_truncate(step.id, 40))}", "\n".join(body))

    # ------------------------------------------------------------ text viewer

    def _build_text_dialog(self) -> None:
        """A read-only, selectable, copyable view of anything too long to inline.

        Dear PyGui has no selectable text widget, so a multiline input in
        read-only mode is the way a user gets to keep a prompt, a payload or a
        diff. It is never a way to edit an artifact: nothing reads its value.
        """
        with dpg.window(
            label="Details",
            modal=True,
            show=False,
            tag="text_dialog",
            width=_px(TEXT_DIALOG_SIZE[0]),
            height=_px(TEXT_DIALOG_SIZE[1]),
        ):
            dpg.add_text("", tag="text_subject", color=TEXT_SECONDARY)
            dpg.add_separator()
            dpg.add_input_text(
                tag="text_body",
                multiline=True,
                readonly=True,
                width=-1,
                height=-_px(44),
                default_value="",
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Copy", width=_px(110), callback=self._copy_text_dialog)
                dpg.add_button(
                    label="Close",
                    width=_px(110),
                    callback=lambda: dpg.configure_item("text_dialog", show=False),
                )

    def _show_text(self, subject: str, body: str) -> None:
        dpg.set_value("text_subject", _oneline(subject))
        # Newlines are the point of this widget, so only surrogates and control
        # characters are scrubbed here, not line structure.
        dpg.set_value("text_body", _sanitize(body))
        self._center("text_dialog", TEXT_DIALOG_SIZE)

    def _copy_text_dialog(self) -> None:
        self._copy_to_clipboard(str(dpg.get_value("text_body")), "text")

    def _center(self, tag: str, size: tuple[int, int]) -> None:
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        dpg.configure_item(
            tag,
            show=True,
            pos=[max(0, (vw - _px(size[0])) // 2), max(0, (vh - _px(size[1])) // 2)],
        )

    # ----------------------------------------------------------- panel dialog

    def _build_panel_dialog(self) -> None:
        """One reusable modal for the read-only panels: stats, pricing, refs."""
        with dpg.window(
            label="Panel",
            modal=True,
            show=False,
            tag="panel_dialog",
            width=_px(PANEL_DIALOG_SIZE[0]),
            height=_px(PANEL_DIALOG_SIZE[1]),
        ):
            dpg.add_text("", tag="panel_subject", color=TEXT_SECONDARY)
            dpg.add_separator()
            with dpg.group(horizontal=True, tag="panel_controls"):
                pass
            with dpg.child_window(tag="panel_body", border=False, height=-_px(44)):
                dpg.add_text("", tag="panel_text", wrap=_px(700), color=TEXT_SECONDARY)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Copy", width=_px(110), callback=self._copy_panel)
                dpg.add_button(
                    label="Close",
                    width=_px(110),
                    callback=lambda: dpg.configure_item("panel_dialog", show=False),
                )

    def _show_panel(self, title: str, subject: str, body: str) -> None:
        dpg.configure_item("panel_dialog", label=title)
        dpg.set_value("panel_subject", _oneline(subject))
        dpg.set_value("panel_text", self._panel_text(body.split("\n")))
        self._center("panel_dialog", PANEL_DIALOG_SIZE)

    def _copy_panel(self) -> None:
        self._copy_to_clipboard(str(dpg.get_value("panel_text")), "panel")

    def _clear_panel_controls(self) -> None:
        for child in dpg.get_item_children("panel_controls", slot=1) or []:
            dpg.delete_item(child)

    # ---------------------------------------------------------------- confirm

    def _build_confirm_dialog(self) -> None:
        with dpg.window(
            label="Confirm",
            modal=True,
            show=False,
            tag="confirm_dialog",
            width=_px(560),
            height=_px(240),
            no_resize=True,
        ):
            dpg.add_text("", tag="confirm_text", wrap=_px(520), color=TEXT_SECONDARY)
            dpg.add_spacer(height=_px(8))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Continue", width=_px(130), callback=self._confirm_accept)
                dpg.add_button(label="Cancel", width=_px(130), callback=self._confirm_cancel)

    def _ask(self, question: str, action) -> None:
        """Confirm before something the user cannot undo from inside the app."""
        self._confirm_action = action
        dpg.set_value("confirm_text", _sanitize(question))
        self._center("confirm_dialog", (560, 240))

    def _confirm_cancel(self) -> None:
        """Drop the pending action as well as the dialog, so Escape and Cancel
        cannot leave a write armed for the next confirmation to fire."""
        self._confirm_action = None
        dpg.configure_item("confirm_dialog", show=False)

    def _confirm_accept(self) -> None:
        action, self._confirm_action = self._confirm_action, None
        dpg.configure_item("confirm_dialog", show=False)
        if action is not None:
            action()

    # ------------------------------------------------------------- directories

    def _build_dir_picker(self) -> None:
        with dpg.window(
            label="Change runs directory",
            modal=True,
            show=False,
            tag="dir_picker",
            width=_px(620),
            height=_px(260),
            no_resize=True,
        ):
            dpg.add_text(
                "A directory of .tine files, or an opentine v3 repository.",
                color=TEXT_MUTED,
                wrap=_px(580),
            )
            dpg.add_input_text(
                tag="dir_picker_input",
                default_value=_sanitize(str(self._runs_dir)),
                width=-1,
                on_enter=True,
                callback=self._apply_dir,
            )
            dpg.add_text("Recent", color=TEXT_MUTED)
            dpg.add_listbox(
                _recent_dirs(self._preferences) or ["(none yet)"],
                tag="dir_recent",
                width=-1,
                num_items=4,
                callback=self._pick_recent_dir,
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Open", width=_px(110), callback=self._apply_dir)
                dpg.add_button(
                    label="Cancel",
                    width=_px(110),
                    callback=lambda: dpg.configure_item("dir_picker", show=False),
                )

    def _open_dir_picker(self) -> None:
        dpg.set_value("dir_picker_input", _sanitize(str(self._runs_dir)))
        dpg.configure_item("dir_recent", items=_recent_dirs(self._preferences) or ["(none yet)"])
        self._center("dir_picker", (620, 260))

    def _pick_recent_dir(self, sender, app_data) -> None:
        if app_data and app_data != "(none yet)":
            dpg.set_value("dir_picker_input", _sanitize(str(app_data)))

    def _apply_dir(self, *_args) -> None:
        raw = str(dpg.get_value("dir_picker_input") or "").strip()
        if not raw:
            self._set_status("Type a directory to open")
            return
        self._open_directory(_expand_user(raw))
        dpg.configure_item("dir_picker", show=False)

    def _open_directory(self, new_dir: Path) -> None:
        self._runs_dir = new_dir
        self._source = open_source(new_dir)
        # Not writable until a scan of the new source says so, and nothing
        # pending from the old one.
        self._snapshot = Snapshot(root=new_dir)
        self._select_after_scan = None
        self._table_fingerprint = None
        self._graph_fingerprint = ()
        self._entries = []
        self._errors = []
        self._run_filter = ""
        self._step_filter = ""
        self._selected_key = None
        self._selected_run = None
        self._selected_step = None
        self._quote = None
        if dpg.does_item_exist("run_filter"):
            dpg.set_value("run_filter", "")
        if dpg.does_item_exist("step_filter"):
            dpg.set_value("step_filter", "")
        self._preferences["last_runs_dir"] = str(new_dir)
        self._preferences["last_filter"] = ""
        _remember_dir(self._preferences, str(new_dir))
        self._flush_preferences()
        dpg.set_value("detail_text", "Select a run")
        dpg.set_value("step_text", "Select a step in the DAG")
        self._clear_dag()
        self._render_run_table()
        self._loader.retarget(self._source)
        self._note("info", f"Opened {new_dir}")

    def _touch_preferences(self) -> None:
        self._preferences_dirty_at = time.monotonic()

    def _flush_preferences(self) -> None:
        self._preferences_dirty_at = None
        self._preferences.setdefault("last_runs_dir", str(self._runs_dir))
        try:
            _save_preferences(self._preferences)
        except OSError as e:
            self._note("warn", f"Preferences not saved: {e}")

    def _persist_preferences(self) -> None:
        """Kept for callers that want the write to happen now, not on a timer."""
        self._preferences["last_runs_dir"] = str(self._runs_dir)
        self._preferences["last_filter"] = self._run_filter
        self._flush_preferences()

    # -------------------------------------------------------------- viewport

    def _on_viewport_resize(self, *_args) -> None:
        """Scale panel widths and text wraps with the viewport; keep panels visible."""
        vw = dpg.get_viewport_client_width()
        left = max(_px(300), min(_px(460), int(vw * 0.26)))
        center = max(_px(380), min(_px(560), int(vw * 0.33)))
        if dpg.does_item_exist("panel_runs"):
            dpg.configure_item("panel_runs", width=left)
        if dpg.does_item_exist("panel_detail"):
            dpg.configure_item("panel_detail", width=center)
        for tag, wrap in (
            ("run_summary", left - _px(40)),
            ("err_text", left - _px(28)),
            ("detail_text", center - _px(28)),
            ("step_text", center - _px(28)),
            ("dag_summary", max(_px(320), vw - left - center - _px(90))),
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, wrap=wrap)
        # Four buttons plus inter-item spacing must fit the panel's content box;
        # no floor, or the row overflows and the last button is clipped.
        spacing = _px(8)
        button_w = max(_px(34), (left - _px(30) - 3 * spacing) // 4)
        for tag in ("btn_pause", "btn_resume", "btn_fork", "btn_diff"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, width=button_w)
        # Inspector headers share a row with their buttons; below this the
        # subtitle is dropped so the buttons keep their place.
        compact = center < _px(520)
        for tag, subtitle in (
            ("panel_detail_subtitle", "Trace metadata"),
            ("panel_step_subtitle", "Inputs, outputs, cost"),
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, show=not compact)
                dpg.set_value(tag, subtitle)
        # Only flag a rebuild when the resize actually changes the layout, and
        # let the main loop do it — see _apply_pending_relayout.
        cols = max(1, self._dag_avail_width() // _px(NODE_PITCH_X))
        if left != self._layout_left or cols != self._layout_dag_cols:
            self._relayout_pending = True
        self._layout_left = left
        self._layout_dag_cols = cols

    # ---------------------------------------------------------- write actions

    def _entry_for_write(self) -> RunEntry | None:
        """The selected row, if this console may write to where it came from."""
        entry = self._selected_entry()
        if entry is None:
            self._set_status("Select a run first")
            return None
        if not self._snapshot.writable or entry.path is None:
            self._note(
                "warn",
                "This is a v3 repository: writing here would append an object and move a "
                "branch. Use `tine repo-fork` / `tine repo-resume` instead.",
            )
            return None
        return entry

    def _signature_at_risk(self, path: Path) -> str:
        """What a re-save of this file would silently destroy, if anything.

        `Run.save` recomputes `metadata.integrity` from scratch, so it drops any
        signature block the file carried and clears the draft marker an autosave
        checkpoint uses. Both are one-way: the console cannot re-sign, because
        it holds no signing key.
        """
        try:
            stat_result = path.stat()
        except OSError:
            return ""
        losses = []
        signature = _verify_cached(path, stat_result, "signature", Run.verify_signature)
        if str(signature.get("state") or "") not in ("", "unsigned"):
            losses.append("its signature")
        integrity = _verify_integrity_cached(path, stat_result)
        if integrity.get("draft"):
            losses.append("its draft/autosave marker")
        if not losses:
            return ""
        return (
            f"Saving {path.name} will drop {' and '.join(losses)}: opentine rewrites the "
            "integrity block on every save, and this console holds no signing key.\n\n"
            "Continue?"
        )

    def _pause_selected(self) -> None:
        entry = self._entry_for_write()
        if entry is None:
            return
        run = entry.run
        if run.status != RunStatus.running:
            self._set_status("Select a running run to pause")
            return
        path = entry.path
        question = self._signature_at_risk(path) if path and path.exists() else ""
        if question:
            self._ask(question, lambda: self._do_pause(entry))
            return
        self._do_pause(entry)

    def _do_pause(self, entry: RunEntry) -> None:
        run, path = entry.run, entry.path
        if path is None:
            return
        try:
            if not path.exists():
                # The row is up to one refresh interval stale. Writing here would
                # not pause a run, it would recreate an artifact (and possibly the
                # directory) that something else deleted while it was on screen.
                self._loader.request(force=True)
                self._note("warn", f"{_oneline(run.id)} is gone from disk; nothing to pause")
                return
            # Reload before writing: the cached snapshot can be up to one
            # refresh interval stale, and pausing from it would truncate
            # steps a still-running agent has since written.
            fresh = Run.load(path)
            if fresh.status != RunStatus.running:
                self._loader.request(force=True)
                self._note(
                    "warn", f"{_oneline(run.id)} is no longer running ({fresh.status.value})"
                )
                return
            fresh.pause(path)
        except Exception as e:  # Run.load raises more than OSError on bad files
            self._note("error", f"Cannot pause: {e}")
            return
        _forget_run(path)
        self._loader.request(force=True)
        self._note("ok", f"Paused {_oneline(run.id)}")

    def _resume_selected(self) -> None:
        entry = self._entry_for_write()
        if entry is None:
            return
        if entry.run.status != RunStatus.paused:
            self._set_status("Select a paused run to resume")
            return
        path = entry.path
        question = self._signature_at_risk(path) if path and path.exists() else ""
        if question:
            self._ask(question, lambda: self._do_resume(entry))
            return
        self._do_resume(entry)

    def _do_resume(self, entry: RunEntry) -> None:
        run, path = entry.run, entry.path
        if path is None:
            return
        try:
            # Same freshness rule as pause: never flip a status another process
            # already moved past paused (e.g. completed) since the last refresh.
            fresh = Run.load(path)
            if fresh.status != RunStatus.paused:
                self._loader.request(force=True)
                self._note("warn", f"{run.id} is no longer paused ({fresh.status.value})")
                return
            resumed = Run.resume(path)
            resumed.save(path)
        except Exception as e:
            self._note("error", f"Cannot resume: {e}")
            return
        _forget_run(path)
        self._selected_run = resumed
        self._loader.request(force=True)
        self._note("ok", f"Resumed {_oneline(resumed.id)}")

    def _fork_selected(self) -> None:
        """One-click fork onto main — the fast path."""
        self._do_fork()

    def _do_fork(
        self, *, branch: str = "main", reason: str = "", reproducible: bool = False
    ) -> None:
        entry = self._entry_for_write()
        step = self._selected_step
        if entry is None:
            return
        if not step:
            self._set_status("Select a step to fork from")
            return
        run = entry.run
        reason = reason.strip()
        if len(reason) > MAX_FORK_REASON:
            self._note("warn", f"Fork reason must be at most {MAX_FORK_REASON} characters")
            return
        try:
            source = entry.path
            fresh = Run.load(source) if source and source.exists() else run
            if fresh.get_step(step.id) is None:
                self._loader.request(force=True)
                self._note("warn", f"Step {step.id} no longer exists in {run.id}")
                return
            # Mirror opentine's own MCP fork: the reason enters the fork identity
            # via intent, and is also stored as plaintext. Note the plaintext is
            # NOT signed (opentine omits fork_reason from _SIGNED_METADATA_KEYS),
            # which is why the inspector re-derives the intent digest to decide
            # whether the shown reason is attested.
            new_run = fresh.fork(
                step.id,
                branch=branch or "main",
                intent={"reason": reason} if reason else None,
                nonce="" if reproducible else None,
            )
            if reason:
                new_run.metadata["fork_reason"] = reason
            out_path = _safe_run_path(self._runs_dir, new_run.id)
            if out_path.exists():
                # Refuse rather than clobber, the way opentine's own CLI
                # (_require_output_slot) and MCP fork do. A reproducible fork
                # (nonce="") derives the same id every time, so a second one
                # would otherwise overwrite the first — and any work done inside
                # it — with no error. Unconditional: it also catches a
                # hand-placed file colliding with a unique-act id.
                self._note(
                    "warn",
                    f"A run already exists at {out_path.name}; uncheck "
                    "'Reproducible id' or change the branch or reason",
                )
                return
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            new_run.save(out_path)
        except Exception as e:
            self._note("error", f"Cannot fork: {e}")
            return
        kept = retained_slice(fresh, step.id)
        # Leave the selection where it is until the rescan lists the fork, then
        # move to it. Pointing _selected_run at a run no row matches makes the
        # console's two ideas of "the selected run" disagree: the inspector
        # describes one run while every action reports there is no selection.
        self._select_after_scan = str(new_run.id)
        self._selected_step = None
        if dpg.does_item_exist("step_text"):
            dpg.set_value("step_text", "Select a step in the DAG")
        if dpg.does_item_exist("fork_dialog"):
            dpg.configure_item("fork_dialog", show=False)
        self._loader.request(force=True)
        where = f" on {branch}" if branch and branch != "main" else ""
        kept_note = f", keeping {len(kept)} step(s)" if kept is not None else ""
        self._note("ok", f"Forked {run.id}@{step.id}{where} -> {new_run.id}{kept_note}")

    def _build_fork_dialog(self) -> None:
        with dpg.window(
            label="Fork run",
            modal=True,
            show=False,
            tag="fork_dialog",
            width=_px(FORK_DIALOG_SIZE[0]),
            height=_px(FORK_DIALOG_SIZE[1]),
            no_resize=True,
        ):
            dpg.add_text("", tag="fork_subject", color=TEXT_SECONDARY, wrap=_px(560))
            dpg.add_text("", tag="fork_slice", color=BRAND, wrap=_px(560))
            dpg.add_separator()
            dpg.add_text("Branch", color=TEXT_MUTED)
            dpg.add_input_text(tag="fork_branch", default_value="main", width=-1)
            dpg.add_text("Reason (optional)", color=TEXT_MUTED)
            dpg.add_input_text(tag="fork_reason", width=-1, hint="why this fork exists")
            dpg.add_checkbox(label="Reproducible id (no random nonce)", tag="fork_reproducible")
            dpg.add_text(
                "Branch and reason are part of the fork id, so two forks of one step "
                "stay distinct runs. A reason is recorded but not signed.",
                color=TEXT_MUTED,
                wrap=_px(560),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Fork", callback=self._confirm_fork, width=_px(110))
                dpg.add_button(
                    label="Cancel",
                    width=_px(110),
                    callback=lambda: dpg.configure_item("fork_dialog", show=False),
                )

    def _open_fork_dialog(self) -> None:
        entry, step = self._selected_entry(), self._selected_step
        if not entry or not step:
            self._set_status("Select a step to fork from")
            return
        run = entry.run
        dpg.set_value("fork_subject", _oneline(f"Fork {run.id} at step {step.id}"))
        kept = retained_slice(run, step.id)
        if kept is None:
            dpg.set_value("fork_slice", "")
        else:
            causal = len(kept - {s.id for s in run.ancestors(step.id)}) if kept else 0
            note = f" ({causal} of them reached through causal edges)" if causal else ""
            dpg.set_value("fork_slice", f"Keeps {len(kept)} of {len(run.steps)} step(s){note}")
        dpg.set_value("fork_branch", "main")
        dpg.set_value("fork_reason", "")
        dpg.set_value("fork_reproducible", False)
        self._center("fork_dialog", FORK_DIALOG_SIZE)

    def _confirm_fork(self) -> None:
        self._do_fork(
            branch=(dpg.get_value("fork_branch") or "main").strip(),
            reason=dpg.get_value("fork_reason") or "",
            reproducible=bool(dpg.get_value("fork_reproducible")),
        )

    # ------------------------------------------------------------------ diff

    def _build_diff_dialog(self) -> None:
        with dpg.window(
            label="Compare runs",
            modal=True,
            show=False,
            tag="diff_dialog",
            width=_px(DIFF_DIALOG_SIZE[0]),
            height=_px(DIFF_DIALOG_SIZE[1]),
        ):
            dpg.add_text("", tag="diff_subject", color=TEXT_SECONDARY)
            with dpg.group(horizontal=True):
                dpg.add_listbox(
                    [],
                    tag="diff_candidates",
                    width=-_px(130),
                    num_items=6,
                    callback=self._compare_runs,
                )
                with dpg.group():
                    dpg.add_button(label="Compare", callback=self._compare_runs, width=_px(110))
                    dpg.add_button(label="Copy", callback=self._copy_diff, width=_px(110))
                    dpg.add_button(
                        label="Close",
                        width=_px(110),
                        callback=lambda: dpg.configure_item("diff_dialog", show=False),
                    )
            dpg.add_separator()
            with dpg.child_window(tag="diff_scroll", border=False):
                dpg.add_text(
                    "Pick a run to compare against.",
                    tag="diff_text",
                    wrap=_px(780),
                    color=TEXT_SECONDARY,
                )

    def _diff_label(self, entry: RunEntry) -> str:
        """A picker row a human can actually tell apart from the next one."""
        run = entry.run
        when = _format_age(entry.mtime or getattr(run, "created_at", 0.0))
        model = _truncate(_oneline(run.model_info) or "-", 18)
        return (
            f"{_elide_middle(_oneline(run.id), 22)}  {run.status.value:<9} "
            f"{model:<18} {len(run.steps):>3} steps  {_cost_cell(run)}  {when}"
        )

    def _open_diff_dialog(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._set_status("Select a run to compare")
            return
        others = [e for e in self._entries if e.key != entry.key]
        if not others:
            self._set_status("Need a second run in this source to compare")
            return
        # Keyed by label, so two runs whose rows render identically would
        # collapse into one and leave the other unreachable. An artifact owns its
        # whole id, so agreeing on the elided head and tail is something it can
        # simply choose to do; a repeated label gets a numbered suffix.
        self._diff_choices = {}
        labels: list[str] = []
        for other in others:
            label = self._diff_label(other)
            if label in self._diff_choices:
                label = f"{label}  #{len(labels) + 1}"
            self._diff_choices[label] = other.key
            labels.append(label)
        # A fork's origin is the comparison the user almost always wants.
        origin = str(entry.run.metadata.get("forked_from") or "")
        default = next(
            (label for label, key in self._diff_choices.items()
             if key == origin or str(self._snapshot.entry(key).run.id) == origin),
            labels[0],
        )
        dpg.configure_item("diff_candidates", items=labels, default_value=default)
        dpg.set_value("diff_candidates", default)
        dpg.set_value("diff_subject", _oneline(f"A: {entry.run.id}   - compare with:"))
        dpg.set_value("diff_text", "Pick a run and press Compare.")
        self._center("diff_dialog", DIFF_DIALOG_SIZE)

    def _compare_runs(self, *_args) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        label = dpg.get_value("diff_candidates")
        key = self._diff_choices.get(label)
        other = self._snapshot.entry(key) if key else None
        if other is None:
            dpg.set_value("diff_text", "That run is no longer loaded.")
            return
        try:
            body = _format_run_diff(entry.run, other.run)
        except Exception as e:
            body = f"Could not diff these runs: {e}"
        dpg.set_value("diff_text", self._panel_text(body.split("\n")))

    def _copy_diff(self) -> None:
        self._copy_to_clipboard(str(dpg.get_value("diff_text")), "diff")

    # ------------------------------------------------------------- transcript

    def _build_transcript_dialog(self) -> None:
        with dpg.window(
            label="Transcript",
            modal=True,
            show=False,
            tag="transcript_dialog",
            width=_px(TRANSCRIPT_DIALOG_SIZE[0]),
            height=_px(TRANSCRIPT_DIALOG_SIZE[1]),
        ):
            dpg.add_text("", tag="transcript_subject", color=TEXT_SECONDARY)
            dpg.add_text("", tag="transcript_summary", color=TEXT_MUTED, wrap=_px(800))
            dpg.add_separator()
            dpg.add_child_window(tag="transcript_body", border=False, height=-_px(44))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Copy all", width=_px(110), callback=self._copy_transcript)
                dpg.add_button(
                    label="Close",
                    width=_px(110),
                    callback=lambda: dpg.configure_item("transcript_dialog", show=False),
                )

    def _open_transcript(self) -> None:
        run = self._selected_run
        if run is None:
            self._set_status("Select a run to read its transcript")
            return
        for child in dpg.get_item_children("transcript_body", slot=1) or []:
            dpg.delete_item(child)
        dpg.set_value("transcript_subject", _oneline(f"Transcript of {run.id}"))
        turns = _transcript_turns(run)
        summary = _transcript_summary(run)
        if len(turns) > MAX_TRANSCRIPT_TURNS:
            summary += f" - showing the first {MAX_TRANSCRIPT_TURNS}"
            turns = turns[:MAX_TRANSCRIPT_TURNS]
        dpg.set_value("transcript_summary", summary)

        for index, turn in enumerate(turns):
            with dpg.group(horizontal=True, parent="transcript_body"):
                dpg.add_text(
                    _transcript_heading(turn),
                    color=TRANSCRIPT_ROLE_COLORS.get(turn["role"], TEXT_SECONDARY),
                )
                if turn["step_id"]:
                    # The turn knows which step it produced; jumping there is the
                    # reason to read a transcript beside a graph rather than alone.
                    dpg.add_button(
                        label="show step",
                        width=_px(84),
                        user_data=turn["step_id"],
                        callback=self._on_transcript_step,
                        tag=f"transcript_step_{index}",
                    )
            if turn.get("reasoning"):
                dpg.add_text(
                    f"reasoning: {turn['reasoning']}",
                    parent="transcript_body",
                    wrap=_px(780),
                    color=TEXT_FAINT,
                )
            dpg.add_text(
                _truncate(turn["content"], 4000) or "(empty)",
                parent="transcript_body",
                wrap=_px(780),
                color=TEXT_SECONDARY,
            )
            dpg.add_spacer(height=_px(6), parent="transcript_body")

        self._center("transcript_dialog", TRANSCRIPT_DIALOG_SIZE)

    def _copy_transcript(self) -> None:
        run = self._selected_run
        if run is None:
            return
        lines = []
        for turn in _transcript_turns(run):
            lines.append(_transcript_heading(turn))
            if turn.get("reasoning"):
                # The dialog renders this above the content; a copy that drops it
                # pastes an empty assistant turn with its explanation removed.
                lines.append(f"reasoning: {turn['reasoning']}")
            lines.append(turn["content"])
            lines.append("")
        self._copy_to_clipboard("\n".join(lines), "transcript")

    def _on_transcript_step(self, sender, app_data, user_data) -> None:
        run = self._selected_run
        if run is None:
            return
        step = run.get_step(user_data)
        if step is None:
            self._set_status(f"Step {user_data} is not in this run")
            return
        dpg.configure_item("transcript_dialog", show=False)
        self._reveal_step(step.id)
        self._set_status(f"Selected step {_truncate(user_data, 12)} from the transcript")

    # ---------------------------------------------------------------- export

    def _export_otel(self) -> None:
        """Write the selected run as an OTLP/JSON GenAI document.

        Read-only with respect to the artifact: it never rewrites the run, so it
        cannot disturb an integrity digest or a signature.
        """
        entry = self._selected_entry()
        if entry is None:
            self._set_status("Select a run to export")
            return
        if not otelio.export_available():
            self._note("warn", "OpenTelemetry export needs opentine 0.5.0 or newer")
            return
        run = entry.run
        try:
            out_path = _export_path(self._export_dir(), str(run.id))
        except ValueError as e:
            self._note("error", f"Cannot export: {e}")
            return
        if out_path.exists():
            self._ask(
                f"{out_path.name} already exists. Overwrite it?",
                lambda: self._write_export(run, out_path, overwrite=True),
            )
            return
        self._write_export(run, out_path, overwrite=False)

    def _export_dir(self) -> Path:
        """Where an export lands: beside the runs, or beside a repository.

        For a repository that is the worktree, never the `.tine` object store.
        Reading a repository writes nothing; an export is a file the user asked
        for, and it lands next to the store rather than inside it.
        """
        if self._snapshot.kind == "repository":
            root = Path(getattr(self._source, "root", self._runs_dir))
            # When the console was opened at the bare object directory, `root`
            # *is* the store, and "beside it" is one level up.
            if getattr(self._source, "tine_dir", None) == root:
                return root.parent
            return root
        return self._runs_dir

    def _write_export(self, run: Run, out_path: Path, *, overwrite: bool) -> None:
        try:
            self._export_dir().mkdir(parents=True, exist_ok=True)
            result = otelio.write_export(
                run,
                out_path,
                service_name=str(self._preferences.get("otel_service_name", "") or ""),
                overwrite=overwrite,
            )
        except Exception as e:
            self._note("error", f"Cannot export: {e}")
            return
        self._note(
            "ok",
            f"Exported {result.spans} span(s), {_format_bytes(result.bytes)} to {result.path.name}",
        )

    # ---------------------------------------------------------------- import

    def _open_import_dialog(self) -> None:
        if not self._snapshot.writable:
            self._note(
                "warn",
                "Open a .tine directory to import into; a v3 repository is read-only here",
            )
            return
        if not otelio.import_available():
            self._note("warn", "Importing needs opentine 0.5.0 or newer")
            return
        self._clear_panel_controls()
        dpg.add_text("File", parent="panel_controls", color=TEXT_MUTED)
        dpg.add_input_text(
            tag="import_path",
            parent="panel_controls",
            width=_px(360),
            hint="path to an OTLP/JSON, JSONL or framework log",
        )
        dpg.add_combo(
            list(otelio.IMPORT_FORMATS),
            tag="import_format",
            parent="panel_controls",
            width=_px(150),
            default_value=otelio.IMPORT_FORMATS[0],
        )
        dpg.add_button(
            label="Import", parent="panel_controls", width=_px(90), callback=self._do_import
        )
        self._show_panel(
            "Import a trace",
            f"Writes a new .tine artifact into {self._runs_dir}",
            "Choose a file exported by an OpenTelemetry collector or an agent framework.\n"
            "The importer never modifies the file it reads, and the run it writes is an "
            "ordinary .tine artifact this console can then open, fork and export.\n\n"
            f"Formats: {', '.join(otelio.IMPORT_FORMATS)}\n"
            "'auto' picks a format from the file's own content.",
        )

    def _do_import(self) -> None:
        raw = str(dpg.get_value("import_path") or "").strip()
        if not raw:
            self._set_status("Type the path of a trace file to import")
            return
        fmt = str(dpg.get_value("import_format") or "")
        source = _expand_user(raw)
        try:
            imported = otelio.import_file(source, fmt="" if fmt == "auto" else fmt)
        except Exception as e:
            self._note("error", f"Cannot import {source.name}: {e}")
            return
        try:
            written = otelio.save_imported(imported, self._runs_dir)
        except Exception as e:
            self._note("error", f"Imported, but could not save: {e}")
            return
        dpg.configure_item("panel_dialog", show=False)
        self._loader.request(force=True)
        note = f"Imported {imported.events} event(s) from {source.name} as {written.name}"
        self._note("ok", note)
        for warning in imported.warnings[:5]:
            self._note("warn", f"import: {warning}")

    # --------------------------------------------------------------- pricing

    def _open_pricing(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._set_status("Select a run to price")
            return
        self._clear_panel_controls()
        dpg.add_text("As of", parent="panel_controls", color=TEXT_MUTED)
        dpg.add_input_text(
            tag="pricing_date",
            parent="panel_controls",
            width=_px(120),
            hint="yyyy-mm-dd",
            default_value=str(self._preferences.get("pricing_as_of", "") or ""),
        )
        dpg.add_text("Provider", parent="panel_controls", color=TEXT_MUTED)
        # A rate card is keyed by provider and model together, and an imported
        # trace usually names only the model. Rather than guess a provider from
        # the model's spelling, the reader picks one and every figure derived
        # from that choice is labelled as assumed.
        dpg.add_combo(
            [RECORDED_PROVIDER, *pricing.catalog_providers()],
            tag="pricing_provider",
            parent="panel_controls",
            width=_px(150),
            default_value=str(self._preferences.get("pricing_provider", RECORDED_PROVIDER)),
            callback=self._recompute_price,
        )
        dpg.add_button(
            label="Recompute",
            parent="panel_controls",
            width=_px(110),
            callback=self._recompute_price,
        )
        self._recompute_price()

    def _recompute_price(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        as_of = ""
        if dpg.does_item_exist("pricing_date"):
            as_of = str(dpg.get_value("pricing_date") or "").strip()
        assume = RECORDED_PROVIDER
        if dpg.does_item_exist("pricing_provider"):
            assume = str(dpg.get_value("pricing_provider") or RECORDED_PROVIDER)
        if as_of:
            self._preferences["pricing_as_of"] = as_of
        self._preferences["pricing_provider"] = assume
        self._touch_preferences()
        try:
            quote = pricing.quote_run(
                entry.run,
                effective_at=as_of or None,
                assume_provider="" if assume == RECORDED_PROVIDER else assume,
            )
        except Exception as e:
            self._show_panel("Price this run", str(entry.run.id), f"Pricing failed: {e}")
            return
        self._quote = quote
        self._quote_key = entry.key
        body = "\n".join(pricing.quote_lines(quote, limit=40))
        if quote.available and quote.unknown and not quote.assumed_provider:
            missing = sum(1 for step in quote.steps if not step.provider)
            if missing:
                body += (
                    f"\n\n{missing} step(s) recorded no provider, and a rate card is keyed by "
                    "provider and model together. Pick one above to price them as if it had "
                    "served them; every figure from that choice is marked assumed."
                )
        self._show_panel(
            "Price this run",
            f"{_oneline(entry.run.id)} - {_recorded_phrase(entry.run)}",
            body
            + "\n\nRecorded cost is what the run itself claims. The figure above is what "
            "opentine's signed catalog says the same record is worth, computed here and "
            "never written back to the artifact.",
        )
        self._show_run_detail(entry)

    # ------------------------------------------------------------------ views

    def _open_stats(self) -> None:
        entries = self._visible_entries()
        if not entries:
            self._set_status("Nothing loaded to summarise")
            return
        group_by = str(self._preferences.get("stats_group_by", "status") or "status")
        self._clear_panel_controls()
        dpg.add_text("Group by", parent="panel_controls", color=TEXT_MUTED)
        dpg.add_combo(
            list(stats.GROUPINGS),
            tag="stats_group",
            parent="panel_controls",
            width=_px(170),
            default_value=group_by if group_by in stats.GROUPINGS else stats.GROUPINGS[0],
            callback=self._render_stats,
        )
        self._render_stats()

    def _render_stats(self, *_args) -> None:
        group_by = "status"
        if dpg.does_item_exist("stats_group"):
            group_by = str(dpg.get_value("stats_group"))
        self._preferences["stats_group_by"] = group_by
        self._touch_preferences()
        runs = [entry.run for entry in self._visible_entries()]
        try:
            result = stats.rollup(runs, group_by=group_by)
            body = "\n".join(stats.rollup_lines(result, limit=25))
        except Exception as e:
            body = f"Could not summarise these runs: {e}"
        scope = "filtered" if self._run_filter else "all"
        self._show_panel(
            "Statistics",
            f"{len(runs)} {scope} run(s) in {self._source.label}",
            body,
        )

    def _open_refs(self) -> None:
        if self._snapshot.kind != "repository":
            self._set_status("Refs are a v3 repository concept; this is a .tine directory")
            return
        self._clear_panel_controls()
        groups: dict[str, list[str]] = {}
        for name, oid in sorted(self._snapshot.refs.items()):
            head = str(name).split("/", 1)[0]
            groups.setdefault(head, []).append(f"  {_oneline(name)}  ->  {_short_oid(str(oid))}")
        lines: list[str] = []
        for head in sorted(groups):
            lines.append(f"{head} ({len(groups[head])})")
            lines.extend(groups[head])
            lines.append("")
        if self._snapshot.shallow:
            lines.append("This clone is shallow: history, diffs and context slices are truncated.")
        self._show_panel(
            "Repository refs",
            f"{self._source.label} - {len(self._snapshot.refs)} ref(s), "
            f"{len(self._entries)} run(s)",
            "\n".join(lines) or "This repository has no refs yet.",
        )

    # ------------------------------------------------------------- help/about

    def _build_help_dialog(self) -> None:
        with dpg.window(
            label="Help",
            modal=True,
            show=False,
            tag="help_dialog",
            width=_px(720),
            height=_px(560),
        ):
            dpg.add_text("", tag="help_text", wrap=_px(680), color=TEXT_SECONDARY)
            dpg.add_separator()
            dpg.add_button(
                label="Close",
                width=_px(110),
                callback=lambda: dpg.configure_item("help_dialog", show=False),
            )

    def _open_help(self) -> None:
        command = "Cmd" if sys.platform == "darwin" else "Ctrl"
        dpg.set_value(
            "help_text",
            "\n".join(
                [
                    "Keyboard",
                    "  Up / Down        move through the visible run list",
                    f"  {command}+F           focus the run search",
                    f"  {command}+C           copy the selected run id",
                    f"  {command}+R           reload the source now",
                    f"  {command}+O           change the runs directory",
                    "  F1               this help",
                    "  Esc              close a dialog, else clear the DAG filter, else the search",
                    "",
                    "Search",
                    "  Plain words are a substring search across ids, prompts, tags, metadata,",
                    "  step payloads and recorded providers.",
                    "  A field prefix switches to opentine's own grammar, the same one",
                    "  `tine ls` and `tine search` accept:",
                    "    status:failed  model:opus  tag:bug  cost:>0.01  cost:0.01..1",
                    "    after:2026-07-01  before:2026-08-01  text:retry",
                    "",
                    "The graph",
                    "  Grey links are execution lineage (parent -> child).",
                    "  Purple links are causal edges: a step that was required but is not a",
                    "  parent. A fork keeps those too, which is why they are drawn.",
                    "  'Next match' selects and scrolls to the next highlighted step.",
                    "",
                    "Sources",
                    "  A directory of .tine files can be paused, resumed and forked.",
                    "  An opentine v3 repository opens read-only: writing into one would",
                    "  append an object and move a branch, so those actions stay disabled.",
                    "",
                    "Trust",
                    "  Integrity covers the artifact body, not its metadata.",
                    "  A signature covers what its scheme says it covers; the panel names it.",
                    f"  Set {trust.HMAC_KEY_ENV} or {trust.PUBLIC_KEY_ENV} to verify"
                    " signatures here.",
                ]
            ),
        )
        self._center("help_dialog", (720, 560))

    def _open_about(self) -> None:
        lines = [
            f"opentine-gui {_GUI_VERSION}",
            f"opentine {_OPENTINE_VERSION}",
            f"Dear PyGui {_dearpygui_version()}",
            f"Python {sys.version.split()[0]} on {sys.platform}",
            "",
            f"Preferences: {_preferences_path()}",
            f"Source: {self._source.label}",
            f"Signing key: {self._trust.source or 'none configured'}",
            "",
            "Reads opentine .tine artifacts (format v2, v1 auto-migrated) and",
            "opentine v3 repositories, read-only.",
        ]
        self._show_panel("About", "opentine run console", "\n".join(lines))


def _recorded_phrase(run: Run) -> str:
    """How to say what the artifact itself claims about cost, in one clause."""
    recorded = _cost_text(run)
    if recorded == "no cost recorded":
        return "nothing was priced at capture"
    return f"recorded {recorded}"


def _dearpygui_version() -> str:
    try:
        return str(dpg.get_dearpygui_version())
    except Exception:
        return "unknown"


def run_app(runs_dir: Path | str | None = None) -> None:
    gui = OpentineGUI(Path(runs_dir) if runs_dir is not None else None)
    gui.run()
