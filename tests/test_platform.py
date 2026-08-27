"""Cross-platform behavior: config locations, filename safety, display scaling.

Everything the console knows about the machine it runs on lives in
`opentine_gui.desktop`, and run-id safety in `opentine_gui.sources`; those are
the modules patched here, because the names `app` re-exports were bound at its
import and patching them would leave the real functions running underneath.

Windows/macOS paths are exercised on any host by patching sys.platform, since
the functions under test only branch on it and use pure pathlib/env lookups.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from opentine_gui import app, desktop, sources
from opentine_gui.desktop import (
    MAX_RECENT_DIRS,
    _config_home,
    _detect_ui_scale,
    _load_preferences,
    _preferences_path,
    _px,
    _recent_dirs,
    _remember_dir,
    _save_preferences,
    _screen_size,
    set_ui_scale,
)
from opentine_gui.sources import _safe_run_path, _windows_unsafe_name


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

    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert _config_home() == home / ".config"

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert _config_home() == home / "Library" / "Application Support"

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\tester\\AppData\\Roaming")
    assert _config_home() == Path("C:\\Users\\tester\\AppData\\Roaming")

    # Windows without APPDATA still lands under the profile, not ~/.config.
    monkeypatch.delenv("APPDATA")
    assert _config_home() == home / "AppData" / "Roaming"


def test_xdg_config_home_wins_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/explicit/config")
    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(desktop.sys, "platform", platform)
        assert _config_home() == Path("/explicit/config")
    # A shell that exports the variable unexpanded (or a hand-edited unit file)
    # would otherwise create a literal "~" directory in the cwd.
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/opentine-config")
    assert _config_home() == Path.home() / "opentine-config"


def test_preferences_env_override_beats_platform_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENTINE_GUI_PREFS", "/tmp/custom-prefs.json")
    assert _preferences_path() == Path("/tmp/custom-prefs.json")
    # An unexpanded "~" would make a literal "~" directory in the cwd on save.
    monkeypatch.setenv("OPENTINE_GUI_PREFS", "~/custom-prefs.json")
    assert _preferences_path() == Path.home() / "custom-prefs.json"


def test_load_preferences_falls_back_to_legacy_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Simulate a macOS upgrade: settings still live in the old ~/.config path.
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"last_runs_dir": "/runs"}))
    monkeypatch.setattr(desktop, "_preferences_path", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(desktop, "_legacy_preferences_path", lambda: legacy)
    assert _load_preferences() == {"last_runs_dir": "/runs"}


def test_load_preferences_prefers_current_over_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = tmp_path / "current.json"
    legacy = tmp_path / "legacy.json"
    current.write_text(json.dumps({"last_runs_dir": "/new"}))
    legacy.write_text(json.dumps({"last_runs_dir": "/old"}))
    monkeypatch.setattr(desktop, "_preferences_path", lambda: current)
    monkeypatch.setattr(desktop, "_legacy_preferences_path", lambda: legacy)
    assert _load_preferences() == {"last_runs_dir": "/new"}


def test_load_preferences_env_override_never_imports_the_legacy_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pointing the console at a scratch profile must not silently resurrect the
    # settings the user redirected away from.
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"last_runs_dir": "/old"}))
    monkeypatch.setenv("OPENTINE_GUI_PREFS", str(tmp_path / "absent.json"))
    monkeypatch.setattr(desktop, "_legacy_preferences_path", lambda: legacy)
    assert _load_preferences() == {}


@pytest.mark.parametrize(
    "body",
    ['["not", "a", "mapping"]', '"a string"', "null", "not json at all", ""],
)
def test_load_preferences_ignores_a_file_that_is_not_a_mapping(
    tmp_path: Path, body: str
) -> None:
    # A hand-edited or truncated profile must open the console with defaults,
    # not take the whole startup down.
    path = tmp_path / "preferences.json"
    path.write_text(body)
    assert _load_preferences(path) == {}


def test_load_preferences_drops_values_that_are_not_strings(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"last_runs_dir": "/runs", "recent_dirs": ["/a"], "scale": 2}))
    assert _load_preferences(path) == {"last_runs_dir": "/runs"}


def test_save_preferences_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "preferences.json"
    _save_preferences({"last_runs_dir": "/runs"}, target)
    assert json.loads(target.read_text()) == {"last_runs_dir": "/runs"}
    assert [p.name for p in target.parent.iterdir()] == ["preferences.json"]

    # A second write replaces cleanly rather than appending or truncating.
    _save_preferences({"last_runs_dir": "/other"}, target)
    assert json.loads(target.read_text()) == {"last_runs_dir": "/other"}
    assert [p.name for p in target.parent.iterdir()] == ["preferences.json"]


def test_save_preferences_writes_its_temp_beside_the_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # os.replace is only atomic within one filesystem, so a temp in the system
    # temp dir would degrade the swap to a copy — or fail outright.
    seen: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record(src, dst):
        seen.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(desktop.os, "replace", record)
    target = tmp_path / "nested" / "preferences.json"
    _save_preferences({"last_runs_dir": "/runs"}, target)
    source, destination = seen[0]
    assert source != destination, "renamed onto itself: that is a write in place, not a swap"
    assert source.parent == destination.parent == target.parent
    # ...and the swap actually landed, so the recorded call is not the whole test.
    assert json.loads(target.read_text()) == {"last_runs_dir": "/runs"}


def test_save_preferences_keeps_the_old_settings_when_the_swap_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The point of write-then-replace: a full disk loses the new settings, never
    # the ones already on disk, and never leaves a half-written file behind.
    target = tmp_path / "preferences.json"
    _save_preferences({"last_runs_dir": "/runs"}, target)

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(desktop.os, "replace", fail)
    with pytest.raises(OSError):
        _save_preferences({"last_runs_dir": "/elsewhere"}, target)
    assert json.loads(target.read_text()) == {"last_runs_dir": "/runs"}
    assert [p.name for p in tmp_path.iterdir()] == ["preferences.json"]


# ---- recent directories ----


def test_recent_dirs_are_newest_first_and_deduplicated() -> None:
    assert _recent_dirs({"recent_dirs": json.dumps(["/a", "/b", "/a", "/c"])}) == [
        "/a",
        "/b",
        "/c",
    ]


def test_recent_dirs_keeps_a_path_exactly_as_it_was_stored() -> None:
    # Both of these are legal POSIX directory names, and both were mangled while
    # the list was a newline-joined string: the first became two entries that do
    # not exist, the second lost its trailing space and opened nothing.
    awkward = ["/two\nlines", "/trailing ", " /leading", "/a"]
    assert _recent_dirs({"recent_dirs": json.dumps(awkward)}) == awkward


def test_recent_dirs_drops_entries_that_are_not_paths() -> None:
    # A hand-edited or foreign profile can hold anything; an empty row in the
    # picker selects nothing and looks broken.
    stored = json.dumps(["", "/a", None, 7, {"path": "/b"}, "/a", "/c"])
    assert _recent_dirs({"recent_dirs": stored}) == ["/a", "/c"]


def test_recent_dirs_are_capped_even_when_the_stored_value_is_huge() -> None:
    stored = json.dumps([f"/d{i}" for i in range(500)])
    entries = _recent_dirs({"recent_dirs": stored})
    assert len(entries) == MAX_RECENT_DIRS
    assert entries[0] == "/d0"


def test_recent_dirs_of_an_empty_profile_is_empty() -> None:
    assert _recent_dirs({}) == []
    assert _recent_dirs({"recent_dirs": ""}) == []


def test_recent_dirs_survives_a_stored_value_that_is_not_a_json_list() -> None:
    # Every one of these reached the picker at some point in this file's history:
    # the pre-0.3 newline encoding, a truncated write, and a JSON scalar.
    for stored in ("/a\n/b", "[not json", "null", '"/a"', "{}", "[[]]"):
        assert _recent_dirs({"recent_dirs": stored}) in ([], [[]])


def test_recent_dirs_survives_a_foreign_stored_shape(tmp_path: Path) -> None:
    # A profile whose recent_dirs is a JSON list rather than a string: loading
    # drops non-string values, so the picker opens empty instead of raising.
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"recent_dirs": ["/a", "/b"], "last_runs_dir": "/runs"}))
    preferences = _load_preferences(path)
    assert _recent_dirs(preferences) == []


def test_remember_dir_promotes_a_repeat_visit_to_the_front() -> None:
    preferences = {"recent_dirs": json.dumps(["/a", "/b"])}
    assert _remember_dir(preferences, "/b") == ["/b", "/a"]
    # Written back, not merely returned: the caller saves the dict, not the list.
    assert json.loads(preferences["recent_dirs"]) == ["/b", "/a"]


def test_remember_dir_stores_an_awkward_but_legal_path_verbatim() -> None:
    preferences: dict[str, str] = {}
    _remember_dir(preferences, "/runs with a trailing space ")
    _remember_dir(preferences, "/runs\nwith a newline")
    assert _recent_dirs(preferences) == [
        "/runs\nwith a newline",
        "/runs with a trailing space ",
    ]


def test_remember_dir_evicts_the_oldest_past_the_cap() -> None:
    preferences: dict[str, str] = {}
    returned: list[str] = []
    for i in range(MAX_RECENT_DIRS + 3):
        returned = _remember_dir(preferences, f"/d{i}")
    # The cap has to bite before the value is stored, not only when it is read
    # back: a profile that keeps every directory ever opened grows for the life
    # of the install and hands the picker a list it then has to truncate.
    assert len(returned) == MAX_RECENT_DIRS
    assert len(json.loads(preferences["recent_dirs"])) == MAX_RECENT_DIRS
    entries = _recent_dirs(preferences)
    assert len(entries) == MAX_RECENT_DIRS
    assert entries[0] == f"/d{MAX_RECENT_DIRS + 2}"
    assert "/d0" not in entries


def test_remember_dir_ignores_a_blank_directory() -> None:
    # Opening the picker and confirming with nothing typed must not push a blank
    # row onto the list. A path that is only whitespace is the same non-answer.
    preferences = {"recent_dirs": json.dumps(["/a"])}
    assert _remember_dir(preferences, "   ") == ["/a"]
    assert _remember_dir(preferences, "") == ["/a"]
    assert json.loads(preferences["recent_dirs"]) == ["/a"]



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
    monkeypatch.setattr(sources.sys, "platform", "win32")
    with pytest.raises(ValueError, match="Windows filename"):
        _safe_run_path(tmp_path, "CON")
    # ...and still allows them where they are legal.
    monkeypatch.setattr(sources.sys, "platform", "linux")
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
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    # Pin the platform probe: otherwise this asserts the reviewer's own display
    # DPI and fails on any HiDPI X session.
    monkeypatch.setattr(desktop, "_linux_dpi_scale", lambda: 1.0)
    assert _detect_ui_scale() == 1.0


def test_ui_scale_reads_linux_toolkit_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("GDK_SCALE", "2")
    assert _detect_ui_scale() == 2.0


def test_linux_scale_falls_back_to_the_x_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    # KDE, i3 and bare X sessions set Xft.dpi and export no toolkit variable at
    # all, so a console that only reads GDK_SCALE opens tiny there.
    monkeypatch.setattr(desktop, "_xrdb_dpi", lambda: 192.0)
    assert desktop._linux_dpi_scale() == 2.0


def test_linux_scale_ignores_an_unparseable_toolkit_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GDK_SCALE", "huge")
    monkeypatch.setattr(desktop, "_xrdb_dpi", lambda: 144.0)
    assert desktop._linux_dpi_scale() == 1.5


def test_ui_scale_follows_the_windows_system_dpi(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without this the win32 arm can be deleted outright and every other test
    # still passes, because the Linux arm answers 1.0 on a bare test host.
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_dpi_scale", lambda: 1.5)
    assert _detect_ui_scale() == 1.5
    # A nonsense reading from the OS is clamped like any other source.
    monkeypatch.setattr(desktop, "_windows_dpi_scale", lambda: 99.0)
    assert _detect_ui_scale() == 3.0


def test_ui_scale_is_pinned_to_one_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    # AppKit already hands the GL surface a Retina backing scale; scaling the
    # layout again would draw a 2x display at 4x. The Linux probe is armed with
    # a value that must not be consulted.
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "_linux_dpi_scale", lambda: 2.0)
    assert _detect_ui_scale() == 1.0


def test_ui_scale_never_raises_on_hostile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_dpi_scale", lambda: 1 / 0)
    assert _detect_ui_scale() == 1.0


def test_set_ui_scale_clamps_and_reports_what_it_pinned() -> None:
    assert set_ui_scale(1.5) == 1.5
    # A probe that reports nonsense must not be able to shrink every widget to a
    # single pixel or open a viewport larger than any panel.
    assert set_ui_scale(0.0) == 0.5
    assert set_ui_scale(-4.0) == 0.5
    assert set_ui_scale(12.0) == 3.0


def test_set_ui_scale_is_what_px_reads() -> None:
    set_ui_scale(2.0)
    assert _px(100) == 200
    set_ui_scale(1.0)
    assert _px(100) == 100


# ---- viewport geometry ----

def test_viewport_never_exceeds_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_screen_size", lambda: (1366, 768))
    monkeypatch.setattr(desktop, "_UI_SCALE", 2.0)  # a 2880x1720 window would not fit
    width, height, min_width, min_height = desktop._viewport_geometry()
    assert width <= 1366 and height <= 768


def test_viewport_minimum_stays_below_the_opening_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A minimum equal to the window opens it un-shrinkable, the opposite of the
    # clamp's purpose.
    monkeypatch.setattr(desktop, "_screen_size", lambda: (1280, 720))
    for scale in (1.0, 1.5, 2.0, 3.0):
        monkeypatch.setattr(desktop, "_UI_SCALE", scale)
        width, height, min_width, min_height = desktop._viewport_geometry()
        assert min_width < width, f"scale {scale}: min_width {min_width} == width {width}"
        assert min_height < height, f"scale {scale}: min_height {min_height} == height {height}"


def test_viewport_geometry_without_a_screen_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_screen_size", lambda: None)
    monkeypatch.setattr(desktop, "_UI_SCALE", 1.0)
    width, height, min_width, min_height = desktop._viewport_geometry()
    assert (width, height) == (1520, 900)
    assert min_width < width and min_height < height


def test_screen_size_is_skipped_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    # system_profiler is slow and reports backing pixels, not points.
    # `_screen_size` is bound here at import, before conftest replaces the module
    # attribute to keep the suite off the host display: this is the real probe.
    assert _screen_size is not desktop._screen_size, (
        "conftest replaced the module attribute with `lambda: None`; asserting "
        "against that instead of the real probe would make this test vacuous"
    )
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert _screen_size() is None


#: Two 2560x1440 panels side by side: "current" is the 5120-wide virtual
#: desktop, which is not a window size any single monitor can show.
XRANDR_DUAL_HEAD = """\
Screen 0: minimum 320 x 200, current 5120 x 1440, maximum 16384 x 16384
eDP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis) 344mm x 194mm
HDMI-1 connected 2560x1440+2560+0 (normal left inverted right x axis) 600mm x 340mm
"""


def _fake_xrandr(monkeypatch: pytest.MonkeyPatch, output: str | None) -> None:
    """Pin the Linux screen probe to `output`, or to no xrandr at all."""
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None if output is None else "/usr/bin/xrandr")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=output or "")
    )


def test_screen_size_prefers_the_primary_output_over_the_whole_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sizing the viewport off the virtual bounding box opens the console
    # straddling both monitors on a dual-head desktop.
    _fake_xrandr(monkeypatch, XRANDR_DUAL_HEAD)
    assert _screen_size() == (2560, 1440)


def test_screen_size_reads_a_single_head_without_a_primary_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_xrandr(
        monkeypatch,
        "Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384\n"
        "DP-1 connected 1920x1080+0+0 (normal left inverted right x axis) 510mm x 290mm\n",
    )
    assert _screen_size() == (1920, 1080)


def test_screen_size_is_none_when_xrandr_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wayland-only images and headless CI have no xrandr; the console must fall
    # back to its default geometry instead of raising during startup.
    _fake_xrandr(monkeypatch, None)
    assert _screen_size() is None


def test_screen_size_is_none_when_xrandr_says_nothing_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_xrandr(monkeypatch, "Screen 0: minimum 320 x 200, maximum 16384 x 16384\n")
    assert _screen_size() is None


def test_screen_size_rejects_a_zero_sized_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    # A locked or headless X session reports 0x0. (0, 0) is truthy to the
    # `if screen:` in _viewport_geometry, so letting it through opens the
    # console at its 640x480 floor -- with the minimum equal to the window, so
    # it cannot even be resized back.
    _fake_xrandr(monkeypatch, "Screen 0: minimum 320 x 200, current 0 x 0, maximum 16384 x 16384\n")
    assert _screen_size() is None


def test_parse_xft_dpi_accepts_sane_values_only() -> None:
    assert desktop._parse_xft_dpi("Xft.dpi:\t192\nXft.hinting:\t1") == 192.0
    assert desktop._parse_xft_dpi("Xft.hinting:\t1") is None
    assert desktop._parse_xft_dpi("Xft.dpi:\t0") is None       # out of range
    assert desktop._parse_xft_dpi("Xft.dpi:\t99999") is None   # out of range
    assert desktop._parse_xft_dpi("Xft.dpi:\tnonsense") is None


# ---- font discovery ----

def test_font_override_is_used_when_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    face = tmp_path / "custom.ttf"
    face.write_bytes(b"not really a font, but a readable file")
    monkeypatch.setenv("OPENTINE_GUI_FONT", str(face))
    assert desktop._find_ui_font() == face


def test_font_override_missing_file_falls_back_to_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENTINE_GUI_FONT", str(tmp_path / "nope.ttf"))
    assert desktop._find_ui_font() is None


def test_font_candidates_are_probed_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = tmp_path / "second.ttf"
    second.write_bytes(b"font")
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setitem(
        desktop.FONT_CANDIDATES, "linux", (str(tmp_path / "first-missing.ttf"), str(second))
    )
    assert desktop._find_ui_font() == second


def test_every_platform_has_font_candidates() -> None:
    for platform in ("win32", "darwin", "linux"):
        assert desktop.FONT_CANDIDATES.get(platform), f"no font candidates for {platform}"


def test_unknown_platform_degrades_to_builtin_font(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "freebsd13")
    assert desktop._find_ui_font() is None


def test_extra_glyph_ranges_cover_common_model_output() -> None:
    def covered(char: str) -> bool:
        return any(lo <= ord(char) <= hi for lo, hi in desktop.EXTRA_GLYPH_RANGES)

    # Above Latin-1, so the Default range hint does not include them; these are
    # exactly the characters that rendered as '?' before the extra ranges.
    for char in "—→✓€…":  # em dash, arrow, check, euro, ellipsis
        assert covered(char), f"{char!r} would render as a missing glyph"

    # Latin-1 (e.g. multiplication sign, accents) comes from mvFontRangeHint_Default.
    assert ord("×") < 0x100


def test_px_scales_design_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_UI_SCALE", 1.0)
    assert _px(340) == 340
    monkeypatch.setattr(desktop, "_UI_SCALE", 1.5)
    assert _px(340) == 510
    monkeypatch.setattr(desktop, "_UI_SCALE", 2.0)
    assert _px(NODE_PITCH := app.NODE_PITCH_X) == NODE_PITCH * 2


def test_px_never_collapses_a_hairline_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0.5 is the smallest scale set_ui_scale will pin, and round(0.5) is 0 --
    # so separators, borders and the 1px node outline would vanish rather than
    # draw thin. Asserting this at scale 1.0 or above proves nothing.
    monkeypatch.setattr(desktop, "_UI_SCALE", 0.5)
    assert _px(1) == 1
    assert _px(0.4) == 1
