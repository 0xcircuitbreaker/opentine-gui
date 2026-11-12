"""opentine 0.26-0.34: format v4, tine-sig/3, and the metadata newer recorders add.

Since 0.26 every save writes a TINEZ1-compressed `tine-artifact/4` bundle, so
the console must read the scheme, the format and what a save would lose off a
document that is no longer a run dict, and it must say so when one of its own
writes converts an older file.
"""

from __future__ import annotations

import json
from pathlib import Path

from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.inspectors import (
    _capture_lines,
    _fork_edit_lines,
    _format_version_line,
    _run_detail_lines,
    _timestamp_line,
    _trust_lines,
    _upgrade_line,
)
from opentine_gui.sources import (
    _fields_a_save_would_drop,
    artifact_format,
    is_v4_document,
    read_artifact_document,
    writer_format,
)
from opentine_gui.trust import coverage_lines, signature_line, signature_scheme
from tests.artifacts import save_v2

KEY = b"0123456789abcdef0123456789abcdef"


def _run(run_id: str = "abc", status: RunStatus = RunStatus.completed) -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(Step(id="s2", parent_ids=["s1"], kind=StepKind.done, inputs={"text": "ok"}))
    return Run(id=run_id, graph=graph, status=status, user_prompt="hi")


# ---- reading the document -------------------------------------------------


def test_a_default_save_is_a_compressed_v4_bundle_the_console_can_read(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    assert not path.read_bytes().startswith(b"{"), "precondition: opentine compresses"
    document = read_artifact_document(path)
    # opentine 0.42+ writes v5 by default; its reader hands back the v4 document.
    assert is_v4_document(document)
    assert artifact_format(path) == writer_format() == 5


def test_a_legacy_file_still_reads_as_what_it_is(tmp_path: Path) -> None:
    path = save_v2(_run(), tmp_path / "abc.tine")
    assert not is_v4_document(read_artifact_document(path))
    assert artifact_format(path) == 2


def test_an_unreadable_file_has_no_format(tmp_path: Path) -> None:
    path = tmp_path / "junk.tine"
    path.write_bytes(b"TINEZ1\0\x01garbage")
    assert read_artifact_document(path) is None
    assert artifact_format(path) is None
    assert artifact_format(None) is None


def test_a_v4_file_that_round_trips_loses_nothing_on_save(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    assert _fields_a_save_would_drop(path) == ()


# ---- trust panel ----------------------------------------------------------


def test_a_v4_signature_is_tine_sig_3_and_covers_tags_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path, sign_key=KEY)
    assert signature_scheme(path) == "tine-sig/3"
    verdict = {"state": "verified", "algorithm": "hmac-sha256"}
    assert signature_line(verdict, scheme="tine-sig/3").endswith("(hmac-sha256, tine-sig/3)")
    lines = coverage_lines(verdict, scheme="tine-sig/3", v4=True)
    assert any("steps, tags and metadata are all covered" in line for line in lines)
    assert any(line.startswith("Integrity re-derives every object id") for line in lines)
    assert "binds them to a key" in lines[-1]


def test_an_unverified_v3_block_is_described_as_a_claim() -> None:
    lines = coverage_lines({"state": "no-key"}, scheme="tine-sig/3", v4=True)
    assert any(line.startswith("This block names tine-sig/3, which would") for line in lines)
    assert "unkeyed" in lines[-1]


def test_a_v2_document_cannot_claim_tine_sig_3(tmp_path: Path) -> None:
    # A v2 run dict names its scheme inside metadata.integrity; only a real v4
    # bundle may carry tine-sig/3, so a v2 block naming it is "unknown".
    data = {"metadata": {"integrity": {"signature": {"scheme": "tine-sig/3"}}}}
    assert signature_scheme(data) == ""
    forged = {
        "format_version": 4,
        "kind": "tine-artifact/4",
        "signatures": [{"scheme": "tine-sig/2\nSignature: verified"}],
    }
    assert signature_scheme(forged) == ""


def test_the_trust_panel_of_an_unsigned_v4_file_states_v4_scope(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    lines = _trust_lines(path)
    assert lines[:2] == ["Integrity: ok", "Signature: unsigned"]
    assert any(line.startswith("Integrity re-derives every object id") for line in lines)


def test_timestamp_tokens_beside_a_run_are_reported_not_verified(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    assert _timestamp_line(path) == ""
    sidecar = tmp_path / "abc.tine.timestamps.json"
    sidecar.write_text(
        json.dumps({"kind": "tine-timestamp-sidecar/1", "entries": [{}, {}]}), encoding="utf-8"
    )
    line = _timestamp_line(path)
    assert line.startswith("Timestamps: 2 RFC 3161 token(s)") and "not verified here" in line
    assert line in _trust_lines(path)
    sidecar.write_text('{"kind": "something else"}', encoding="utf-8")
    assert _timestamp_line(path) == "Timestamps: sidecar present but malformed"
    sidecar.write_bytes(b"\xff\xfe not json")
    assert _timestamp_line(path) == ""


# ---- run inspector --------------------------------------------------------


def test_a_v4_run_reports_format_v4_not_the_record_version(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    loaded = Run.load(path)
    assert loaded.format_version == 2, "precondition: opentine rebuilds a v2-shaped record"
    assert _format_version_line(loaded) == "Format: v4 (tine-artifact/4)"
    assert _format_version_line(Run.load(save_v2(_run(), tmp_path / "old.tine"))) == "Format: v2"


def test_an_upgraded_run_names_its_source_as_a_claim() -> None:
    run = _run()
    run._v4_origin = {
        "payload": {
            "upgraded_from": {
                "format": "tine/2",
                "tool": "opentine/0.34.0",
                "upgraded_at": "2026-11-03T00:00:00Z",
                "signature": {"state": "verified\nSignature: verified by evil"},
            }
        }
    }
    line = _upgrade_line(run)
    assert line.startswith("Upgraded: from tine/2, by opentine/0.34.0")
    assert "source signature claimed" in line
    assert "\n" not in line
    assert _upgrade_line(_run()) == ""


def test_capture_identity_originals_and_warnings_are_shown_as_claims() -> None:
    run = _run()
    run.metadata.update(
        {
            "capture": {
                "method": "transcript-import",
                "source_format": "claude-code",
                "agent_writable": True,
            },
            "agent_identity": {"harness": "claude-code", "model": "m\nIntegrity: ok"},
            "file_originals": {"sha256:a": {}, "sha256:b": {}},
            "warnings": ["usage.input stored as a decimal string", "second"],
        }
    )
    lines = _capture_lines(run)
    assert lines[0] == (
        "Capture: transcript-import (claude-code) - the agent could have edited this log"
    )
    assert lines[1].startswith("Agent (claimed): claude-code, m")
    assert "Rewind originals: 2 file(s) stored" in lines
    assert lines[-1] == "Recorder warning: usage.input stored as a decimal string (+1 more)"
    assert all("\n" not in line for line in lines)
    assert set(lines) <= set(_run_detail_lines(run))


def test_hostile_capture_metadata_cannot_crash_the_inspector() -> None:
    run = _run()
    run.metadata.update(
        {"capture": "x", "agent_identity": [], "file_originals": 3, "warnings": {"a": 1}}
    )
    assert _capture_lines(run) == []


def test_an_edited_fork_says_the_edited_step_never_ran() -> None:
    metadata = {
        "edit": {
            "step": "s2",
            "edited_step": "s2e",
            "changes": [{"path": "inputs.text"}, {"path": "model_info"}, "junk"],
            "executed": False,
        }
    }
    lines = _fork_edit_lines(metadata)
    assert lines[0] == "Edited fork: step s2e, changed inputs.text, model_info"
    assert "NOT executed" in lines[1]
    assert _fork_edit_lines({"edit": "nonsense"}) == []


# ---- writes ---------------------------------------------------------------


def test_pausing_a_legacy_file_says_it_will_be_converted_to_v4(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    path = save_v2(_run(status=RunStatus.running), tmp_path / "abc.tine")
    before = path.read_bytes()
    gui = gui_factory(tmp_path)
    gui._select_run("abc")

    gui._pause_selected()
    assert gui._modal_open() == "confirm_dialog"
    question = fake_dpg.value("confirm_text")
    assert "from format v2 to v5" in question and "before 0.40" in question
    assert path.read_bytes() == before, "the question comes before the write"

    gui._confirm_accept()
    assert Run.load(path).status == RunStatus.paused
    assert artifact_format(path) == 5


def test_pausing_a_v4_file_asks_nothing(tmp_path: Path, gui_factory) -> None:
    path = tmp_path / "abc.tine"
    _run(status=RunStatus.running).save(path)
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    gui._pause_selected()
    assert gui._modal_open() is None
    assert Run.load(path).status == RunStatus.paused


# ---- review findings ------------------------------------------------------


def test_a_small_file_that_expands_past_the_limit_is_skipped_unparsed(tmp_path: Path) -> None:
    import zlib

    from opentine._codec import MAGIC, ZLIB, _uvarint

    from opentine_gui import sources

    # A well-formed frame opentine itself would inflate: honest length, < 1000:1.
    raw = json.dumps({"pad": "x" * (sources.MAX_TINE_BYTES + 1024)}).encode()
    bomb = MAGIC + bytes([ZLIB]) + _uvarint(len(raw)) + zlib.compress(raw, 9)
    assert len(bomb) < sources.MAX_TINE_BYTES
    path = tmp_path / "bomb.tine"
    path.write_bytes(bomb)
    assert sources.inflated_size(path, len(bomb)) == len(raw)
    assert read_artifact_document(path) is None
    runs, errors, _, _ = sources.load_runs(tmp_path)
    assert runs == [] and any("expands to" in e for e in errors)


def test_the_save_check_is_cached_and_skips_the_rewrite_for_known_keys(
    tmp_path: Path, monkeypatch
) -> None:
    import opentine._artifact_v4 as v4

    path = tmp_path / "abc.tine"
    _run().save(path)
    calls: list[int] = []
    real = v4.write_v4
    monkeypatch.setattr(v4, "write_v4", lambda run: calls.append(1) or real(run))
    assert _fields_a_save_would_drop(path) == ()
    assert _fields_a_save_would_drop(path) == ()
    assert calls == [], "a file holding only known keys needs no re-encode"


def test_a_legacy_linear_file_is_named_in_the_conversion_warning(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    from opentine_gui.app import OpentineGUI

    path = tmp_path / "old.tine"
    path.write_text(json.dumps({"type": "Run", "run_id": "old", "steps": []}), encoding="utf-8")
    assert artifact_format(path) == 0
    gui = gui_factory(tmp_path)
    question = OpentineGUI._signature_at_risk(gui, path)
    assert "from the legacy 0.1 format to v5" in question


def test_a_v4_file_is_read_as_v4_and_its_save_announces_v5(
    tmp_path: Path, gui_factory, fake_dpg, monkeypatch
) -> None:
    from opentine._format_v4 import write_format

    path = tmp_path / "abc.tine"
    with write_format(4):
        _run(status=RunStatus.running).save(path)
    assert artifact_format(path) == 4
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    gui._pause_selected()
    assert "from format v4 to v5" in fake_dpg.value("confirm_text")
    gui._confirm_accept()
    assert artifact_format(path) == 5


def test_v5_is_recognised_by_its_top_level_not_by_text_inside_it(tmp_path: Path) -> None:
    from opentine._format_v4 import write_format

    path = tmp_path / "abc.tine"
    run = _run()
    run.metadata["note"] = {"kind": "tine-artifact/5", "format_version": 5}
    with write_format(4):
        run.save(path)
    assert artifact_format(path) == 4


def test_the_inspector_names_v5_when_the_file_is_v5(tmp_path: Path, gui_factory, fake_dpg) -> None:
    _run().save(tmp_path / "abc.tine")
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    assert "Format: v5 (tine-artifact/5)" in fake_dpg.value("detail_text")
