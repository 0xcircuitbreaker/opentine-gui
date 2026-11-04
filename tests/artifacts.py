"""Reading and hand-editing `.tine` files the way the tests need to.

Since opentine 0.26 the default writer produces TINEZ1-compressed format v4
bundles, so a test that tampers with an artifact can no longer `json.loads` the
raw bytes. Tests about the v2 record shape - which every older opentine wrote
and which stays readable forever - write one explicitly with `save_v2`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opentine._artifact_io import read_artifact_json
from opentine._format_v4 import write_format


def save_v2(run: Any, path: Path, **kwargs: Any) -> Path:
    """Save *run* as a format v2 artifact, as opentine < 0.26 wrote it."""
    with write_format(2):
        run.save(path, **kwargs)
    return path


def load_doc(path: Path) -> Any:
    """The parsed JSON document of any `.tine` file, compressed or not."""
    return read_artifact_json(path)


def dump_doc(path: Path, data: Any) -> None:
    """Write a hand-edited document back, uncompressed (opentine reads both)."""
    path.write_text(json.dumps(data), encoding="utf-8")
