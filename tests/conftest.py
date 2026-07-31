"""Test isolation from the developer's real machine.

OpentineGUI.__init__ loads preferences eagerly, and the display-scale detection
shells out to the host's X resources. Without these fixtures the suite reads (and
could write) the real ~/.config profile, and its assertions would depend on the
DPI of whichever monitor happens to be attached.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opentine_gui import app


@pytest.fixture(autouse=True)
def isolate_user_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    prefs: Path = tmp_path_factory.mktemp("prefs") / "preferences.json"
    monkeypatch.setenv("OPENTINE_GUI_PREFS", str(prefs))
    for var in ("XDG_CONFIG_HOME", "APPDATA", "OPENTINE_GUI_SCALE", "OPENTINE_GUI_FONT",
                "GDK_SCALE", "QT_SCALE_FACTOR"):
        monkeypatch.delenv(var, raising=False)
    # Never probe the host display: no subprocess, no host-dependent assertions.
    monkeypatch.setattr(app, "_xrdb_dpi", lambda: None)
    monkeypatch.setattr(app, "_xresources_dpi", lambda: None)
    monkeypatch.setattr(app, "_screen_size", lambda: None)
    monkeypatch.setattr(app, "_UI_SCALE", 1.0)
