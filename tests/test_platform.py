"""Cross-platform behavior: config locations, filename safety, display scaling.

Windows/macOS paths are exercised on any host by patching sys.platform, since
the functions under test only branch on it and use pure pathlib/env lookups.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opentine_gui import app
from opentine_gui.app import (
    _config_home,
    _detect_ui_scale,
    _load_preferences,
    _preferences_path,
    _px,
    _safe_run_path,
    _save_preferences,
    _windows_unsafe_name,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest sets OPENTINE_GUI_PREFS for isolation; these tests exercise the
    # default resolution, so it is cleared here too.
    for var in ("XDG_CONFIG_HOME", "APPDATA", "OPENTINE_GUI_PREFS", "OPENTINE_GUI_SCALE",
                "GDK_SCALE", "QT_SCALE_FACTOR"):
        monkeypatch.delenv(var, raising=False)


# ---- config locations ----

def test_config_home_is_platform_idiomatic(monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path("/home/tester")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    monkeypatch.setattr(app.sys, "platform", "linux")
    assert _config_home() == home / ".config"

    monkeypatch.setattr(app.sys, "platform", "darwin")
    assert _config_home() == home / "Library" / "Application Support"

    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\tester\\AppData\\Roaming")
    assert _config_home() == Path("C:\\Users\\tester\\AppData\\Roaming")

    # Windows without APPDATA still lands under the profile, not ~/.config.
    monkeypatch.delenv("APPDATA")
    assert _config_home() == home / "AppData" / "Roaming"


def test_xdg_config_home_wins_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/explicit/config")
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(app.sys, "platform", platform)
        assert _config_home() == Path("/explicit/config")


def test_preferences_env_override_beats_platform_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENTINE_GUI_PREFS", "/tmp/custom-prefs.json")
    assert _preferences_path() == Path("/tmp/custom-prefs.json")


def test_load_preferences_falls_back_to_legacy_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Simulate a macOS upgrade: settings still live in the old ~/.config path.
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"last_runs_dir": "/runs"}))
    monkeypatch.setattr(app, "_preferences_path", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(app, "_legacy_preferences_path", lambda: legacy)
    assert _load_preferences() == {"last_runs_dir": "/runs"}


def test_load_preferences_prefers_current_over_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = tmp_path / "current.json"
    legacy = tmp_path / "legacy.json"
    current.write_text(json.dumps({"last_runs_dir": "/new"}))
    legacy.write_text(json.dumps({"last_runs_dir": "/old"}))
    monkeypatch.setattr(app, "_preferences_path", lambda: current)
    monkeypatch.setattr(app, "_legacy_preferences_path", lambda: legacy)
    assert _load_preferences() == {"last_runs_dir": "/new"}


def test_save_preferences_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "preferences.json"
    _save_preferences({"last_runs_dir": "/runs"}, target)
    assert json.loads(target.read_text()) == {"last_runs_dir": "/runs"}
    assert [p.name for p in target.parent.iterdir()] == ["preferences.json"]

    # A second write replaces cleanly rather than appending or truncating.
    _save_preferences({"last_runs_dir": "/other"}, target)
    assert json.loads(target.read_text()) == {"last_runs_dir": "/other"}
    assert [p.name for p in target.parent.iterdir()] == ["preferences.json"]


# ---- Windows filename safety ----

@pytest.mark.parametrize(
    "run_id",
    ["CON", "con", "NUL", "nul", "AUX", "PRN", "COM1", "lpt9", "CON.tine", "aux.backup"],
)
def test_windows_reserved_device_names_flagged(run_id: str) -> None:
    assert _windows_unsafe_name(run_id)


@pytest.mark.parametrize("run_id", ["abc", "demo-complete", "run_2026-04-15.v1", "CONSOLE", "com"])
def test_ordinary_ids_not_flagged(run_id: str) -> None:
    assert not _windows_unsafe_name(run_id)


def test_trailing_dot_id_is_allowed() -> None:
    # Windows strips a trailing dot from a filename, but the id is always
    # suffixed: "abc." becomes "abc..tine", which is a perfectly legal name.
    assert not _windows_unsafe_name("abc.")


def test_trailing_space_flagged() -> None:
    # A trailing space *is* stripped from the resulting filename on Windows.
    assert _windows_unsafe_name("abc ")


def test_safe_run_path_rejects_reserved_names_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(app.sys, "platform", "win32")
    with pytest.raises(ValueError, match="Windows filename"):
        _safe_run_path(tmp_path, "CON")
    # ...and still allows them where they are legal.
    monkeypatch.setattr(app.sys, "platform", "linux")
    assert _safe_run_path(tmp_path, "CON").name == "CON.tine"


# ---- display scaling ----

def test_ui_scale_env_override_and_clamping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENTINE_GUI_SCALE", "1.5")
    assert _detect_ui_scale() == 1.5
    monkeypatch.setenv("OPENTINE_GUI_SCALE", "99")
    assert _detect_ui_scale() == 3.0
    monkeypatch.setenv("OPENTINE_GUI_SCALE", "0.01")
    assert _detect_ui_scale() == 0.5
    monkeypatch.setenv("OPENTINE_GUI_SCALE", "not-a-number")
    monkeypatch.setattr(app.sys, "platform", "linux")
    # Pin the platform probe: otherwise this asserts the reviewer's own display
    # DPI and fails on any HiDPI X session.
    monkeypatch.setattr(app, "_linux_dpi_scale", lambda: 1.0)
    assert _detect_ui_scale() == 1.0


def test_ui_scale_reads_linux_toolkit_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.sys, "platform", "linux")
    monkeypatch.setenv("GDK_SCALE", "2")
    assert _detect_ui_scale() == 2.0


def test_ui_scale_never_raises_on_hostile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app, "_windows_dpi_scale", lambda: 1 / 0)
    assert _detect_ui_scale() == 1.0


# ---- viewport geometry ----

def test_viewport_never_exceeds_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_screen_size", lambda: (1366, 768))
    monkeypatch.setattr(app, "_UI_SCALE", 2.0)  # a 2880x1720 window would not fit
    width, height, min_width, min_height = app._viewport_geometry()
    assert width <= 1366 and height <= 768


def test_viewport_minimum_stays_below_the_opening_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A minimum equal to the window opens it un-shrinkable, the opposite of the
    # clamp's purpose.
    monkeypatch.setattr(app, "_screen_size", lambda: (1280, 720))
    for scale in (1.0, 1.5, 2.0, 3.0):
        monkeypatch.setattr(app, "_UI_SCALE", scale)
        width, height, min_width, min_height = app._viewport_geometry()
        assert min_width < width, f"scale {scale}: min_width {min_width} == width {width}"
        assert min_height < height, f"scale {scale}: min_height {min_height} == height {height}"


def test_viewport_geometry_without_a_screen_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_screen_size", lambda: None)
    monkeypatch.setattr(app, "_UI_SCALE", 1.0)
    width, height, min_width, min_height = app._viewport_geometry()
    assert (width, height) == (1440, 860)
    assert min_width < width and min_height < height


def test_screen_size_is_skipped_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    # system_profiler is slow and reports backing pixels, not points.
    monkeypatch.setattr(app.sys, "platform", "darwin")
    assert app._screen_size() is None


def test_parse_xft_dpi_accepts_sane_values_only() -> None:
    assert app._parse_xft_dpi("Xft.dpi:\t192\nXft.hinting:\t1") == 192.0
    assert app._parse_xft_dpi("Xft.hinting:\t1") is None
    assert app._parse_xft_dpi("Xft.dpi:\t0") is None       # out of range
    assert app._parse_xft_dpi("Xft.dpi:\t99999") is None   # out of range
    assert app._parse_xft_dpi("Xft.dpi:\tnonsense") is None


# ---- font discovery ----

def test_font_override_is_used_when_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    face = tmp_path / "custom.ttf"
    face.write_bytes(b"not really a font, but a readable file")
    monkeypatch.setenv("OPENTINE_GUI_FONT", str(face))
    assert app._find_ui_font() == face


def test_font_override_missing_file_falls_back_to_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENTINE_GUI_FONT", str(tmp_path / "nope.ttf"))
    assert app._find_ui_font() is None


def test_font_candidates_are_probed_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = tmp_path / "second.ttf"
    second.write_bytes(b"font")
    monkeypatch.setattr(app.sys, "platform", "linux")
    monkeypatch.setitem(
        app.FONT_CANDIDATES, "linux", (str(tmp_path / "first-missing.ttf"), str(second))
    )
    assert app._find_ui_font() == second


def test_every_platform_has_font_candidates() -> None:
    for platform in ("win32", "darwin", "linux"):
        assert app.FONT_CANDIDATES.get(platform), f"no font candidates for {platform}"


def test_unknown_platform_degrades_to_builtin_font(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.sys, "platform", "freebsd13")
    assert app._find_ui_font() is None


def test_extra_glyph_ranges_cover_common_model_output() -> None:
    def covered(char: str) -> bool:
        return any(lo <= ord(char) <= hi for lo, hi in app.EXTRA_GLYPH_RANGES)

    # Above Latin-1, so the Default range hint does not include them; these are
    # exactly the characters that rendered as '?' before the extra ranges.
    for char in "—→✓€…":  # em dash, arrow, check, euro, ellipsis
        assert covered(char), f"{char!r} would render as a missing glyph"

    # Latin-1 (e.g. multiplication sign, accents) comes from mvFontRangeHint_Default.
    assert ord("×") < 0x100


def test_px_scales_design_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_UI_SCALE", 1.0)
    assert _px(340) == 340
    monkeypatch.setattr(app, "_UI_SCALE", 1.5)
    assert _px(340) == 510
    monkeypatch.setattr(app, "_UI_SCALE", 2.0)
    assert _px(NODE_PITCH := app.NODE_PITCH_X) == NODE_PITCH * 2
    # Never collapses a positive size to zero.
    assert _px(1) >= 1
