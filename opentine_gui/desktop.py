"""Everything about the machine the console is running on.

Display scale, a monospace face that can draw more than ASCII, the usable
screen, and where this user's preferences live. Kept apart from the app so the
rest of the console can be tested without a display, and so each platform's
convention is stated once.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PREFERENCES_ENV = "OPENTINE_GUI_PREFS"
PREFERENCES_FILE = "preferences.json"
UI_SCALE_ENV = "OPENTINE_GUI_SCALE"
FONT_ENV = "OPENTINE_GUI_FONT"

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
        out = subprocess.run([exe, "-query"], capture_output=True, text=True, timeout=5).stdout
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


def set_ui_scale(scale: float) -> float:
    """Pin the scale every _px() call reads. Returns what was actually set."""
    global _UI_SCALE
    _UI_SCALE = min(3.0, max(0.5, float(scale)))
    return _UI_SCALE


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
    override = os.environ.get(FONT_ENV)
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
            match = (
                re.search(r"\bconnected\s+primary\s+(\d+)x(\d+)", out)
                or re.search(r"\bconnected(?:\s+primary)?\s+(\d+)x(\d+)", out)
                or re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
            )
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
    width, height = _px(1520), _px(900)
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


#: How many previously opened directories the picker offers. Small on purpose:
#: the list is a convenience, not a history the user has to curate.
MAX_RECENT_DIRS = 8


def _recent_dirs(preferences: dict[str, str]) -> list[str]:
    """Recently opened runs directories, newest first.

    Stored as a JSON array rather than a joined string. A POSIX directory name
    may legally contain a newline or a trailing space, and a line-based encoding
    turned one such directory into two entries that do not exist while losing
    the one that does.
    """
    raw = preferences.get("recent_dirs", "")
    try:
        parsed = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    seen: list[str] = []
    for entry in parsed:
        if isinstance(entry, str) and entry and entry not in seen:
            seen.append(entry)
    return seen[:MAX_RECENT_DIRS]


def _remember_dir(preferences: dict[str, str], directory: str) -> list[str]:
    """Push `directory` to the front of the recent list and store it back.

    Stored verbatim. A path is only rejected when it is empty or nothing but
    whitespace: " " is a legal POSIX directory name, and a helper that quietly
    strips it hands the picker a row that opens the wrong path.
    """
    if not directory or not directory.strip():
        return _recent_dirs(preferences)
    entries = [directory, *(d for d in _recent_dirs(preferences) if d != directory)]
    entries = entries[:MAX_RECENT_DIRS]
    preferences["recent_dirs"] = json.dumps(entries)
    return entries
