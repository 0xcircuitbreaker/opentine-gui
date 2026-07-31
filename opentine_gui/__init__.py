"""opentine-gui — Desktop GUI for opentine agent runs."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from opentine_gui.app import run_app

try:
    __version__ = version("opentine-gui")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__", "main", "run_app"]


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
    run_app(Path(args.runs_dir) if args.runs_dir else None)


if __name__ == "__main__":
    main()
