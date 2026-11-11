"""Assert every bundled .tine fixture loads and verifies under the pinned opentine.

Runs identically on Windows, macOS and Linux (CI invokes it on all three), so it
avoids shell heredocs and hardcoded separators.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from opentine.core import Run

from opentine_gui.sources import artifact_format

ROOT = Path(__file__).resolve().parent.parent

#: Written by demo/seed.py on purpose, to exercise the load-error panel.
CORRUPT_ON_PURPOSE = {"zz-corrupt.tine"}


def main() -> int:
    failures: list[str] = []
    checked = 0
    formats: dict[int | None, int] = {}

    for path in sorted((ROOT / ".tine_runs").glob("*.tine")):
        checked += 1
        if path.name in CORRUPT_ON_PURPOSE:
            # demo/seed.py writes this to exercise the load-error panel. Assert
            # it really is unloadable rather than skipping it.
            try:
                Run.load(path)
            except Exception:
                print(f"ok  {path.name}  (unloadable, as intended)")
            else:
                failures.append(f"{path.name}: expected to be corrupt, but it loaded")
            continue
        try:
            run = Run.load(path)
            result = Run.verify_integrity(path)
            if not result.ok:
                failures.append(f"{path.name}: integrity {result.reason}")
                continue
            # Run.format_version is the rebuilt record's version (2 for a v4
            # bundle), so the on-disk format is read off the file itself.
            on_disk = artifact_format(path)
            formats[on_disk] = formats.get(on_disk, 0) + 1
            print(f"ok  {path.name}  format v{on_disk} {run.status.value} "
                  f"{len(run.steps)} step(s)")
        except Exception as exc:
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")

    # The legacy fixture must keep exercising the v1 -> v2 migration path.
    legacy = ROOT / "tests" / "fixtures" / "legacy_v1.tine"
    if legacy.exists():
        checked += 1
        try:
            assert json.loads(legacy.read_text(encoding="utf-8"))["format_version"] == 1, (
                "legacy fixture is no longer format v1"
            )
            run = Run.load(legacy)
            assert run.metadata.get("migration"), "v1 load recorded no migration provenance"
            print(f"ok  {legacy.name}  v1 -> v{run.format_version} (migrated)")
        except Exception as exc:
            failures.append(f"{legacy.name}: {type(exc).__name__}: {exc}")

    # The demo must exercise both the current writer and the legacy reader.
    if checked and not formats.get(5):
        failures.append("no format v5 fixture - re-run demo/seed.py with opentine >= 0.42")
    if checked and not formats.get(4):
        failures.append("no format v4 fixture - demo/seed.py writes demo-v4.tine")
    if checked and not formats.get(2):
        failures.append("no format v2 fixture - demo/seed.py writes demo-legacy-v2.tine")

    if not checked:
        failures.append("no fixtures found — expected .tine_runs/*.tine")

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
