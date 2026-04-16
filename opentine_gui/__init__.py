"""opentine-gui — Desktop GUI for opentine agent runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from opentine_gui.app import DEFAULT_RUNS_DIR, run_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="tine-gui", description="opentine desktop GUI")
    parser.add_argument(
        "runs_dir",
        nargs="?",
        default=str(DEFAULT_RUNS_DIR),
        help="Directory containing *.tine run files (default: ./.tine_runs)",
    )
    args = parser.parse_args()
    run_app(Path(args.runs_dir))


if __name__ == "__main__":
    main()
