"""opentine-gui — Desktop GUI for opentine agent runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from opentine_gui.app import run_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="tine-gui", description="opentine desktop GUI")
    parser.add_argument(
        "runs_dir",
        nargs="?",
        default=None,
        help=(
            "Directory containing *.tine run files "
            "(default: last used directory, then ./.tine_runs)"
        ),
    )
    args = parser.parse_args()
    run_app(Path(args.runs_dir) if args.runs_dir else None)


if __name__ == "__main__":
    main()
