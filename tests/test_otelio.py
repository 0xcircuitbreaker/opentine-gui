"""Export and import as the console does them, over real opentine artifacts.

The round trip is asserted rather than assumed: a run that leaves as OTLP/JSON
and comes back has to be the same graph. The rest of the file is about the two
things a desktop console can do that a CLI cannot — destroy a file the user did
not name, and hand a hostile trace to the machinery — so every refusal here is a
refusal the app depends on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import otelio


def _run(run_id: str = "demo-run") -> Run:
    graph = Graph()
    graph.add(
        Step(
            id="s1",
            parent_ids=[],
            kind=StepKind.model,
            inputs={"prompt": "plan the release"},
            outputs={"text": "planning"},
            model_info="claude-sonnet-4",
            cost=0.0125,
            usage={"input": 40, "output": 12},
            timestamp=1_700_000_000.0,
            duration=1.5,
        )
    )
    graph.add(
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.tool,
            inputs={"name": "run_ci"},
            outputs={"exit": 0},
            tool_info={"name": "run_ci"},
            timestamp=1_700_000_002.0,
        )
    )
    graph.add(
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.error,
            inputs={},
            outputs={},
            error={"message": "flaky test"},
            timestamp=1_700_000_003.0,
        )
    )
    return Run(
        id=run_id,
        graph=graph,
        status=RunStatus.completed,
        user_prompt="cut the release",
    )


def _spans(document: dict) -> list[dict]:
    return document["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _resource_attributes(document: dict) -> dict:
    resource = document["resourceSpans"][0]["resource"]["attributes"]
    return {item["key"]: item["value"] for item in resource}


def _span_file(tmp_path: Path, spans: list[dict], name: str = "trace.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(spans), encoding="utf-8")
    return path


def _usage_span(tokens: object) -> dict:
    """One OTel GenAI span whose input-token counter is whatever is passed."""
    return {
        "name": "chat",
        "traceId": "t1",
        "spanId": "a1",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000001000000000",
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": str(tokens)}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "9"}},
        ],
    }


def test_the_console_names_itself_as_the_producing_service() -> None:
    document = otelio.export_document(_run())
    assert _resource_attributes(document)["service.name"] == {
        "stringValue": otelio.DEFAULT_SERVICE_NAME
    }
    named = otelio.export_document(_run(), service_name="release-bot")
    assert _resource_attributes(named)["service.name"] == {"stringValue": "release-bot"}


def test_a_service_name_cannot_smuggle_control_characters_to_a_collector() -> None:
    document = otelio.export_document(_run(), service_name="ok\nservice.name=spoofed")
    name = _resource_attributes(document)["service.name"]["stringValue"]
    assert "\n" not in name
    assert len(name) <= otelio.MAX_SERVICE_NAME


def test_a_run_survives_the_round_trip_through_a_written_document(tmp_path: Path) -> None:
    result = otelio.write_export(_run(), tmp_path / "demo-run.otel.json")
    assert result.spans == 3
    assert result.bytes == result.path.stat().st_size

    imported = otelio.import_file(result.path, fmt="otel-json")
    steps = imported.run.steps
    assert imported.events == 3
    assert [step.kind.value for step in steps] == ["model", "tool", "error"]
    assert [step.model_info for step in steps] == ["claude-sonnet-4", "", ""]
    assert steps[0].cost == pytest.approx(0.0125)
    assert steps[0].usage == {"input": 40, "output": 12}
    assert steps[0].inputs == {"prompt": "plan the release"}
    assert steps[0].duration == pytest.approx(1.5)
    # Step ids are content addresses assigned by the recorder, so lineage is
    # asserted as shape: a chain of three, each step parented on the last.
    assert steps[0].parent_ids == []
    assert steps[1].parent_ids == [steps[0].id]
    assert steps[2].parent_ids == [steps[1].id]


def test_a_written_document_is_detected_as_what_it_is(tmp_path: Path) -> None:
    path = otelio.write_export(_run(), tmp_path / "demo-run.otel.json").path
    assert otelio.detect_format(path) == "otel-json"
    assert otelio.import_file(path).fmt == "otel-json"


def test_write_export_refuses_an_existing_file_until_told_otherwise(tmp_path: Path) -> None:
    target = tmp_path / "demo-run.otel.json"
    first = otelio.write_export(_run(), target)
    before = target.read_bytes()

    with pytest.raises(FileExistsError):
        otelio.write_export(_run("other-run"), target)
    assert target.read_bytes() == before

    second = otelio.write_export(_run("other-run"), target, overwrite=True)
    assert second.path == first.path
    assert target.read_bytes() != before
    assert _spans(json.loads(target.read_text()))[0]["traceId"] == "other-run"


def test_a_successful_export_leaves_only_the_document(tmp_path: Path) -> None:
    otelio.write_export(_run(), tmp_path / "demo-run.otel.json")
    assert [path.name for path in tmp_path.iterdir()] == ["demo-run.otel.json"]


def test_a_failed_rename_leaves_neither_a_temp_file_nor_a_damaged_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "demo-run.otel.json"
    otelio.write_export(_run(), target)
    before = target.read_bytes()

    def refuse(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        otelio.write_export(_run("other-run"), target, overwrite=True)
    monkeypatch.undo()

    # The previous export is intact and nothing half-written is left to be
    # picked up as a trace file by anything scanning the directory.
    assert target.read_bytes() == before
    assert [path.name for path in tmp_path.iterdir()] == ["demo-run.otel.json"]


def test_exporting_does_not_touch_the_artifact_it_read(tmp_path: Path) -> None:
    artifact = tmp_path / "demo-run.tine"
    _run().save(artifact)
    before, stamp = artifact.read_bytes(), artifact.stat().st_mtime_ns

    otelio.write_export(Run.load(artifact), tmp_path / "demo-run.otel.json")

    assert artifact.read_bytes() == before
    assert artifact.stat().st_mtime_ns == stamp
    assert Run.verify_integrity(artifact).ok


def test_an_imported_run_saves_as_a_verifiable_artifact(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    document = otelio.export_document(_run())
    source = tmp_path / "trace.json"
    source.write_text(json.dumps(document), encoding="utf-8")

    saved = otelio.save_imported(otelio.import_file(source), runs)

    assert saved.parent == runs.resolve()
    assert saved.name.endswith(".tine")
    assert Run.verify_integrity(saved).ok
    # The id in the artifact and the id in its file name are the same run.
    assert Run.load(saved).id == saved.stem


def test_the_saved_id_is_never_the_one_the_trace_asked_for(tmp_path: Path) -> None:
    """A trace names its own traceId, so it must not name the file it lands in."""
    span = _usage_span(11)
    span["traceId"] = "../../../etc/passwd"
    source = _span_file(tmp_path, [span])

    saved = otelio.save_imported(otelio.import_file(source, fmt="otel-spans"), tmp_path / "runs")

    assert saved.parent == (tmp_path / "runs").resolve()
    assert saved.stem.startswith("imported-")


def test_save_imported_refuses_an_unsafe_run_id(tmp_path: Path) -> None:
    imported = otelio.import_file(_span_file(tmp_path, [_usage_span(11)]), fmt="otel-spans")
    runs = tmp_path / "runs"
    # An empty run_id is not in this list: it means "keep the id the import
    # already derived", which is the default path and a safe one.
    for unsafe in ("../escape", "runs/nested", "..", ".hidden", "a" * 200):
        with pytest.raises(ValueError):
            otelio.save_imported(imported, runs, run_id=unsafe)
    assert not runs.exists()


def test_save_imported_refuses_to_clobber_a_run_that_is_already_there(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    existing = runs / "keeper.tine"
    _run("keeper").save(existing)
    before = existing.read_bytes()
    imported = otelio.import_file(_span_file(tmp_path, [_usage_span(11)]), fmt="otel-spans")

    with pytest.raises(FileExistsError):
        otelio.save_imported(imported, runs, run_id="keeper")
    assert existing.read_bytes() == before

    otelio.save_imported(imported, runs, run_id="keeper", overwrite=True)
    assert Run.load(existing).id == "keeper"
    assert len(Run.load(existing).steps) == 1


def test_a_truncated_document_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    whole = json.dumps(otelio.export_document(_run()))
    truncated = tmp_path / "half.json"
    truncated.write_text(whole[: len(whole) // 2], encoding="utf-8")

    with pytest.raises(ValueError) as refusal:
        otelio.import_file(truncated, fmt="otel-json")
    assert "half.json" in str(refusal.value)
    assert "not valid JSON" in str(refusal.value)


def test_garbage_is_refused_by_every_format_that_reads_json(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.json"
    garbage.write_bytes(b"\x00\x01 not json at all {{{")
    for fmt in ("otel-json", "otel-spans", "langchain"):
        with pytest.raises(ValueError):
            otelio.import_file(garbage, fmt=fmt)
    # The JSONL importer skips lines it cannot decode, so a garbage file is not a
    # parse failure there — it is a file that held no events, which is its own
    # refusal rather than an empty run written to disk.
    with pytest.raises(ValueError) as refusal:
        otelio.import_file(garbage, fmt="jsonl")
    assert "no trace events" in str(refusal.value)


def test_a_file_of_the_wrong_shape_is_refused_before_it_becomes_a_run(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        otelio.import_file(empty, fmt="otel-spans")
    with pytest.raises(ValueError):
        otelio.import_file(empty, fmt="parquet")
    # Nothing about a bare list of numbers names a format, so nothing is guessed.
    numbers = tmp_path / "numbers.json"
    numbers.write_text("[1, 2, 3]", encoding="utf-8")
    assert otelio.detect_format(numbers) == ""
    with pytest.raises(ValueError) as refusal:
        otelio.import_file(numbers)
    assert "choose a format" in str(refusal.value)


def test_a_file_that_is_not_there_says_so(tmp_path: Path) -> None:
    # Not "cannot tell what kind of trace this is": detect_format answers "" for
    # a missing file exactly as it does for an unrecognised one.
    with pytest.raises(FileNotFoundError):
        otelio.import_file(tmp_path / "gone.json")


def test_an_oversized_trace_is_refused_without_being_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _span_file(tmp_path, [_usage_span(11)] * 4)
    monkeypatch.setattr(otelio, "MAX_IMPORT_BYTES", 64)
    with pytest.raises(ValueError) as refusal:
        otelio.import_file(source, fmt="otel-spans")
    assert "at most 64" in str(refusal.value)
    # The JSONL path reads through opentine's own streaming importer, so the
    # ceiling has to hold there too or it holds only where it is convenient.
    lines = tmp_path / "events.jsonl"
    lines.write_text("\n".join(json.dumps({"kind": "model", "span_id": str(n)}) for n in range(9)))
    with pytest.raises(ValueError):
        otelio.import_file(lines, fmt="jsonl")


def test_opentine_import_warnings_reach_the_run_and_the_saved_artifact(tmp_path: Path) -> None:
    # A negative token count is not a usage dimension opentine will record; it
    # drops it and says so, and the console must not be the thing that swallows
    # that sentence.
    source = _span_file(tmp_path, [_usage_span(-5)])
    imported = otelio.import_file(source, fmt="otel-spans")

    assert imported.warnings
    assert any("usage" in warning for warning in imported.warnings)
    assert imported.run.steps[0].usage == {"output": 9}
    assert otelio.import_warning_lines(imported.run) == list(imported.warnings)

    saved = otelio.save_imported(imported, tmp_path / "runs")
    assert otelio.import_warning_lines(Run.load(saved)) == list(imported.warnings)


def test_a_repeated_warning_is_reported_once(tmp_path: Path) -> None:
    spans = []
    for index in range(5):
        span = _usage_span(-5)
        span["spanId"] = f"a{index}"
        spans.append(span)
    imported = otelio.import_file(_span_file(tmp_path, spans), fmt="otel-spans")
    assert imported.events == 5
    assert len(imported.warnings) == 1


def test_a_trace_longer_than_a_run_may_be_is_cut_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spans = []
    for index in range(4):
        span = _usage_span(3)
        span["spanId"] = f"a{index}"
        spans.append(span)
    monkeypatch.setattr(otelio, "MAX_IMPORT_EVENTS", 2)

    imported = otelio.import_file(_span_file(tmp_path, spans), fmt="otel-spans")

    assert imported.events == 2
    assert len(imported.run.steps) == 2
    assert any("4 events" in warning and "first 2" in warning for warning in imported.warnings)


def test_a_run_that_was_never_imported_has_nothing_to_warn_about() -> None:
    assert otelio.import_warning_lines(_run()) == []


def test_import_warning_lines_fails_open_on_anything_an_artifact_can_hold() -> None:
    run = _run()
    for hostile in (None, "boom", 7, {"warnings": "not a list"}, {"warnings": [None, {}]}):
        run.metadata[otelio.IMPORT_METADATA_KEY] = hostile
        assert otelio.import_warning_lines(run) == []
    run.metadata[otelio.IMPORT_METADATA_KEY] = {"warnings": ["line one\nforged: ok", "x" * 900]}
    lines = otelio.import_warning_lines(run)
    assert lines[0] == "line one forged: ok"
    assert len(lines[1]) <= otelio.MAX_WARNING_CHARS


def test_formats_are_guessed_from_the_file_and_never_invented(tmp_path: Path) -> None:
    jsonl = tmp_path / "run.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps({"kind": "model", "span_id": "a1", "trace_id": "t1", "timestamp": 1})
            for _ in range(1)
        ),
        encoding="utf-8",
    )
    langchain = tmp_path / "chain.json"
    langchain.write_text(
        json.dumps([{"run_id": "1", "parent_run_id": None, "name": "LLMChain"}]), encoding="utf-8"
    )
    spans = _span_file(tmp_path, [_usage_span(11)], name="spans.json")
    prose = tmp_path / "notes.txt"
    prose.write_text("this is not a trace", encoding="utf-8")

    assert otelio.detect_format(jsonl) == "jsonl"
    assert otelio.detect_format(langchain) == "langchain"
    assert otelio.detect_format(spans) == "otel-spans"
    assert otelio.detect_format(prose) == ""
    assert otelio.detect_format(tmp_path / "nothing-here.json") == ""
    assert otelio.detect_format(tmp_path) == ""


def test_a_document_too_large_to_parse_from_its_head_is_still_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = otelio.write_export(_run(), tmp_path / "demo-run.otel.json").path
    monkeypatch.setattr(otelio, "MAX_DETECT_BYTES", 64)
    assert otelio.detect_format(path) == "otel-json"


def test_every_offered_format_is_one_opentine_can_actually_read(tmp_path: Path) -> None:
    # Each format is routed to a real importer; an empty JSON array reaches all
    # of them, so a format this console offers that opentine dropped shows up
    # here as an unsupported-importer failure rather than in front of a user.
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    for fmt in otelio.IMPORT_FORMATS:
        with pytest.raises(ValueError) as refusal:
            otelio.import_file(empty, fmt=fmt)
        assert "no trace events" in str(refusal.value)


def test_the_serialized_document_is_the_document(tmp_path: Path) -> None:
    document = otelio.export_document(_run())
    text = otelio.serialize_document(document)
    assert json.loads(text) == document
    assert text == otelio.serialize_document(document)
    path = otelio.write_export(_run(), tmp_path / "demo-run.otel.json").path
    assert path.read_text(encoding="utf-8") == text + "\n"


def _noisy_jsonl(tmp_path: Path, records: int, dimensions: int) -> Path:
    """A JSONL trace whose every event trips `dimensions` distinct opentine warnings."""
    usage = {f"dimension_{index:03d}": -1 for index in range(dimensions)}
    path = tmp_path / "noisy.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"kind": "model", "span_id": f"s{index}", "trace_id": "t", "usage": usage})
            for index in range(records)
        ),
        encoding="utf-8",
    )
    return path


def test_the_notice_that_a_trace_was_cut_outranks_the_warnings_it_competes_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file noisy enough to fill the warning budget must not push out the cut."""
    monkeypatch.setattr(otelio, "MAX_IMPORT_EVENTS", 2)
    source = _noisy_jsonl(tmp_path, records=4, dimensions=otelio.MAX_IMPORT_WARNINGS + 10)

    imported = otelio.import_file(source, fmt="jsonl")

    assert "imported the first 2" in imported.warnings[0]
    assert "further warning" in imported.warnings[-1]
    # One cap, applied once: what is rendered is the whole list that was
    # recorded, so the line saying something was left out is not itself left out.
    assert len(imported.warnings) == otelio.MAX_IMPORT_WARNINGS
    assert otelio.import_warning_lines(imported.run) == list(imported.warnings)
    saved = otelio.save_imported(imported, tmp_path / "runs")
    assert otelio.import_warning_lines(Run.load(saved)) == list(imported.warnings)


def test_the_overflow_notice_accounts_for_every_warning_it_stands_in_for(
    tmp_path: Path,
) -> None:
    kinds = otelio.MAX_IMPORT_WARNINGS + 10
    imported = otelio.import_file(_noisy_jsonl(tmp_path, 1, kinds), fmt="jsonl")
    listed = len(imported.warnings) - 1
    assert f"and {kinds - listed} further" in imported.warnings[-1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_re_export_keeps_the_permissions_the_export_it_replaces_had(tmp_path: Path) -> None:
    """The rename carries the temp file's mode, so it has to be corrected first."""
    target = tmp_path / "demo-run.otel.json"
    otelio.write_export(_run(), target)
    # A fresh export is the temp file's own 0600: a document holding prompts and
    # completions is not world-readable because the umask happened to allow it.
    assert target.stat().st_mode & 0o777 == 0o600

    target.chmod(0o644)
    otelio.write_export(_run("other-run"), target, overwrite=True)
    # Re-exporting must not quietly revoke access a collector or a colleague had.
    assert target.stat().st_mode & 0o777 == 0o644


def test_the_import_ceiling_holds_against_a_file_that_grew_after_it_was_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `stat` before the read is a courtesy; the bound is at the read itself.

    A file whose size disagrees with what it delivers is not exotic — an
    appended-to log, a FIFO, whose size reads as zero — and every format has to
    be bounded by the same ceiling or the JSONL importer's own 256 MiB is the
    one that applies.
    """
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps({"kind": "model", "span_id": "s0", "trace_id": "t"}) + "\n")
    monkeypatch.setattr(otelio, "MAX_IMPORT_BYTES", 512)

    def grow_then_guess(path: object) -> str:
        with open(path, "a", encoding="utf-8") as handle:
            for index in range(1, 60):
                handle.write(
                    json.dumps({"kind": "model", "span_id": f"s{index}", "trace_id": "t"}) + "\n"
                )
        return "jsonl"

    monkeypatch.setattr(otelio, "detect_format", grow_then_guess)

    with pytest.raises(ValueError) as refusal:
        otelio.import_file(source)
    assert "larger than 512" in str(refusal.value)


def test_a_run_the_exporter_cannot_read_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    """Export hangs off a menu item, and a callback that raises takes the window."""
    graph = Graph()
    graph.add(
        Step(
            id="s1",
            parent_ids=[],
            kind=StepKind.tool,
            inputs={},
            outputs={},
            # A shape `Run.load` refuses and `repo.load_run` coerces, so it can
            # only arrive from a run the console assembled itself.
            tool_info="not-a-mapping",
            timestamp=1_700_000_000.0,
        )
    )
    broken = Run(id="broken", graph=graph, status=RunStatus.completed)

    with pytest.raises(ValueError):
        otelio.export_document(broken)
    with pytest.raises(ValueError):
        otelio.write_export(broken, tmp_path / "broken.otel.json")
    assert list(tmp_path.iterdir()) == []


def test_the_two_directions_are_gated_on_the_opentine_that_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The menu asks these two questions, so they must answer for the real thing."""
    assert otelio.export_available() and otelio.import_available()

    monkeypatch.setattr(otelio, "_to_otel_genai_document", None)
    monkeypatch.setattr(otelio, "_Repo", None)
    assert not otelio.export_available()
    assert not otelio.import_available()
    with pytest.raises(RuntimeError):
        otelio.export_document(_run())
    with pytest.raises(RuntimeError):
        otelio.write_export(_run(), tmp_path / "demo-run.otel.json")
    with pytest.raises(RuntimeError):
        otelio.import_file(_span_file(tmp_path, [_usage_span(11)]), fmt="otel-spans")
    # A refused direction writes nothing, including the destination it was given.
    assert [path.name for path in tmp_path.iterdir()] == ["trace.json"]


def test_the_serializer_fallback_still_writes_the_document_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing opentine's private serializer costs byte-identity, not the export."""
    monkeypatch.setattr(otelio, "_opentine_serialize", None)
    document = otelio.export_document(_run())
    assert json.loads(otelio.serialize_document(document)) == document

    result = otelio.write_export(_run(), tmp_path / "demo-run.otel.json")
    assert result.spans == 3
    assert result.bytes == result.path.stat().st_size
    assert json.loads(result.path.read_text(encoding="utf-8")) == document
