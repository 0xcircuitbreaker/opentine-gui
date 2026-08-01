"""Dear PyGui application — 3-panel desktop dashboard for opentine runs.

Layout:
  Left:   Run list (table) + actions
  Center: Run detail + selected-step detail
  Right:  DAG node editor (parent -> child)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import weakref
from pathlib import Path

import dearpygui.dearpygui as dpg
from opentine.core import Run, RunStatus, Step, StepKind

try:
    # opentine 0.4.0's only surface for the fork-id check. It is not exported
    # from opentine.core, the package root, Run, the CLI or MCP, so a private
    # import is the only option; a later rename must degrade, not crash.
    from opentine._fork_identity import verify_fork_id
except Exception:  # pragma: no cover - depends on the installed opentine
    verify_fork_id = None

try:
    # opentine 0.5.0. The declared floor is 0.4.0, so the export action is
    # offered only when the installed opentine actually provides it.
    from opentine import to_otel_genai_document
except Exception:  # pragma: no cover - depends on the installed opentine
    to_otel_genai_document = None

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")
MAX_TINE_BYTES = 10 * 1024 * 1024  # skip .tine files larger than 10 MiB

# Win32 device names. They resolve as devices whatever the extension or case, so
# "CON.tine" opens the console rather than a file.
WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _windows_unsafe_name(run_id: str) -> bool:
    """Whether <run_id>.tine is unusable as a filename on Windows.

    Pure and platform-independent so it can be tested anywhere; only enforced
    when actually running on Windows, since these names are valid elsewhere.
    """
    stem = run_id.split(".", 1)[0].upper()
    return stem in WINDOWS_RESERVED_STEMS or run_id.endswith(" ")


def _safe_run_path(runs_dir: Path, run_id: str) -> Path:
    """Return runs_dir/<id>.tine iff run_id is safe and resolves inside runs_dir."""
    if not SAFE_ID.fullmatch(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    if sys.platform == "win32" and _windows_unsafe_name(run_id):
        raise ValueError(f"run id is not a usable Windows filename: {run_id!r}")
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
TEXT_MUTED = [150, 142, 131]
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
MAX_FORK_REASON = 4096
# Kept in one place so the modal and its centering maths cannot drift apart.
FORK_DIALOG_SIZE = (560, 330)
NODE_PITCH_X = 250
NODE_PITCH_Y = 170
PREFERENCES_ENV = "OPENTINE_GUI_PREFS"
PREFERENCES_FILE = "preferences.json"
UI_SCALE_ENV = "OPENTINE_GUI_SCALE"

#: Set once at startup from the display's DPI; every hardcoded pixel size in the
#: layout goes through _px() so the console looks the same at 100% and 200%.
_UI_SCALE = 1.0


def _windows_set_dpi_aware() -> None:
    """Opt out of DWM bitmap-stretching before any window exists.

    Must run whatever the scale ends up being: if the process stays DPI-unaware
    while _px() also scales, Windows stretches an already-scaled window.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:  # per-monitor v2 (Win10 1703+): crisp text, correct on mixed-DPI setups
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:  # per-monitor v1
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _windows_dpi_scale() -> float:
    import ctypes

    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            return ctypes.windll.gdi32.GetDeviceCaps(hdc, 88) / 96.0  # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)


def _linux_dpi_scale() -> float:
    for var in ("GDK_SCALE", "QT_SCALE_FACTOR"):
        value = os.environ.get(var)
        if value:
            try:
                return float(value)
            except ValueError:
                continue
    # Xft.dpi is the X11-standard setting; KDE, i3 and bare X sessions set it
    # without exporting any toolkit variable.
    for source in (_xrdb_dpi, _xresources_dpi):
        dpi = source()
        if dpi:
            return dpi / 96.0
    return 1.0


def _xrdb_dpi() -> float | None:
    import shutil
    import subprocess

    exe = shutil.which("xrdb")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-query"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_xft_dpi(out)


def _xresources_dpi() -> float | None:
    try:
        return _parse_xft_dpi((Path.home() / ".Xresources").read_text(encoding="utf-8"))
    except OSError:
        return None


def _parse_xft_dpi(text: str) -> float | None:
    match = re.search(r"^\s*Xft\.dpi\s*:\s*([0-9.]+)", text, re.MULTILINE)
    if not match:
        return None
    try:
        dpi = float(match.group(1))
    except ValueError:
        return None
    return dpi if 48 <= dpi <= 480 else None


def _detect_ui_scale() -> float:
    """Display scale factor, 1.0 == 96 dpi. OPENTINE_GUI_SCALE overrides."""
    override = os.environ.get(UI_SCALE_ENV)
    if override:
        try:
            return min(3.0, max(0.5, float(override)))
        except ValueError:
            pass
    try:
        if sys.platform == "win32":
            return min(3.0, max(0.5, _windows_dpi_scale()))
        if sys.platform == "darwin":
            # AppKit already hands the GL surface a Retina backing scale;
            # scaling the layout again would double-count it.
            return 1.0
        return min(3.0, max(0.5, _linux_dpi_scale()))
    except Exception:
        return 1.0


def _px(value: float) -> int:
    """A design pixel in real device pixels at the current display scale."""
    return max(1, int(round(value * _UI_SCALE)))


#: Monospace faces with Latin-1/extended coverage, best first per platform. The
#: layout aligns text in columns, so a proportional face would ragged it out.
FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "win32": (
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\lucon.ttf",
        r"C:\Windows\Fonts\cour.ttf",
    ),
    "darwin": (
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Courier New.ttf",
    ),
    "linux": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
        "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    ),
}
FONT_SIZE = 15

#: Beyond ASCII+Latin-1: the punctuation, arrows and marks that routinely appear
#: in model output and would otherwise draw as '?'.
EXTRA_GLYPH_RANGES: tuple[tuple[int, int], ...] = (
    (0x0100, 0x017F),  # Latin Extended-A
    (0x2010, 0x205E),  # General Punctuation: dashes, quotes, ellipsis, bullets
    (0x20A0, 0x20BF),  # Currency symbols
    (0x2190, 0x21FF),  # Arrows
    (0x2200, 0x22FF),  # Mathematical operators
    (0x2500, 0x257F),  # Box drawing
    (0x2713, 0x2718),  # Check marks and ballots
)


def _find_ui_font() -> Path | None:
    """First readable monospace TTF for this platform, or None to keep DPG's default.

    DPG's built-in bitmap font is ASCII-only, so without this every accented
    character, CJK glyph or emoji in recorded agent output renders as '?'.
    """
    override = os.environ.get("OPENTINE_GUI_FONT")
    candidates = (override,) if override else FONT_CANDIDATES.get(sys.platform, ())
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _screen_size() -> tuple[int, int] | None:
    """Usable desktop size in device pixels, or None if it cannot be determined."""
    try:
        if sys.platform == "win32":
            import ctypes

            user32 = ctypes.windll.user32
            size = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        elif sys.platform == "darwin":
            # system_profiler takes seconds and reports backing-store pixels,
            # while the viewport is sized in points. AppKit already keeps a
            # window on-screen, so skip the probe entirely here.
            return None
        else:
            import shutil
            import subprocess

            exe = shutil.which("xrandr")
            if not exe:
                return None
            out = subprocess.run([exe], capture_output=True, text=True, timeout=2).stdout
            # Prefer the primary output's own geometry; "current" is the virtual
            # bounding box across all monitors, which is far too wide on a
            # multi-head desktop.
            match = re.search(r"\bconnected\s+primary\s+(\d+)x(\d+)", out) or re.search(
                r"\bconnected(?:\s+primary)?\s+(\d+)x(\d+)", out
            ) or re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
            if not match:
                return None
            size = (int(match.group(1)), int(match.group(2)))
        if size[0] > 0 and size[1] > 0:
            return size
    except Exception:
        return None
    return None


def _viewport_geometry() -> tuple[int, int, int, int]:
    """(width, height, min_width, min_height), never larger than the screen.

    At 150-200% scaling the scaled default (e.g. 2160x1290) exceeds many
    laptop panels, which would otherwise open the console partly offscreen with
    a minimum size too large to shrink back.
    """
    width, height = _px(1440), _px(860)
    min_width, min_height = _px(960), _px(600)
    screen = _screen_size()
    if screen:
        max_w = max(640, int(screen[0] * 0.95))
        max_h = max(480, int(screen[1] * 0.92))
        width, height = min(width, max_w), min(height, max_h)
    # The minimum must stay meaningfully below the opening size, or the window
    # opens at its own minimum and cannot be shrunk at all.
    min_width = min(min_width, max(640, width * 2 // 3))
    min_height = min(min_height, max(400, height * 2 // 3))
    return width, height, min_width, min_height


def _config_home() -> Path:
    """Per-user config directory following each platform's own convention.

    An explicit XDG_CONFIG_HOME wins everywhere (opentine's own catalog overlay
    honours it too, so a user who sets it keeps both in one place).
    """
    override = os.environ.get("XDG_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


def _preferences_path() -> Path:
    override = os.environ.get(PREFERENCES_ENV)
    if override:
        return Path(override).expanduser()
    return _config_home() / "opentine-gui" / PREFERENCES_FILE


def _legacy_preferences_path() -> Path:
    """Pre-0.2 location: ~/.config on every platform, including Windows/macOS."""
    return Path.home() / ".config" / "opentine-gui" / PREFERENCES_FILE


def _load_preferences(path: Path | None = None) -> dict[str, str]:
    if path is not None:
        candidates = [path]
    elif os.environ.get(PREFERENCES_ENV):
        # An explicit override means "use exactly this file"; falling back to the
        # default location would import settings the user redirected away from.
        candidates = [_preferences_path()]
    else:
        candidates = [_preferences_path(), _legacy_preferences_path()]
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    return {}


def _save_preferences(preferences: dict[str, str], path: Path | None = None) -> None:
    path = path or _preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(preferences, indent=2, sort_keys=True) + "\n"
    # Write-then-replace so a crash mid-write cannot truncate existing settings.
    # os.replace is atomic on POSIX and Windows alike.
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

_APP_THEME: int | None = None
_BUTTON_THEMES: dict[str, int] = {}


def _reset_theme_caches() -> None:
    """Theme ids die with their DPG context; a second run() must not reuse them."""
    global _APP_THEME
    _APP_THEME = None
    _BUTTON_THEMES.clear()
    _NODE_THEMES.clear()


def load_runs(
    runs_dir: Path,
) -> tuple[list[Run], list[str], tuple[tuple[str, float, int], ...], dict[str, Path]]:
    """Return (runs, errors, signature, paths) from one atomic directory scan.

    paths maps run.id to the file it was loaded from (newest mtime wins on
    duplicate ids) so actions write back to the real source file. Oversized
    files are skipped with an error rather than decoded, to bound memory/CPU
    on the auto-refresh loop. Integrity failures load but are surfaced as
    errors — a digest mismatch is a warning, not a parse failure.
    """
    runs: list[Run] = []
    errors: list[str] = []
    sig_entries: list[tuple[str, float, int]] = []
    paths: dict[str, Path] = {}
    if not runs_dir.exists():
        return runs, errors, (), paths
    if _is_v3_repository(runs_dir):
        # Say so instead of half-opening it: Run.load on a repository directory
        # redirects to heads/main, which would show one run out of many, and
        # Run.save would write a new object and move the branch.
        errors.append(
            f"{runs_dir.name}: this is an opentine v3 repository; this console "
            "reads loose .tine files (use `tine repo-log` for repositories)"
        )
        return runs, errors, (), paths
    files = []
    for f in runs_dir.glob("*.tine"):
        if f.is_dir():
            continue  # a repository's own .tine/ directory, not a run file
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
            run = Run.load(f)
        except Exception as e:
            errors.append(f"{f.name}: {e}")
            continue
        if run.id in paths:
            # Two files claiming one run id: actions can only target one path, and
            # a second identical row would be an unselectable dead click. Keep the
            # newest (files are mtime-sorted) and say which file is shadowed.
            # Short id: real ids are 64 hex chars, and the panel truncates rows.
            errors.append(f"{f.name}: duplicate run id {_truncate(run.id, 14)}, "
                          f"shadowed by {paths[run.id].name}")
            continue
        runs.append(run)
        paths[run.id] = f
        verdict = _verify_integrity_cached(f, st)
        if not verdict["ok"]:
            errors.append(f"{f.name}: integrity {verdict['reason']}")
    sig_entries.sort()
    return runs, errors, tuple(sig_entries), paths


#: Digest and signature checks re-read the whole file, so results are memoised
#: against (path, mtime, size): an unchanged file is verified once, not on every
#: refresh of a directory a live agent keeps touching.
_VERIFY_CACHE: dict[tuple[str, float, int, str], dict[str, object]] = {}
_VERIFY_CACHE_MAX = 512


def _verify_cached(path: Path, stat_result, kind: str, check) -> dict[str, object]:
    """Cached IntegrityResult/SignatureResult fields for one file revision.

    The key includes inode and change time, not just size and mtime: an integrity
    check exists to catch tampering, and `os.utime` lets a writer restore mtime
    after a same-length edit. POSIX will not let it backdate st_ctime, so any
    rewrite still misses the cache. (On Windows st_ctime is creation time, so a
    same-size, mtime-restored rewrite there can still be served from cache until
    the file changes again.)
    """
    key = (
        str(path),
        stat_result.st_mtime_ns,
        getattr(stat_result, "st_ctime_ns", 0),
        stat_result.st_size,
        stat_result.st_ino,
        kind,
    )
    hit = _VERIFY_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        result = check(path)
    except Exception as e:
        verdict: dict[str, object] = {"ok": False, "state": "error", "reason": f"check failed: {e}"}
    else:
        verdict = {
            "ok": bool(getattr(result, "ok", False)),
            "state": str(getattr(result, "state", "") or ""),
            "reason": str(getattr(result, "reason", "") or ""),
            "draft": bool(getattr(result, "draft", False)),
            "signer": getattr(result, "signer", None),
            "algorithm": getattr(result, "algorithm", None),
        }
    if len(_VERIFY_CACHE) >= _VERIFY_CACHE_MAX:
        _VERIFY_CACHE.clear()
    _VERIFY_CACHE[key] = verdict
    return verdict


def _verify_integrity_cached(path: Path, stat_result) -> dict[str, object]:
    return _verify_cached(path, stat_result, "integrity", Run.verify_integrity)


def _signature_line(verdict: dict[str, object]) -> str:
    """Render a SignatureResult by its state.

    ok=False is the normal case for both an unsigned run and a validly signed one
    the GUI holds no key for, so only a real mismatch or malformed block may read
    as an alarm.
    """
    state = str(verdict.get("state") or "")
    reason = str(verdict.get("reason") or "")
    signer = verdict.get("signer")
    who = f" by {signer}" if signer else ""
    if state == "verified":
        algorithm = verdict.get("algorithm")
        detail = f" ({algorithm})" if algorithm else ""
        return f"Signature: verified{who}{detail}"
    if state == "unsigned":
        return "Signature: unsigned"
    if state == "no-key":
        return f"Signature: present{who}, not verified here (no key)"
    if state == "mismatch":
        return f"Signature: INVALID{who} - {reason}"
    return f"Signature: {reason or state or 'unknown'}"


def _is_v3_repository(path: Path) -> bool:
    """Whether path is an opentine v3 repository, by the library's own rule.

    Matches both a worktree (<dir>/.tine/config.json) and the object directory
    itself (<dir>/config.json), which is what Run.load keys off when it silently
    redirects a directory to Repo.open(...).load_run('heads/main').
    """
    try:
        return path.is_dir() and (
            (path / "config.json").is_file() or (path / ".tine" / "config.json").is_file()
        )
    except OSError:
        return False


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
        self._run_paths: dict[str, Path] = {}
        self._selected_run: Run | None = None
        self._selected_step: Step | None = None
        self._last_signature: tuple = ()
        self._last_check: float = 0.0
        self._run_filter = self._preferences.get("last_filter", "").strip().lower()
        self._step_filter = ""
        self._layout_left = 0
        self._layout_dag_cols = 0
        self._relayout_pending = False
        self._pending_select: str | None = None
        self._force_refresh = False

    def run(self) -> None:
        global _UI_SCALE
        _windows_set_dpi_aware()  # before any window, and whatever the scale is
        _UI_SCALE = _detect_ui_scale()
        _reset_theme_caches()
        dpg.create_context()
        # Dear PyGui dispatches every callback on a dedicated non-main thread.
        # Selecting a run, typing in the DAG filter or confirming a fork all
        # delete and recreate hundreds of node-editor items, which races the
        # renderer mid-frame and crashes natively. Draining the queue ourselves
        # runs every callback on the render thread, between frames.
        dpg.configure_app(manual_callback_management=True)
        dpg.bind_theme(_app_theme())
        if not self._bind_ui_font() and _UI_SCALE != 1.0:
            # No TTF available: scale the built-in bitmap font instead.
            dpg.set_global_font_scale(_UI_SCALE)
        width, height, min_width, min_height = _viewport_geometry()
        dpg.create_viewport(
            title="opentine - agent run console",
            width=width,
            height=height,
            min_width=min_width,
            min_height=min_height,
        )

        with dpg.window(tag="primary"):
            with dpg.menu_bar():
                with dpg.menu(label="File"):
                    dpg.add_menu_item(label="Refresh", callback=self._refresh)
                    dpg.add_menu_item(label="Change runs dir...", callback=self._open_dir_picker)
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
                    dpg.add_separator()
                    dpg.add_menu_item(
                        label="Fork to branch...",
                        callback=self._open_fork_dialog,
                        tag="menu_fork_branch",
                    )
                    dpg.add_separator()
                    dpg.add_menu_item(
                        label="Export as OpenTelemetry JSON",
                        callback=self._export_otel,
                        tag="menu_export_otel",
                    )
                    dpg.add_separator()
                    dpg.add_menu_item(
                        label="Compare with...",
                        callback=self._open_diff_dialog,
                        tag="menu_diff",
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
                        tag="dir_picker", width=_px(560), height=_px(110), no_resize=True):
            dpg.add_input_text(
                tag="dir_picker_input",
                default_value=_sanitize(str(self._runs_dir)),
                width=-1,
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Apply", callback=self._apply_dir)
                dpg.add_button(
                    label="Cancel",
                    callback=lambda: dpg.configure_item("dir_picker", show=False),
                )

        self._build_diff_dialog()
        self._build_fork_dialog()
        self._build_key_bindings()

        dpg.set_primary_window("primary", True)
        dpg.set_viewport_resize_callback(self._on_viewport_resize)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        self._on_viewport_resize()
        self._refresh()
        while dpg.is_dearpygui_running():
            try:
                dpg.run_callbacks(dpg.get_callback_queue())
                self._apply_pending_input()
                self._apply_pending_relayout()
                self._auto_refresh_tick()
            except Exception as e:  # one bad .tine must not take down the console
                self._set_status(f"Refresh failed: {e}")
                self._last_signature = ()  # retry on the next tick
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

    def _build_key_bindings(self) -> None:
        """Global shortcuts. Handlers fire off the render thread, so anything
        that churns items records intent and lets the main loop apply it."""
        with dpg.handler_registry(tag="global_keys"):
            dpg.add_key_press_handler(dpg.mvKey_Down, callback=lambda: self._move_selection(1))
            dpg.add_key_press_handler(dpg.mvKey_Up, callback=lambda: self._move_selection(-1))
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self._on_escape)
            dpg.add_key_press_handler(dpg.mvKey_F, callback=self._on_ctrl_f)
            dpg.add_key_press_handler(dpg.mvKey_C, callback=self._on_ctrl_c)
            dpg.add_key_press_handler(dpg.mvKey_R, callback=self._on_ctrl_r)

    def _typing(self) -> bool:
        """True while a text field has focus, so keys reach the field, not us."""
        return any(
            dpg.does_item_exist(tag) and dpg.is_item_focused(tag)
            for tag in ("run_filter", "step_filter", "dir_picker_input",
                        "fork_branch", "fork_reason")
        )

    def _modal_open(self) -> str | None:
        for tag in ("fork_dialog", "diff_dialog", "dir_picker"):
            if dpg.does_item_exist(tag) and dpg.is_item_shown(tag):
                return tag
        return None

    def _move_selection(self, delta: int) -> None:
        if self._typing() or self._modal_open():
            return
        visible = self._filtered_runs()
        if not visible:
            return
        ids = [r.id for r in visible]
        if self._selected_run is None or self._selected_run.id not in ids:
            index = 0
        else:
            index = min(max(ids.index(self._selected_run.id) + delta, 0), len(ids) - 1)
        self._pending_select = ids[index]

    def _on_escape(self) -> None:
        modal = self._modal_open()
        if modal:
            dpg.configure_item(modal, show=False)
        elif self._step_filter:
            self._clear_step_filter()
        elif self._run_filter:
            dpg.set_value("run_filter", "")
            self._on_filter_change(None, "")

    def _on_ctrl_f(self) -> None:
        if dpg.is_key_down(dpg.mvKey_ModCtrl) and dpg.does_item_exist("run_filter"):
            dpg.focus_item("run_filter")

    def _on_ctrl_c(self) -> None:
        if dpg.is_key_down(dpg.mvKey_ModCtrl) and not self._typing():
            self._copy_run_id()

    def _on_ctrl_r(self) -> None:
        if dpg.is_key_down(dpg.mvKey_ModCtrl) and not self._typing():
            # An explicit flag, not a cleared signature: the tick is both rate
            # limited and change gated, and () is the signature of an empty
            # directory — so clearing it would reload everywhere except the one
            # case where the user most wants confirmation that nothing is there.
            self._force_refresh = True

    def _apply_pending_input(self) -> None:
        """Consume a keyboard-requested selection on the render thread."""
        run_id, self._pending_select = self._pending_select, None
        if run_id is not None and run_id != getattr(self._selected_run, "id", None):
            self._select_run(run_id)

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

    def _build_top_bar(self) -> None:
        with dpg.group(horizontal=True):
            dpg.add_text("opentine", color=TEXT_PRIMARY)
            dpg.add_text("agent run console", color=TEXT_MUTED)
            dpg.add_spacer(width=20)
            dpg.add_text(_sanitize(str(self._runs_dir)), tag="top_runs_dir", color=TEXT_MUTED)
        dpg.add_separator()

    def _on_viewport_resize(self, *_args) -> None:
        """Scale panel widths and text wraps with the viewport; keep status bar visible."""
        vw = dpg.get_viewport_client_width()
        left = max(_px(260), min(_px(360), int(vw * 0.24)))
        center = max(_px(360), min(_px(520), int(vw * 0.33)))
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
        # Inspector headers share a row with their Copy button; below this the
        # subtitle is dropped so the button keeps its place.
        compact = center < _px(430)
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

    def _build_run_list(self) -> None:
        with dpg.child_window(width=_px(340), height=-_px(34), border=True, tag="panel_runs"):
            _panel_header("Runs", "Search, select, and manage traces")
            dpg.add_input_text(
                hint="Search runs (id, status, model, text)",
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
                _action_button("Diff", self._open_diff_dialog, "btn_diff")
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
        with dpg.child_window(width=_px(480), height=-_px(34), border=True, tag="panel_detail"):
            with dpg.group(horizontal=True):
                _panel_header("Run inspector", "Trace metadata", "panel_detail_subtitle")
                dpg.add_spacer(width=_px(8))
                # Ids are hashes and get elided on screen; the CLI needs them whole.
                _action_button("Copy id", self._copy_run_id, "btn_copy_run", width=0)
            dpg.add_separator()
            dpg.add_text("Select a run", tag="detail_text", wrap=452, color=TEXT_SECONDARY)
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                _panel_header("Step inspector", "Inputs, outputs, cost", "panel_step_subtitle")
                dpg.add_spacer(width=_px(8))
                _action_button("Copy id", self._copy_step_id, "btn_copy_step", width=0)
            dpg.add_separator()
            dpg.add_text(
                "Select a step in the DAG",
                tag="step_text",
                wrap=452,
                color=TEXT_SECONDARY,
            )

    def _build_dag_panel(self) -> None:
        with dpg.child_window(border=True, height=-_px(34), tag="panel_dag"):
            _panel_header("Step DAG", "Parent-child execution graph")
            dpg.add_text(
                "Select a run to inspect its opentine step graph.",
                tag="dag_summary",
                wrap=580,
                color=TEXT_SECONDARY,
            )
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    hint="Highlight: id, kind, tool, payload (Enter)",
                    tag="step_filter",
                    width=_px(360),
                    callback=self._on_step_filter_change,
                    on_enter=True,
                )
                dpg.add_button(label="Clear", callback=self._clear_step_filter, width=_px(70))
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
            dpg.set_value("status_bar", _oneline(msg))

    def _auto_refresh_tick(self) -> None:
        now = time.monotonic()
        if self._force_refresh:
            self._force_refresh = False
            self._last_check = now
            self._refresh()
            return
        if now - self._last_check < AUTO_REFRESH_SECONDS:
            return
        self._last_check = now
        sig = _dir_signature(self._runs_dir)
        if sig != self._last_signature:
            self._refresh()

    def _refresh(self) -> None:
        self._runs, self._errors, self._last_signature, self._run_paths = load_runs(
            self._runs_dir
        )
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
                self._rebuild_dag(match, highlight=self._current_matches(match))
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
            dir_str = _sanitize(str(self._runs_dir))
            if len(dir_str) > 64:
                dir_str = "..." + dir_str[-61:]
            dpg.set_value("top_runs_dir", dir_str)
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
        id_width = _px(130)
        if dpg.does_item_exist("panel_runs"):
            panel_w = dpg.get_item_configuration("panel_runs")["width"]
            id_width = max(_px(96), min(_px(170), panel_w - _px(162)))
        # Budget the real cell: frame padding plus the 2-char selection prefix.
        id_chars = max(9, (id_width - _px(20)) // _px(8) - 2)
        for run in visible_runs:
            with dpg.table_row(parent="run_table"):
                color = RUN_STATUS_COLORS.get(run.status, [255, 255, 255])
                selected = self._selected_run is not None and self._selected_run.id == run.id
                short = _elide_middle(run.id, id_chars)
                label = f"> {short}" if selected else short
                dpg.add_button(
                    label=label,
                    callback=self._on_run_selected,
                    user_data=run.id,
                    width=id_width,
                )
                dpg.add_text(run.status.value, color=color)
                dpg.add_text(_cost_text(run))
        dpg.set_value("run_summary", _run_list_summary(self._runs, visible_runs, self._run_filter))
        if not visible_runs:
            with dpg.table_row(parent="run_table"):
                msg = "No runs match filter" if self._run_filter else "No .tine runs found"
                dpg.add_text(msg, color=[150, 150, 150])
                dpg.add_text("")
                dpg.add_text("")

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
        self._rebuild_dag(self._selected_run, highlight=self._current_matches(run))
        self._render_run_table()
        self._update_action_state()

    def _on_filter_change(self, sender, app_data) -> None:
        self._run_filter = (app_data or "").strip().lower()
        self._persist_preferences()
        self._render_run_table()
        self._update_action_state()
        shown = len(self._filtered_runs())
        self._set_status(f"{self._runs_dir} - {shown}/{len(self._runs)} run(s) shown")

    def _current_matches(self, run: Run) -> set[str]:
        if not self._step_filter:
            return set()
        return set(_matching_steps(run, self._step_filter))

    def _on_step_filter_change(self, sender, app_data) -> None:
        self._step_filter = (app_data or "").strip().lower()
        run = self._selected_run
        matches = _matching_steps(run, self._step_filter) if run else []
        if run and dpg.does_item_exist("dag_summary"):
            dpg.set_value(
                "dag_summary",
                _dag_summary(run, self._step_filter, matches),
            )
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

    def _filtered_runs(self) -> list[Run]:
        return [run for run in self._runs if _run_matches_filter(run, self._run_filter)]

    def _update_action_state(self) -> None:
        run = self._selected_run
        can_pause = bool(run and run.status == RunStatus.running)
        can_resume = bool(run and run.status == RunStatus.paused)
        can_fork = bool(run and self._selected_step)
        can_diff = bool(run and any(r.id != run.id for r in self._runs))
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
            ("menu_export_otel", run is not None and to_otel_genai_document is not None),
            ("menu_diff", can_diff),
            ("btn_diff", can_diff),
            ("btn_diff_wrap", can_diff),
            ("btn_copy_run", run is not None),
            ("btn_copy_run_wrap", run is not None),
            ("btn_copy_step", self._selected_step is not None),
            ("btn_copy_step_wrap", self._selected_step is not None),
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=enabled)

    def _show_run_detail(self, run: Run) -> None:
        kind_counts: dict[str, int] = {}
        for step in run.steps:
            kind_counts[step.kind.value] = kind_counts.get(step.kind.value, 0) + 1
        stats = _graph_stats(run)
        run_id = str(run.id)
        lines = [
            f"Run: {run_id}" if len(run_id) <= 32 else f"Run: {run_id[:12]}...",
            f"Model: {_oneline(run.model_info) or '(none)'}",
            f"Status: {run.status.value}",
            f"Created: {_format_timestamp(run.created_at)}",
            _format_version_line(run),
            f"Steps: {len(run.steps)}",
            f"Step kinds: {_format_counts(kind_counts)}",
            (
                "Graph: "
                f"{stats['roots']} root(s), {stats['links']} link(s), "
                f"{stats['branches']} branch point(s), depth {stats['max_depth']}"
            ),
            f"Cost: {_cost_text(run)}",
            f"Tokens: {run.total_tokens}",
            f"Duration: {run.total_duration:.1f}s",
        ]
        pricing_line = _pricing_line(run)
        if pricing_line:
            lines.append(pricing_line)
        lines.extend(_cost_attribution_lines(run))
        budget_line = _budget_line(run)
        if budget_line:
            lines.append(budget_line)
        breach = _budget_breach_line(run)
        if breach:
            lines.append(breach)
        if run.tags:
            lines.append(f"Tags: {', '.join(_oneline(t) for t in sorted(run.tags))}")
        if run.refs:
            refs = ", ".join(
                f"{_oneline(name)} -> {_oneline(tip)}" for name, tip in run.refs.items()
            )
            lines.append(f"Refs: {refs}")
        lines.extend(self._trust_lines(run))
        if len(run_id) > 32:
            lines.append(f"Full id: {run_id}")
        lines.extend(["", "Prompt:", *_indent_block(_truncate(run.user_prompt or "", 700))])
        if run.system_prompt:
            lines.extend(
                ["", "System prompt:", *_indent_block(_truncate(run.system_prompt, 400))]
            )
        lineage = _fork_lineage_lines(run)
        if lineage:
            lines.append("")
            lines.extend(lineage)
        dpg.set_value("detail_text", _sanitize("\n".join(lines)))

    def _trust_lines(self, run: Run) -> list[str]:
        """Integrity digest and signature state for the run's file on disk."""
        path = self._run_paths.get(run.id)
        if path is None:
            return ["Integrity: (not on disk yet)"]
        try:
            stat_result = path.stat()
        except OSError as e:
            return [f"Integrity: unreadable ({e})"]

        integrity = _verify_integrity_cached(path, stat_result)
        reason = str(integrity["reason"])
        if integrity["ok"]:
            lines = ["Integrity: ok" + (" (draft)" if integrity.get("draft") else "")]
        elif reason.startswith("check failed"):
            lines = [f"Integrity: {reason}"]
        else:
            lines = [f"Integrity: FAILED - {reason}"]

        lines.append(
            _signature_line(_verify_cached(path, stat_result, "signature", Run.verify_signature))
        )
        return lines

    def _show_step_detail(self, step: Step) -> None:
        parents = ", ".join(_oneline(p) for p in step.parent_ids) if step.parent_ids else "(root)"
        lines = [
            f"ID: {_oneline(step.id)}",
            f"Kind: {step.kind.value}",
            f"Parents: {parents}",
            f"Model: {_oneline(step.model_info) or '(none)'}",
            f"Duration: {step.duration:.3f}s",
            f"Cost: ${step.cost:.6f}",
        ]
        if step.timestamp:
            lines.insert(4, f"Time: {_format_timestamp(step.timestamp)}")
        if step.usage:
            lines.append("")
            lines.append("Usage:")
            lines.extend(_mapping_lines(step.usage))
        if step.billing:
            lines.append("")
            lines.append("Billing:")
            lines.extend(_mapping_lines(step.billing))
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
        dpg.set_value("step_text", _sanitize("\n".join(lines)))

    def _clear_dag(self) -> None:
        # Links (slot 0) must go before nodes (slot 1): deleting a node that a
        # live link still references segfaults Dear PyGui's native layer.
        for link in dpg.get_item_children("dag_editor", slot=0) or []:
            dpg.delete_item(link)
        for child in dpg.get_item_children("dag_editor", slot=1) or []:
            dpg.delete_item(child)
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
        return max(_px(260), vw - (left or _px(340)) - (center or _px(480)) - _px(80))

    def _rebuild_dag(self, run: Run, highlight: set[str] | None = None) -> None:
        highlight = highlight or set()
        self._clear_dag()
        dpg.set_value(
            "dag_summary",
            _dag_summary(run, self._step_filter, highlight)
            if self._step_filter
            else _dag_summary(run),
        )
        in_attr: dict[str, int] = {}
        out_attr: dict[str, int] = {}
        depth = _step_depths(run)
        # Wrap depth columns into horizontal bands sized to the visible panel,
        # so whole graphs stay on screen instead of running off to the right.
        rows_at_depth: dict[int, int] = {}
        for step in run.steps:
            rows_at_depth[depth[step.id]] = rows_at_depth.get(depth[step.id], 0) + 1
        max_depth = max(rows_at_depth, default=0)
        pitch_x, pitch_y = _px(NODE_PITCH_X), _px(NODE_PITCH_Y)
        cols = max(1, self._dag_avail_width() // pitch_x)
        # Bucket depths by band once. Rescanning every depth for every band is
        # quadratic, and a legal run can hold ~15,900 steps within MAX_TINE_BYTES
        # — enough to freeze the render thread for over ten seconds.
        depths_by_band: dict[int, list[int]] = {}
        for d in rows_at_depth:
            depths_by_band.setdefault(d // cols, []).append(d)
        band_y: dict[int, int] = {}
        y_cursor = _px(20)
        for band in range(max_depth // cols + 1):
            band_y[band] = y_cursor
            band_rows = max(
                (rows_at_depth[d] for d in depths_by_band.get(band, ())), default=1
            )
            y_cursor += band_rows * pitch_y + _px(30)
        col_fill: dict[int, int] = {}
        for step in run.steps:
            d = depth[step.id]
            row = col_fill.get(d, 0)
            col_fill[d] = row + 1
            band, cx = divmod(d, cols)
            pos = [_px(20) + cx * pitch_x, band_y[band] + row * pitch_y]
            color = STEP_COLORS.get(step.kind, [255, 255, 255])
            is_match = step.id in highlight
            label = _node_label(step, highlighted=is_match)
            node_id = dpg.add_node(
                parent="dag_editor",
                label=label,
                pos=pos,
                user_data=step.id,
            )
            dpg.bind_item_theme(node_id, _node_theme(color, highlighted=is_match))

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
                width=_px(80),
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

    def _build_diff_dialog(self) -> None:
        with dpg.window(
            label="Compare runs",
            modal=True,
            show=False,
            tag="diff_dialog",
            width=_px(760),
            height=_px(560),
            no_resize=False,
        ):
            dpg.add_text("", tag="diff_subject", color=TEXT_SECONDARY)
            with dpg.group(horizontal=True):
                dpg.add_listbox(
                    [],
                    tag="diff_candidates",
                    width=_px(300),
                    num_items=6,
                    callback=self._compare_runs,
                )
                with dpg.group():
                    dpg.add_button(label="Compare", callback=self._compare_runs, width=_px(110))
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
                    wrap=_px(720),
                    color=TEXT_SECONDARY,
                )

    def _copy_to_clipboard(self, value: str, label: str) -> None:
        try:
            dpg.set_clipboard_text(value)
        except Exception as e:
            self._set_status(f"Could not copy {label}: {e}")
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

    def _open_diff_dialog(self) -> None:
        run = self._selected_run
        if not run:
            self._set_status("Select a run to compare")
            return
        others = [str(r.id) for r in self._runs if r.id != run.id]
        if not others:
            self._set_status("Need a second run in this directory to compare")
            return
        # A fork's origin is the comparison the user almost always wants.
        origin = str(run.metadata.get("forked_from") or "")
        default = origin if origin in others else others[0]
        dpg.configure_item("diff_candidates", items=others, default_value=default)
        dpg.set_value("diff_candidates", default)
        dpg.set_value("diff_subject", _sanitize(f"A: {run.id}   - compare with:"))
        dpg.set_value("diff_text", "Pick a run and press Compare.")
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        dpg.configure_item(
            "diff_dialog",
            show=True,
            pos=[max(0, (vw - _px(760)) // 2), max(0, (vh - _px(560)) // 2)],
        )

    def _compare_runs(self, *_args) -> None:
        run = self._selected_run
        if not run:
            return
        other_id = dpg.get_value("diff_candidates")
        other = next((r for r in self._runs if str(r.id) == str(other_id)), None)
        if other is None:
            dpg.set_value("diff_text", f"Run {other_id} is no longer loaded.")
            return
        try:
            body = _format_run_diff(run, other)
        except Exception as e:
            body = f"Could not diff these runs: {e}"
        dpg.set_value("diff_text", _sanitize(body))

    def _open_dir_picker(self) -> None:
        dpg.set_value("dir_picker_input", _sanitize(str(self._runs_dir)))
        vw = dpg.get_viewport_client_width()
        vh = dpg.get_viewport_client_height()
        dpg.configure_item(
            "dir_picker",
            show=True,
            pos=[max(0, (vw - _px(560)) // 2), max(0, (vh - _px(110)) // 2)],
        )

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

    def _run_path(self, run: Run) -> Path:
        """The file this run was loaded from; falls back to <id>.tine for new runs."""
        path = self._run_paths.get(run.id)
        if path is not None:
            return path
        return _safe_run_path(self._runs_dir, run.id)

    def _pause_selected(self) -> None:
        run = self._selected_run
        if not run or run.status != RunStatus.running:
            self._set_status("Select a running run to pause")
            return
        try:
            path = self._run_path(run)
            if path.exists():
                # Reload before writing: the cached snapshot can be up to one
                # refresh interval stale, and pausing from it would truncate
                # steps a still-running agent has since written.
                fresh = Run.load(path)
                if fresh.status != RunStatus.running:
                    self._refresh()
                    self._set_status(f"{run.id} is no longer running ({fresh.status.value})")
                    return
            else:
                self._runs_dir.mkdir(parents=True, exist_ok=True)
                fresh = run
            fresh.pause(path)
        except Exception as e:  # Run.load raises more than OSError on bad files
            self._set_status(f"Cannot pause: {e}")
            return
        self._refresh()
        self._set_status(f"Paused {run.id}")

    def _resume_selected(self) -> None:
        run = self._selected_run
        if not run or run.status != RunStatus.paused:
            self._set_status("Select a paused run to resume")
            return
        try:
            path = self._run_path(run)
            # Same freshness rule as pause: never flip a status another process
            # already moved past paused (e.g. completed) since the last refresh.
            fresh = Run.load(path)
            if fresh.status != RunStatus.paused:
                self._refresh()
                self._set_status(f"{run.id} is no longer paused ({fresh.status.value})")
                return
            resumed = Run.resume(path)
            resumed.save(path)
        except Exception as e:
            self._set_status(f"Cannot resume: {e}")
            return
        self._selected_run = resumed
        self._refresh()
        self._set_status(f"Resumed {resumed.id}")

    def _fork_selected(self) -> None:
        """One-click fork onto main — the fast path."""
        self._do_fork()

    def _do_fork(
        self, *, branch: str = "main", reason: str = "", reproducible: bool = False
    ) -> None:
        run = self._selected_run
        step = self._selected_step
        if not run or not step:
            self._set_status("Select a step to fork from")
            return
        reason = reason.strip()
        if len(reason) > MAX_FORK_REASON:
            self._set_status(f"Fork reason must be at most {MAX_FORK_REASON} characters")
            return
        try:
            source = self._run_path(run)
            fresh = Run.load(source) if source.exists() else run
            if fresh.get_step(step.id) is None:
                self._refresh()
                self._set_status(f"Step {step.id} no longer exists in {run.id}")
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
                self._set_status(
                    f"A run already exists at {out_path.name}; uncheck "
                    "'Reproducible id' or change the branch or reason"
                )
                return
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            new_run.save(out_path)
        except Exception as e:
            self._set_status(f"Cannot fork: {e}")
            return
        self._selected_run = new_run
        self._selected_step = None
        if dpg.does_item_exist("step_text"):
            dpg.set_value("step_text", "Select a step in the DAG")
        if dpg.does_item_exist("fork_dialog"):
            dpg.configure_item("fork_dialog", show=False)
        self._refresh()
        where = f" on {branch}" if branch and branch != "main" else ""
        self._set_status(f"Forked {run.id}@{step.id}{where} -> {new_run.id}")

    def _export_otel(self) -> None:
        """Write the selected run as an OTLP/JSON GenAI document.

        Read-only: it never touches the artifact, so it cannot disturb an
        integrity digest or a signature.
        """
        run = self._selected_run
        if run is None:
            self._set_status("Select a run to export")
            return
        if to_otel_genai_document is None:
            self._set_status("OpenTelemetry export needs opentine 0.5.0 or newer")
            return
        try:
            document = to_otel_genai_document(run)
            out_path = _export_path(self._runs_dir, run.id)
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        except Exception as e:
            self._set_status(f"Cannot export: {e}")
            return
        spans = _span_count(document)
        self._set_status(f"Exported {spans} span(s) to {out_path.name}")

    def _open_fork_dialog(self) -> None:
        run, step = self._selected_run, self._selected_step
        if not run or not step:
            self._set_status("Select a step to fork from")
            return
        dpg.set_value("fork_subject", _sanitize(f"Fork {run.id} at step {step.id}"))
        dpg.set_value("fork_branch", "main")
        dpg.set_value("fork_reason", "")
        dpg.set_value("fork_reproducible", False)
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        dpg.configure_item(
            "fork_dialog",
            show=True,
            pos=[
                max(0, (vw - _px(FORK_DIALOG_SIZE[0])) // 2),
                max(0, (vh - _px(FORK_DIALOG_SIZE[1])) // 2),
            ],
        )

    def _confirm_fork(self) -> None:
        self._do_fork(
            branch=(dpg.get_value("fork_branch") or "main").strip(),
            reason=dpg.get_value("fork_reason") or "",
            reproducible=bool(dpg.get_value("fork_reproducible")),
        )

    def _build_fork_dialog(self) -> None:
        with dpg.window(
            label="Fork run", modal=True, show=False, tag="fork_dialog",
            width=_px(FORK_DIALOG_SIZE[0]), height=_px(FORK_DIALOG_SIZE[1]),
            no_resize=True,
        ):
            dpg.add_text("", tag="fork_subject", color=TEXT_SECONDARY)
            dpg.add_separator()
            dpg.add_text("Branch", color=TEXT_MUTED)
            dpg.add_input_text(tag="fork_branch", default_value="main", width=-1)
            dpg.add_text("Reason (optional)", color=TEXT_MUTED)
            dpg.add_input_text(
                tag="fork_reason", width=-1, hint="why this fork exists"
            )
            dpg.add_checkbox(
                label="Reproducible id (no random nonce)", tag="fork_reproducible"
            )
            dpg.add_text(
                "Branch and reason are part of the fork id in opentine 0.4.0.",
                color=TEXT_MUTED,
                wrap=_px(520),
            )
            with dpg.group(horizontal=True):
                dpg.add_button(label="Fork", callback=self._confirm_fork, width=_px(110))
                dpg.add_button(
                    label="Cancel", width=_px(110),
                    callback=lambda: dpg.configure_item("fork_dialog", show=False),
                )

    def _persist_preferences(self) -> None:
        self._preferences["last_runs_dir"] = str(self._runs_dir)
        self._preferences["last_filter"] = self._run_filter
        try:
            _save_preferences(self._preferences)
        except OSError as e:
            self._set_status(f"Preferences not saved: {e}")


def _rgba(color: list[int], alpha: int = 255) -> list[int]:
    return [color[0], color[1], color[2], alpha]


def _brighten(color: list[int], amount: int) -> list[int]:
    return [min(255, c + amount) for c in color[:3]]


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
            # Padding, spacing and scrollbars are sizes too: leaving them at 100%
            # while text and panels scale makes a HiDPI window look cramped.
            for target, x, y in (
                (dpg.mvStyleVar_WindowPadding, 12, 10),
                (dpg.mvStyleVar_FramePadding, 8, 5),
                (dpg.mvStyleVar_ItemSpacing, 8, 7),
                (dpg.mvStyleVar_ItemInnerSpacing, 6, 5),
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
            # Scaled like _app_theme's: an item theme overrides the global one,
            # so leaving these raw would un-scale every action button at HiDPI.
            dpg.add_theme_style(
                dpg.mvStyleVar_FrameRounding, _px(6), category=dpg.mvThemeCat_Core
            )
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


def _panel_header(title: str, subtitle: str, subtitle_tag: str | None = None) -> None:
    dpg.add_text(title, color=TEXT_PRIMARY)
    if subtitle_tag:
        dpg.add_text(subtitle, color=TEXT_MUTED, tag=subtitle_tag)
    else:
        dpg.add_text(subtitle, color=TEXT_MUTED)


def _action_button(label: str, callback, tag: str, width: int = 96) -> int | str:
    # DPG 2.x buttons ignore enabled= for click-blocking; a disabled wrapping
    # group both swallows clicks and applies the disabled styling.
    with dpg.group(tag=f"{tag}_wrap"):
        # width=0 lets Dear PyGui size the button to its label.
        item = dpg.add_button(
            label=label, callback=callback, tag=tag, width=_px(width) if width else 0
        )
    dpg.bind_item_theme(item, _button_theme("ghost"))
    return item


_NODE_THEMES: dict[tuple[int, int, int, bool], int] = {}


def _dim(color: list[int], factor: float) -> list[int]:
    return [int(c * factor) for c in color[:3]]


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


def _node_label(step: Step, *, highlighted: bool = False) -> str:
    kind = step.kind.value
    prefix = "* " if highlighted else ""
    if step.kind == StepKind.tool:
        name = (step.tool_info or {}).get("name") or step.inputs.get("name", "?")
        return f"{prefix}{kind}: {_truncate(name, 16)}"
    if step.kind == StepKind.error:
        error = step.error or {}
        text = (
            error.get("message")
            or error.get("type")
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
        return f"{prefix}{kind}: {_truncate(text, 18)}"
    return f"{prefix}{kind}: {_sanitize(step.short_id)}"


def _step_depths(run: Run) -> dict[str, int]:
    """Longest-path depth per step, iterative so 1000+-step chains don't overflow.

    Cycle back-edges contribute nothing (steps on a pure cycle get depth 0).
    """
    by_id = {step.id: step for step in run.steps}

    def valid_parents(sid: str) -> list[str]:
        return [p for p in by_id[sid].parent_ids if p in by_id and p != sid]

    memo: dict[str, int] = {}
    for step in run.steps:
        if step.id in memo:
            continue
        # frame: [step_id, parents, next_parent_index, best_parent_depth]
        stack: list[list] = [[step.id, valid_parents(step.id), 0, -1]]
        on_stack = {step.id}
        while stack:
            frame = stack[-1]
            sid, parents, idx, best = frame
            if idx < len(parents):
                frame[2] += 1
                parent = parents[idx]
                if parent in memo:
                    frame[3] = max(best, memo[parent])
                elif parent not in on_stack:
                    stack.append([parent, valid_parents(parent), 0, -1])
                    on_stack.add(parent)
            else:
                memo[sid] = best + 1 if best >= 0 else 0
                on_stack.discard(sid)
                stack.pop()
                if stack:
                    stack[-1][3] = max(stack[-1][3], memo[sid])
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
    partial = sum(1 for run in visible_runs if _pricing_incompleteness(run)[0])
    cost = f"{'>=' if partial else ''}${total_cost:.4f}"
    note = f" ({partial} run(s) partially priced)" if partial else ""
    return f"{shown} - {counts} - visible cost {cost}{note}"


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


#: The run filter fires on every keystroke on the render thread and otherwise
#: re-serialises every payload of every run. A loaded Run is not mutated, so its
#: lowercased search text is built once and dropped with the run itself.
#: (Step is a frozen dataclass holding lists, so it is unhashable and cannot be
#: cached this way — but per-step search only ever scans the selected run.)
_RUN_HAYSTACKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _step_haystack(step: Step) -> list[str]:
    # Fields like model_info may be None (or non-str) in third-party .tine
    # files that opentine loads without type-checking; coerce before joining.
    return [
        str(step.id),
        step.kind.value,
        " ".join(str(p) for p in step.parent_ids),
        str(step.model_info or ""),
        _format_value(step.inputs, 500),
        _format_value(step.outputs, 500),
        _format_value(step.tool_info, 500),
        _format_value(step.error, 500),
    ]


def _step_search_text(step: Step) -> str:
    return "\n".join(_step_haystack(step)).lower()


def _run_search_text(run: Run) -> str:
    # status is the one haystack field the GUI mutates in place (pause/resume),
    # so it is part of the cache validity check rather than just its content.
    status = run.status.value
    try:
        cached = _RUN_HAYSTACKS.get(run)
    except TypeError:  # unhashable Run subclass: fall back to recomputing
        cached = None
    if cached is not None and cached[0] == status:
        return cached[1]
    parts = [
        str(run.id),
        run.status.value,
        str(run.model_info or ""),
        str(run.user_prompt or ""),
        str(run.system_prompt or ""),
        " ".join(str(t) for t in run.tags),
        _format_value(run.metadata, 2000),
    ]
    parts.extend(_step_search_text(step) for step in run.steps)
    text = "\n".join(parts).lower()
    try:
        _RUN_HAYSTACKS[run] = (status, text)
    except TypeError:  # not weak-referenceable or unhashable
        pass
    return text


def _step_matches_filter(step: Step, query: str) -> bool:
    return query in _step_search_text(step)


def _run_matches_filter(run: Run, query: str) -> bool:
    if not query:
        return True
    return query in _run_search_text(run)


def _mapping_lines(data: dict, *, limit: int = 700) -> list[str]:
    if not data:
        return ["  (none)"]
    lines: list[str] = []
    for key, value in data.items():
        formatted = _format_value(value, limit)
        key_text = _oneline(key)
        if "\n" in formatted:
            lines.append(f"  {key_text}:")
            lines.extend(_indent_block(formatted, "    "))
        else:
            lines.append(f"  {key_text}: {_oneline(formatted)}")
    return lines


def _format_value(value: object, limit: int) -> str:
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        rendered = str(value)
    return _truncate(rendered, limit)


def _format_run_diff(left: Run, right: Run, *, max_steps: int = 25, max_fields: int = 8) -> str:
    """Human-readable semantic diff of two runs, using opentine's own Run.diff."""
    diff = left.diff(right)
    lines = [
        f"A: {left.id}",
        f"B: {right.id}",
        "",
        f"Common ancestor: {diff.common_ancestor or '(none - unrelated runs)'}",
        f"Cost: {_cost_text(left)} -> {_cost_text(right)}",
        f"Steps: {len(left.steps)} -> {len(right.steps)}",
        "",
    ]

    def step_list(label: str, steps) -> None:
        lines.append(f"{label} ({len(steps)}):")
        if not steps:
            lines.append("  (none)")
            return
        for step in steps[:max_steps]:
            lines.append(f"  {step.id[:12]}  {_node_label(step)}")
        if len(steps) > max_steps:
            lines.append(f"  ...and {len(steps) - max_steps} more")

    step_list("Only in A", diff.only_a)
    lines.append("")
    step_list("Only in B", diff.only_b)
    lines.append("")

    lines.append(f"Changed ({len(diff.changed)}):")
    if not diff.changed:
        lines.append("  (none)")
    for change in diff.changed[:max_steps]:
        a_id = getattr(change.step_a, "id", "?")
        b_id = getattr(change.step_b, "id", "?")
        lines.append(f"  {a_id[:12]} -> {b_id[:12]}")
        for delta in change.fields[:max_fields]:
            keys = f" [{', '.join(map(str, delta.changed_keys))}]" if delta.changed_keys else ""
            lines.append(f"    {delta.name}{keys}")
            lines.append(f"      - {_format_compact(delta.before, 160)}")
            lines.append(f"      + {_format_compact(delta.after, 160)}")
        if len(change.fields) > max_fields:
            lines.append(f"    ...and {len(change.fields) - max_fields} more field(s)")
    if len(diff.changed) > max_steps:
        lines.append(f"  ...and {len(diff.changed) - max_steps} more changed step(s)")
    return "\n".join(lines)


#: Problems that still yield a usable run in the list, unlike a parse failure.
_WARNING_MARKERS = (": integrity ", ": duplicate run id ")


def _split_load_problems(problems: list[str]) -> tuple[list[str], list[str]]:
    """(fatal, warnings) — files that failed to load vs. runs that loaded anyway."""
    fatal, warnings = [], []
    for problem in problems:
        (warnings if any(m in problem for m in _WARNING_MARKERS) else fatal).append(problem)
    return fatal, warnings


def _load_problem_header(fatal: int, warnings: int) -> str:
    parts = []
    if fatal:
        parts.append(f"{fatal} load error(s)")
    if warnings:
        parts.append(f"{warnings} warning(s)")
    return " / ".join(parts) if parts else "Load errors"


def _pricing_incompleteness(run: Run) -> tuple[bool, int, int]:
    """(incomplete, unpriced, total) from manifest.pricing; fails open on any shape.

    opentine records when its catalog could not price an invocation, which makes
    total_cost a lower bound rather than the spend. Nothing validates the shape
    of manifest.pricing, so every branch here tolerates arbitrary JSON.
    """
    try:
        pricing = run.manifest.get("pricing")
    except Exception:
        return (False, 0, 0)
    if not isinstance(pricing, dict) or pricing.get("complete") is not False:
        return (False, 0, 0)  # absent, True, or unreadable -> no caveat
    raw = pricing.get("invocations")
    if not isinstance(raw, list):
        return (True, 0, 0)  # the flag stands; counts unknown
    items = [i for i in raw if isinstance(i, dict)]
    unpriced = sum(1 for i in items if i.get("status") not in ("complete", "unmetered"))
    return (True, unpriced, len(items))


def _cost_text(run: Run, amount: float | None = None) -> str:
    """Cost with a '>=' marker when opentine flagged the pricing as incomplete."""
    value = run.total_cost if amount is None else amount
    prefix = ">=" if _pricing_incompleteness(run)[0] else ""
    return f"{prefix}${value:.4f}"


def _pricing_line(run: Run) -> str:
    incomplete, unpriced, total = _pricing_incompleteness(run)
    if not incomplete:
        return ""
    if total:
        return (
            f"Pricing: incomplete - {unpriced} of {total} invocation(s) unpriced "
            "(cost is a lower bound)"
        )
    return "Pricing: incomplete (cost is a lower bound)"


def _export_path(runs_dir: Path, run_id: str) -> Path:
    """Where an exported run lands: <runs_dir>/<id>.otel.json, id-safe.

    Reuses the run-id validation the write actions use, so an artifact cannot
    steer the export outside the runs directory.
    """
    safe = _safe_run_path(runs_dir, run_id)
    return safe.with_suffix(".otel.json")


def _span_count(document: object) -> int:
    """Spans in an OTLP document, tolerating any shape it might take."""
    try:
        return sum(
            len(scope.get("spans", []))
            for resource in document.get("resourceSpans", [])  # type: ignore[union-attr]
            for scope in resource.get("scopeSpans", [])
        )
    except Exception:
        return 0


def _fork_lineage_lines(run: Run) -> list[str]:
    """Where a fork came from, and which fork act it is.

    Since opentine 0.4.0 a fork id identifies the *act*, not the
    (source, point) coordinate, so two sibling forks share forked_from and
    fork_point while being different runs. The branch and whether the act
    carried a random nonce are what tell them apart. Pre-0.4.0 forks have no
    metadata.fork and simply render the origin line.
    """
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    basis = metadata.get("fork")
    basis = basis if isinstance(basis, dict) else None
    # forked_from is unprotected on unsigned artifacts; fall back to the fork
    # record so stripping it cannot silently downgrade a fork to a root run.
    origin = metadata.get("forked_from") or (basis.get("source") if basis else "")
    if not origin:
        return []
    point = metadata.get("fork_point") or (basis.get("point") if basis else "")
    lines = [
        f"Forked from: {_oneline(_truncate(origin, 120))}"
        + (f" at step {_oneline(_truncate(point, 80))}" if point else "")
    ]

    if isinstance(basis, dict):
        parts = []
        branch = basis.get("branch")
        if branch:
            parts.append(f"branch {branch}")
        nonce = basis.get("nonce")
        if isinstance(nonce, str):
            # An empty nonce is opentine's opt-in to a reproducible fork id;
            # anything else means this is one specific fork act among possible
            # siblings that share the same source and point.
            parts.append("reproducible" if nonce == "" else "unique act")
        if parts:
            lines.append(f"Fork: {', '.join(parts)}")
        # metadata sits outside the integrity digest, so a post-hoc edit to the
        # fork record still verifies "ok". This is the only check that catches it.
        if verify_fork_id is not None:
            try:
                verdict = verify_fork_id(run)
            except Exception:
                verdict = None
            if verdict is False:
                lines.append("Fork id: DOES NOT MATCH its recorded basis")
            elif verdict is True:
                lines.append("Fork id: verified against its recorded basis")
    reason = metadata.get("fork_reason")
    if reason:
        lines.append(f"{_fork_reason_label(basis, reason)}: {_oneline(_truncate(reason, 200))}")
    return lines


def _fork_reason_label(basis: object, reason: object) -> str:
    """"Fork reason", or flagged unverified when the text is not attested.

    opentine deliberately leaves `metadata.fork_reason` out of
    `_SIGNED_METADATA_KEYS` (for 0.3.0 signature compatibility) and the whole
    metadata block sits outside the integrity digest, so the plaintext can be
    rewritten on a signed, integrity-clean artifact. `metadata.fork.intent` IS
    signed and IS committed to by the run id, and it is sha256 over the
    canonical intent object — so a reason that reproduces it is bound to the
    fork act, and one that does not must not be shown as if it were.
    """
    if not isinstance(basis, dict) or not isinstance(reason, str):
        return "Fork reason (unverified)"
    recorded = basis.get("intent")
    if not isinstance(recorded, str):
        return "Fork reason (unverified)"
    # Byte-identical to opentine's own canonical encoding for this shape,
    # so no private module is imported.
    canonical = json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "Fork reason" if digest == recorded else "Fork reason (unverified)"


def _budget_line(run: Run) -> str:
    """Configured budget with the incurred total beside each limit, if any."""
    try:
        budget = run.budget()
    except Exception:
        return ""
    if budget is None:
        return ""
    parts: list[str] = []
    if budget.max_cost is not None:
        parts.append(f"cost {_cost_text(run)}/${budget.max_cost:.4f}")
    if budget.max_steps is not None:
        parts.append(f"steps {len(run.steps)}/{budget.max_steps}")
    if budget.max_duration is not None:
        parts.append(f"duration {run.total_duration:.1f}s/{budget.max_duration:.1f}s")
    if budget.max_usage is not None:
        parts.append(f"tokens {run.total_tokens}/{budget.max_usage}")
    if not parts:
        return ""
    return f"Budget: {', '.join(parts)} (on breach: {budget.on_breach})"


def _budget_breach_line(run: Run) -> str:
    """Why a run died, when opentine halted it for exceeding its budget.

    opentine records metadata['budget_state'] and sets status=failed. Without
    this the run looks like any other failure and the user hunts for a crash
    that never happened. metadata is untrusted and outside the integrity
    digest, so every field is treated as advisory.
    """
    state = run.metadata.get("budget_state") if isinstance(run.metadata, dict) else None
    if not isinstance(state, dict) or not state.get("breached"):
        return ""
    dimension = state.get("dimension") or "budget"
    incurred, limit = state.get("incurred"), state.get("limit")
    if incurred is None or limit is None:
        return f"Budget BREACHED: {dimension}"
    return f"Budget BREACHED: {dimension} {incurred} > {limit}"


def _cost_attribution_lines(run: Run, *, limit: int = 4) -> list[str]:
    """Where the money went, when more than one model or kind spent any."""
    try:
        breakdown = run.cost_breakdown()
    except Exception:
        return []
    lines: list[str] = []
    for label, mapping in (("model", breakdown.by_model), ("kind", breakdown.by_kind)):
        spenders = sorted(
            ((k, v) for k, v in (mapping or {}).items() if v), key=lambda kv: -kv[1]
        )
        if len(spenders) < 2:
            continue  # a single spender adds nothing over the Cost line
        shown = ", ".join(f"{k or '(unattributed)'} ${v:.4f}" for k, v in spenders[:limit])
        if len(spenders) > limit:
            shown += f", +{len(spenders) - limit} more"
        lines.append(f"Cost by {label}: {shown}")
    return lines


def _format_version_line(run: Run) -> str:
    migration = run.metadata.get("migration")
    if isinstance(migration, list) and migration:
        first = migration[0] if isinstance(migration[0], dict) else {}
        last = migration[-1] if isinstance(migration[-1], dict) else {}
        origin = first.get("from", "?")
        tool = last.get("tool", "?")
        return f"Format: v{run.format_version} (migrated from v{origin} by {tool})"
    return f"Format: v{run.format_version}"


def _format_compact(value: object, limit: int) -> str:
    """Single-line rendering — diff rows stay scannable where pretty-printing would not."""
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        rendered = json.dumps(value, sort_keys=True, separators=(", ", ": "))
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
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return f"(invalid timestamp: {timestamp!r})"


def _sanitize(s: str) -> str:
    """Replace lone surrogates: Dear PyGui's native text renderer segfaults on them."""
    if s.isascii():
        return s
    return s.encode("utf-8", "replace").decode("utf-8")


#: Line breaks and other C0/C1 controls, which would let artifact text open a new
#: row in a panel that is rendered as one flat block of text.
_LINE_BREAKS = re.compile(r"[\r\n\x0b\x0c\x85  ]+")
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")


def _oneline(value: object) -> str:
    """Collapse untrusted text to a single line.

    The run and step inspectors render as one flat text widget, so a newline in
    an artifact-supplied field (a model name, a tag, a prompt) would start a new
    row that is pixel-identical to the console's own — including the
    Integrity/Signature/Fork-id lines that state whether the artifact is
    trustworthy. Every interpolated artifact value goes through here so those
    verdicts cannot be forged by the file they describe.
    """
    text = _LINE_BREAKS.sub(" ", _sanitize(str(value)))
    return _CONTROLS.sub("", text.replace("\t", " ")).strip()


def _indent_block(text: str, prefix: str = "  ") -> list[str]:
    """Render possibly multi-line text with every line indented under a heading."""
    cleaned = _CONTROLS.sub("", _sanitize(str(text)).replace("\t", " "))
    return [f"{prefix}{line}" for line in _LINE_BREAKS.split(cleaned)] or [f"{prefix}"]


def _elide_middle(text: str, n: int) -> str:
    """Shorten keeping both ends, so ids sharing a prefix stay distinguishable.

    Run ids are commonly "demo-complete"/"demo-running" or a shared hash prefix;
    truncating only the tail renders them all identically.
    """
    text = _sanitize(str(text))
    if len(text) <= n:
        return text
    if n <= 3:
        return text[:n]
    keep = n - 1  # one char for the ellipsis
    head = (keep + 1) // 2
    return f"{text[:head]}…{text[len(text) - (keep - head):]}"


def _truncate(v: object, n: int) -> str:
    s = _sanitize(str(v))
    return s if len(s) <= n else s[: n - 3] + "..."


def run_app(runs_dir: Path | str | None = None) -> None:
    gui = OpentineGUI(Path(runs_dir) if runs_dir is not None else None)
    gui.run()
