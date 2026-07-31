"""opentine-gui — Desktop GUI for opentine agent runs."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

try:
    __version__ = version("opentine-gui")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__", "main", "run_app"]


def __getattr__(name: str) -> Any:
    """Import the app lazily.

    Dear PyGui links against libX11, so importing it at module scope makes
    `import opentine_gui` — and therefore `tine-gui --version` and `--help` —
    fail on a slim container or a headless box that has no graphics libraries.
    Deferring it keeps the metadata readable everywhere.
    """
    if name == "run_app":
        from opentine_gui.app import run_app

        return run_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tine-gui",
        description="opentine desktop GUI — browse, inspect, compare and fork agent runs",
    )
    parser.add_argument(
        "runs_dir",
        nargs="?",
        default=None,
        help=(
            "Directory containing *.tine run files "
            "(default: last used directory, then ./.tine_runs)"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    try:
        from opentine_gui.app import run_app
    except ImportError as exc:  # pragma: no cover - depends on the host's libraries
        raise SystemExit(
            f"tine-gui could not load its graphics libraries ({exc}).\n"
            "This is a desktop application and needs a display plus the system "
            "libraries Dear PyGui links against.\n"
            "On Debian/Ubuntu: sudo apt install libx11-6"
        ) from exc
    run_app(Path(args.runs_dir) if args.runs_dir else None)


if __name__ == "__main__":
    main()
