"""Test isolation from the developer's real machine.

OpentineGUI.__init__ loads preferences eagerly, and the display-scale detection
shells out to the host's X resources. Without these fixtures the suite reads (and
could write) the real ~/.config profile, and its assertions would depend on the
DPI of whichever monitor happens to be attached.

The display probes and the scale live in `opentine_gui.desktop`, so that is what
is patched: patching the names re-exported by `app` would leave the real probes
running underneath.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opentine_gui import desktop, sources


@pytest.fixture(autouse=True)
def isolate_user_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    prefs: Path = tmp_path_factory.mktemp("prefs") / "preferences.json"
    monkeypatch.setenv("OPENTINE_GUI_PREFS", str(prefs))
    for var in (
        "XDG_CONFIG_HOME",
        "APPDATA",
        "OPENTINE_GUI_SCALE",
        "OPENTINE_GUI_FONT",
        "GDK_SCALE",
        "QT_SCALE_FACTOR",
        "OPENTINE_GUI_HMAC_KEY",
        "OPENTINE_GUI_PUBLIC_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Never probe the host display: no subprocess, no host-dependent assertions.
    monkeypatch.setattr(desktop, "_xrdb_dpi", lambda: None)
    monkeypatch.setattr(desktop, "_xresources_dpi", lambda: None)
    monkeypatch.setattr(desktop, "_screen_size", lambda: None)
    monkeypatch.setattr(desktop, "_UI_SCALE", 1.0)
    # Parsed runs and verification verdicts are cached across calls by file
    # revision. Two tests writing different bytes to the same tmp path within
    # one timestamp tick would otherwise see each other's results.
    sources.reset_caches()


@pytest.fixture(autouse=True)
def fake_dpg(monkeypatch: pytest.MonkeyPatch):
    """Swap Dear PyGui for the recording stand-in in tests/fakedpg.py.

    Autouse, and not negotiable: Dear PyGui is a native library that segfaults
    the interpreter when called without a graphics context, so a test that
    reaches a new `dpg.` call would take the whole suite down instead of
    failing. With the stand-in installed for every test, the worst case is an
    assertion error.
    """
    from opentine_gui import app
    from tests.fakedpg import FakeDPG

    fake = FakeDPG()
    monkeypatch.setattr(app, "dpg", fake)
    app._reset_theme_caches()
    yield fake
    app._reset_theme_caches()


@pytest.fixture
def gui_factory(fake_dpg):
    """Build a fully constructed console over a runs directory, without a loop."""
    from opentine_gui.app import OpentineGUI

    def make(runs_dir=None, *, scan: bool = True):
        gui = OpentineGUI(Path(runs_dir) if runs_dir is not None else None)
        gui._build_ui()
        gui._on_viewport_resize()
        if scan:
            gui._scan_now()
        return gui

    return make
