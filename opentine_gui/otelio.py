"""Moving a run between this console and the OpenTelemetry world.

Export was already here, written by hand: `json.dumps` over the document, no
`service.name` on the resource, and a plain `write_text` over whatever sat at
the destination. Each of those is a small lie about the run — an unnamed
producer in the collector, a document that does not match the one `tine export`
writes, and a previous export a crash can leave half replaced. Export here is
opentine's own document, opentine's own serializer, written atomically, and it
refuses a destination it was not told to replace.

Import is the direction the console never had. opentine 0.5.0 made it a CLI
verb, so a trace from another agent framework can become an ordinary run; this
is the console's half of it. Parsing produces a `Run` and writes nothing: the
recorder needs a v3 repository to record into, so it gets a throwaway one under
the system temp directory, and the user's runs directory sees a file only when
`save_imported` is called.

Every file read here is hostile input. It is bounded before it is read, a parse
failure comes back as a message rather than a traceback, and the warnings
opentine's importers record about what they could not understand travel out with
the run instead of being dropped on the way.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentine_gui.sources import SAFE_ID, _safe_run_path
from opentine_gui.text import _oneline, _truncate

try:
    # opentine 0.5.0's exporter, public from `opentine.trace` and the package
    # root ever since. Guarded even though the declared floor carries it: a
    # missing exporter must grey out one menu item, not stop the console.
    from opentine.trace import to_otel_genai_document as _to_otel_genai_document
except Exception:  # pragma: no cover - depends on the installed opentine
    _to_otel_genai_document = None

try:
    # The one JSON spelling every opentine machine surface writes: sorted keys,
    # two-space indent, and the strict `json_exact` coercion that refuses to
    # write a document it would have to damage (an over-deep or cyclic branch
    # becomes an exception, never a "[MAX_DEPTH]" marker in bytes that are
    # supposed to reproduce a run). Private, so losing it costs byte-identity
    # with `tine export`, not the export.
    from opentine._cli_json import serialize as _opentine_serialize
except Exception:  # pragma: no cover - depends on the installed opentine
    _opentine_serialize = None

try:
    from opentine.repo import Repo as _Repo
    from opentine.trace import Recorder as _Recorder
    from opentine.trace import framework_events as _framework_events
    from opentine.trace import jsonl_events as _jsonl_events
    from opentine.trace import otel_genai_events as _otel_genai_events
except Exception:  # pragma: no cover - depends on the installed opentine
    _Repo = _Recorder = None
    _framework_events = _jsonl_events = _otel_genai_events = None

try:
    #: Attribute opentine's importers append their own warnings to. Private, and
    #: worth reaching for anyway: the literal below is the contract either way,
    #: and reading the constant means a rename shows up as missing warnings in
    #: one place rather than as a key nobody matches.
    from opentine.trace._import_helpers import IMPORT_WARNINGS as _WARNINGS_ATTRIBUTE
except Exception:  # pragma: no cover - depends on the installed opentine
    _WARNINGS_ATTRIBUTE = "opentine.import_warnings"

try:
    #: Events one recorded run may hold. The recorder raises past its own cap,
    #: which would throw away a whole import at the last step, so imports are
    #: cut to it beforehand and the cut is reported.
    from opentine.trace.recorder import MAX_RECORDED_EVENTS as MAX_IMPORT_EVENTS
except Exception:  # pragma: no cover - depends on the installed opentine
    MAX_IMPORT_EVENTS = 3_000

#: `service.name` on the exported resource when the caller names none. The
#: console is a different producer from the `tine` CLI (which says "opentine"),
#: and a collector that cannot tell them apart cannot tell a desktop export from
#: a pipeline one.
DEFAULT_SERVICE_NAME = "opentine-gui"

#: Longest `service.name` this console will write. The value reaches a
#: collector, so it is bounded and flattened like any other free text.
MAX_SERVICE_NAME = 200

#: Largest trace file this console will read. opentine's own importers stop at
#: 256 MiB, which is a sane ceiling for a CLI that exits afterwards and a very
#: poor one for a desktop app that parses on the UI thread and then holds the
#: whole run in memory beside the runs it is already showing.
MAX_IMPORT_BYTES = 32 * 1024 * 1024

#: How much of a file `detect_format` reads. Enough for the head of a
#: pretty-printed OTLP document, small enough that pointing the console at a
#: multi-gigabyte log costs one page of I/O.
MAX_DETECT_BYTES = 128 * 1024

#: Distinct import warnings kept, and how long each is allowed to be. Both are
#: bounds on artifact-controlled text that ends up in a panel and in metadata.
MAX_IMPORT_WARNINGS = 50
MAX_WARNING_CHARS = 300

#: Where an imported run's provenance is recorded on the run itself. Metadata is
#: the only per-run place that survives `Run.save`: opentine's importers hang
#: their warnings on trace-event attributes, and `load_run` does not carry
#: attributes onto a `Step`, so a warning not copied here is gone the moment the
#: scratch repository is deleted.
IMPORT_METADATA_KEY = "import"

#: Ref the scratch recorder advances. It exists for the length of one import and
#: is deleted with the repository around it.
_IMPORT_REF = "heads/main"

#: Formats offered in the import menu, in menu order: this console's own
#: exports first, then opentine's native records, then the framework logs.
#: Mirrors `tine import --format`, whose importers do the actual reading.
IMPORT_FORMATS: tuple[str, ...] = (
    "otel-json",
    "otel-spans",
    "jsonl",
    "langchain",
    "llamaindex",
    "autogen",
    "crewai",
    "openai-agents",
)

_NOT_ID = re.compile(r"[^A-Za-z0-9]")


@dataclass(frozen=True)
class ExportResult:
    """What one written export turned out to be."""

    path: Path
    spans: int
    bytes: int


@dataclass(frozen=True)
class ImportedRun:
    """A foreign trace read into a run, before anything has been written."""

    #: An opentine Run, built in a scratch repository that is already gone.
    run: Any
    fmt: str
    events: int
    #: opentine's own importer warnings, plus a truncation notice when the
    #: source held more events than one run may.
    warnings: tuple[str, ...] = ()


def export_available() -> bool:
    """Whether the installed opentine can render a run as OTLP/JSON."""
    return _to_otel_genai_document is not None


def import_available() -> bool:
    """Whether the installed opentine can read a foreign trace into a run."""
    return None not in (
        _Repo,
        _Recorder,
        _framework_events,
        _jsonl_events,
        _otel_genai_events,
    )


def _service_name(name: str) -> str:
    """A single-line, bounded `service.name`, or this console's own."""
    cleaned = _truncate(_oneline(name), MAX_SERVICE_NAME)
    return cleaned or DEFAULT_SERVICE_NAME


def export_document(run: Any, *, service_name: str = "") -> dict:
    """Render a run as one complete OTLP/JSON export document.

    Read-only over the run: opentine builds the document from `run.steps` and
    touches no file, so exporting cannot disturb an artifact's integrity digest
    or its signature.
    """
    if _to_otel_genai_document is None:
        raise RuntimeError("this opentine has no OpenTelemetry exporter")
    try:
        return _to_otel_genai_document(run, service_name=_service_name(service_name))
    except (AttributeError, TypeError) as exc:
        # opentine validates a `.tine` as it loads it and coerces a repository
        # run, so a step holding a string where the exporter reads a mapping is
        # reachable only from a run this console assembled itself. A refusal
        # even so: export hangs off a menu item, and an AttributeError raised
        # inside a callback takes the window down with it.
        raise ValueError(f"this run cannot be rendered as OpenTelemetry spans: {exc}") from exc


def serialize_document(document: dict) -> str:
    """Spell the document the way `tine export` spells it, when opentine lets us.

    The fallback is deliberately not equivalent: opentine's serializer refuses a
    document whose content it cannot reproduce exactly, and `json.dumps` will
    happily write one. Sorted keys and the same indent keep two exports of the
    same run diffable across the two paths.
    """
    if _opentine_serialize is not None:
        return _opentine_serialize(document)
    return json.dumps(document, sort_keys=True, indent=2)


def _span_count(document: Any) -> int:
    """Spans in the exporter's single resource/scope envelope, 0 if unrecognised."""
    try:
        spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
    except Exception:
        return 0
    return len(spans) if isinstance(spans, list) else 0


def _inherit_mode(temporary: Path, target: Path) -> None:
    """Give the replacement the permissions of the file it is replacing.

    `mkstemp` creates at 0600 and the rename carries that onto the destination,
    so re-exporting over a document someone had made readable — by a collector
    running as another user, by a group — would quietly take that access away
    and look like a corrupt file at the far end. opentine's own atomic writer
    copies the mode across for the same reason. A destination that does not
    exist yet keeps 0600, which is the right default for a document that holds
    prompts and completions.
    """
    try:
        os.chmod(temporary, stat.S_IMODE(os.stat(target).st_mode))
    except OSError:
        # No previous file, or a filesystem with no modes worth copying.
        # Neither is a reason to abandon an export that is otherwise written.
        pass


def write_export(
    run: Any,
    path: str | Path,
    *,
    service_name: str = "",
    overwrite: bool = False,
) -> ExportResult:
    """Write a run's OTLP/JSON document to `path`, atomically and unprompted.

    Refusing an existing file unless `overwrite` mirrors opentine's
    `_require_output_slot`: a destination is replaced only when someone said so.
    The check is advisory against a racing writer, which is the same bargain the
    CLI makes; what it does buy is that a second export of a run cannot quietly
    destroy the first.

    The document is serialized in full before the destination directory is
    touched, then written to a temporary file in the destination's own directory
    and renamed over it. A crash therefore leaves either the previous export or
    the new one, never a truncated file that reads as a corrupt trace.
    """
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    document = export_document(run, service_name=service_name)
    # Trailing newline, like `tine export --output`: the document stays one
    # well-formed line-terminated file for anything reading it with `cat`.
    payload = (serialize_document(document) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temporary = Path(scratch)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            # The rename is what makes the write atomic, but only over bytes the
            # filesystem already has; without this a crash can order the rename
            # ahead of the content and leave a zero-length document where a good
            # export used to be.
            os.fsync(stream.fileno())
        _inherit_mode(temporary, target)
        os.replace(temporary, target)
    except BaseException:
        # Including a KeyboardInterrupt: an abandoned dot-file in the runs
        # directory would be picked up by nothing and cleaned by nobody.
        temporary.unlink(missing_ok=True)
        raise
    return ExportResult(path=target, spans=_span_count(document), bytes=len(payload))


def detect_format(path: str | Path) -> str:
    """Best guess at a trace file's format from its own content, "" if unknown.

    A guess only, and fail-open: an unreadable file, a truncated one, or a shape
    nothing here recognises returns "" so the caller asks rather than imports
    something as the wrong thing. Only the head of the file is read, so a
    detected format is a reason to offer a format, never a promise the whole
    file parses.
    """
    try:
        with Path(path).open("rb") as stream:
            prefix = stream.read(MAX_DETECT_BYTES)
    except Exception:
        return ""
    text = prefix.decode("utf-8", "replace")
    record = _first_record(text)
    if record is not None:
        guess = _format_from_record(record)
        if guess:
            return guess
    return _format_from_markers(text)


def _first_record(text: str) -> dict | None:
    """The first JSON object in the text: the document, or an array's or line's."""
    decoded: Any = None
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        # Expected for JSONL, and for any file longer than the prefix read.
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except (ValueError, RecursionError):
                decoded = None
            break
    if isinstance(decoded, list):
        decoded = next((item for item in decoded if isinstance(item, dict)), None)
    return decoded if isinstance(decoded, dict) else None


def _format_from_record(record: dict) -> str:
    """Name the format a single record looks like, most distinctive test first.

    The order matters where the shapes overlap. An OTLP span and an opentine
    JSONL record both carry a span and trace id, so camelCase (OTLP's own
    spelling) is tested before the snake_case pair; a LangChain record carries
    `run_id` the way a JSONL record does, so the JSONL test additionally
    requires the `kind` that only opentine writes.
    """
    if "resourceSpans" in record or "scopeSpans" in record:
        return "otel-json"
    if isinstance(record.get("spans"), list):
        return "otel-json"  # the {"spans": [...]} wrapper the otel-json importer accepts
    if any(key in record for key in ("spanId", "traceId", "startTimeUnixNano")):
        return "otel-spans"
    if "kind" in record and any(
        key in record for key in ("span_id", "trace_id", "inputs", "outputs")
    ):
        return "jsonl"
    if "parent_run_id" in record or ("run_id" in record and "name" in record):
        return "langchain"
    if "id_" in record or "event_type" in record:
        return "llamaindex"
    if "sender" in record:
        return "autogen"
    if "agent" in record:
        return "crewai"
    if "span_id" in record and "type" in record:
        return "openai-agents"
    return ""


def _format_from_markers(text: str) -> str:
    """Last resort for a file whose first record would not parse from the head.

    A pretty-printed OTLP document larger than the prefix read decodes as
    nothing at all, and its opening key is still the most reliable thing about
    it. Only keys distinctive enough to name one format on their own are here.
    """
    for marker, name in (
        ('"resourceSpans"', "otel-json"),
        ('"scopeSpans"', "otel-json"),
        ('"startTimeUnixNano"', "otel-spans"),
        ('"spanId"', "otel-spans"),
        ('"parent_run_id"', "langchain"),
        ('"event_type"', "llamaindex"),
    ):
        if marker in text:
            return name
    return ""


def _read_text(source: Path) -> str:
    """The whole file as text, refusing more than this console imports.

    Bounded at the read and not only by the earlier `stat`: the size of a growing
    file, a FIFO, or anything else whose `stat` disagrees with what it delivers
    is decided here, where the bytes actually arrive.
    """
    with source.open("rb") as stream:
        data = stream.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError(f"{source.name} is larger than {MAX_IMPORT_BYTES} bytes")
    return data.decode("utf-8", "replace")


def _decode(source: Path, text: str) -> Any:
    """Whole-document JSON, with a failure the user can act on."""
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{source.name} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        raise ValueError(f"{source.name} is nested too deeply to read") from exc


def _records(source: Path, text: str) -> list:
    """A JSON array, a single object, or one object per line.

    Whole-document parsing is tried first so a pretty-printed array spanning many
    lines is not mistaken for JSONL, exactly as `tine import` does it. The line
    number is ours: the decoder counts within the fragment it was handed and so
    reports "line 1" for every bad record in a file.
    """
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        pass
    else:
        return decoded if isinstance(decoded, list) else [decoded]
    records: list = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{source.name} line {number} is not valid JSON: {exc}") from exc
        except RecursionError as exc:
            raise ValueError(f"{source.name} line {number} is nested too deeply") from exc
    return records


def _read_events(source: Path, fmt: str) -> list:
    """Route the file to opentine's importer for `fmt`.

    The routing is spelled here rather than borrowed from `tine import`, whose
    reader is private and moved modules in 0.8.0. The importers it calls are the
    public ones, so what a foreign file becomes is opentine's decision, not this
    console's.
    """
    text = _read_text(source)
    if fmt == "jsonl":
        # Lines and not the path. The JSONL importer will happily open a path
        # itself, but then the read is bounded by opentine's 256 MiB ceiling
        # instead of this console's — and the earlier `stat` is no bound at all
        # for a file that grew since, or for a FIFO, whose size reads as zero.
        # It still skips a line it cannot decode rather than failing the file.
        return _jsonl_events(text.splitlines())
    if fmt == "otel-json":
        return _otel_genai_events(_decode(source, text))
    if fmt == "otel-spans":
        return _otel_genai_events(_records(source, text))
    return _framework_events(_records(source, text), fmt)


def _event_warnings(events: list) -> tuple[list[str], int]:
    """opentine's own import warnings, deduplicated, and how many did not fit.

    An importer records one warning per problem per span, so a file with a
    systematically wrong field produces the identical line thousands of times.
    They are collapsed here; the overflow is returned as a count rather than
    appended, because the caller has its own warnings to fit in the same budget
    and only it can say which line the reader most needs to see.
    """
    lines: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for event in events:
        attributes = getattr(event, "attributes", None)
        raw = attributes.get(_WARNINGS_ATTRIBUTE) if isinstance(attributes, dict) else None
        for item in raw if isinstance(raw, (list, tuple)) else ():
            if not isinstance(item, str):
                continue
            text = _truncate(_oneline(item), MAX_WARNING_CHARS)
            if not text or text in seen:
                continue
            seen.add(text)
            if len(lines) >= MAX_IMPORT_WARNINGS:
                dropped += 1
                continue
            lines.append(text)
    return lines, dropped


def _bounded_warnings(lines: list[str], dropped: int) -> list[str]:
    """At most `MAX_IMPORT_WARNINGS` lines, the last of them counting the rest.

    One cap, applied once, on the list that is both returned and written to the
    run's metadata — so what `import_warning_lines` renders is the whole list
    that was recorded, and the notice saying something was left out cannot
    itself be the thing that gets left out.
    """
    overflow = dropped + max(0, len(lines) - MAX_IMPORT_WARNINGS)
    if not overflow:
        return list(lines)
    kept = lines[: max(0, MAX_IMPORT_WARNINGS - 1)]
    return [*kept, f"and {dropped + len(lines) - len(kept)} further warning(s) not listed"]


def _record_events(events: list) -> Any:
    """Record events into a throwaway repository and read the run back out.

    The recorder needs somewhere to put content-addressed objects, and an import
    the user has not agreed to keep must not put them in the runs directory. The
    scratch repository lives in the system temp directory and is removed on every
    path out of here, success or failure, so a refused import leaves nothing.

    Capture is off for the same reason `tine import` turns it off: the code and
    environment of an imported trace belong to the machine that produced it, not
    to the one reading the file.
    """
    with tempfile.TemporaryDirectory(prefix="tine-gui-import-") as scratch:
        repo = _Repo.init(Path(scratch) / "import")
        recorder = _Recorder.start(repo, ref=_IMPORT_REF, capture=False)
        recorder.import_events(events)
        # Read the run back before the repository around it is deleted.
        return repo.load_run(recorder.finalize())


def _derived_run_id(run: Any) -> str:
    """A filename-safe id for an imported run, derived from the recorded object.

    Deliberately not the trace's own id. That value comes out of the file being
    imported, and the id decides which artifact in the runs directory the save
    lands on; a trace naming itself after an existing run would aim an
    overwrite, and a user who confirmed "replace" would be confirming a
    destination the file chose. The object id has no such author.
    """
    tail = _NOT_ID.sub("", str(getattr(run, "id", "")).rsplit(":", 1)[-1])[:12]
    return f"imported-{tail or uuid.uuid4().hex[:12]}"


def import_file(path: str | Path, *, fmt: str = "", run_id: str = "") -> ImportedRun:
    """Read a foreign trace file into an opentine run, writing nothing.

    `fmt` names one of `IMPORT_FORMATS`; empty means guess with `detect_format`
    and refuse if the guess is empty, because importing a file as the wrong
    format produces a plausible, wrong run rather than an error.

    Every failure here is a `ValueError` carrying a sentence: an unknown format,
    an oversized file, unparseable JSON, a trace with no events in it, or a
    dependency cycle the recorder refuses. `OSError` from an unreadable file is
    left alone, since its own message already names the file and the reason.
    """
    if not import_available():
        raise RuntimeError("this opentine cannot import foreign traces")
    source = Path(path)
    # Size before format: a missing or unreadable file must say so, and
    # detect_format cannot — it answers "" for a file that is not there just as
    # it does for one it does not recognise.
    size = source.stat().st_size
    if size > MAX_IMPORT_BYTES:
        raise ValueError(
            f"{source.name} is {size} bytes; this console imports at most {MAX_IMPORT_BYTES}"
        )
    chosen = fmt or detect_format(source)
    if not chosen:
        raise ValueError(f"cannot tell what kind of trace {source.name} is; choose a format")
    if chosen not in IMPORT_FORMATS:
        raise ValueError(f"unknown trace format: {chosen!r}")
    if run_id and not SAFE_ID.fullmatch(run_id):
        # Refused before a byte is read, and again by _safe_run_path at save
        # time, which is the check that actually decides a path.
        raise ValueError(f"unsafe run id: {run_id!r}")
    events = _read_events(source, chosen)
    if not events:
        # Distinct from a parse failure: the file read fine and held nothing this
        # importer recognises, which almost always means the wrong format.
        raise ValueError(f"no trace events found in {source.name} as {chosen}")
    truncated = 0
    if len(events) > MAX_IMPORT_EVENTS:
        truncated = len(events) - MAX_IMPORT_EVENTS
        events = events[:MAX_IMPORT_EVENTS]
    harvested, dropped = _event_warnings(events)
    # The cut goes first, ahead of opentine's own lines. It is the one warning
    # here that says the run is not the whole trace, and a file holding a
    # warning per span would otherwise push it past the cap and out of sight.
    leading = (
        [
            f"source held {truncated + MAX_IMPORT_EVENTS} events; imported the first "
            f"{MAX_IMPORT_EVENTS}, which is all one run may hold"
        ]
        if truncated
        else []
    )
    warnings = _bounded_warnings([*leading, *harvested], dropped)
    run = _record_events(events)
    run.run_id = run_id or _derived_run_id(run)
    run.metadata[IMPORT_METADATA_KEY] = {
        "format": chosen,
        "events": len(events),
        # The file name and not the path: this metadata is saved into an artifact
        # that gets shared, and the directory it was imported from is the user's
        # business, not the run's.
        "source": _truncate(_oneline(source.name), 120),
        "warnings": list(warnings),
    }
    return ImportedRun(run=run, fmt=chosen, events=len(events), warnings=tuple(warnings))


def save_imported(
    imported: ImportedRun,
    runs_dir: str | Path,
    *,
    run_id: str = "",
    overwrite: bool = False,
) -> Path:
    """Write an imported run into `runs_dir` as `<id>.tine`.

    The id is validated by `_safe_run_path`, so neither a caller's typo nor a
    name derived from a foreign file can put the artifact outside the runs
    directory, and an existing file is refused unless `overwrite` says
    otherwise.

    The run is renamed to the id it is saved under. The console keys a directory
    listing by `run.id` and writes actions back to the file that id came from;
    an artifact whose recorded id disagreed with its file name would show up as
    a run whose file nobody can find.
    """
    run = getattr(imported, "run", None)
    if run is None:
        raise ValueError("nothing to save: no imported run")
    directory = Path(runs_dir)
    chosen = run_id or str(getattr(run, "id", ""))
    target = _safe_run_path(directory, chosen)
    if target.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    directory.mkdir(parents=True, exist_ok=True)
    run.run_id = chosen
    # Run.save writes through opentine's atomic writer and stamps the integrity
    # digest, so the artifact this produces verifies like any recorded run.
    run.save(target)
    return target


def import_warning_lines(run: Any) -> list[str]:
    """Warnings recorded when this run was imported, ready to render.

    Returns nothing for a run that was not imported, and nothing rather than
    raising for one whose metadata holds some other shape entirely: everything
    read here came out of an artifact and may be anything at all.
    """
    metadata = getattr(run, "metadata", None)
    record = metadata.get(IMPORT_METADATA_KEY) if isinstance(metadata, dict) else None
    raw = record.get("warnings") if isinstance(record, dict) else record
    if not isinstance(raw, (list, tuple)):
        return []
    lines: list[str] = []
    for item in raw[:MAX_IMPORT_WARNINGS]:
        # Strings only: an importer writes sentences, so anything else in here
        # was put there by hand, and "None" or "{}" on a warning line reads as a
        # console bug rather than as the artifact oddity it is.
        if not isinstance(item, str):
            continue
        text = _truncate(_oneline(item), MAX_WARNING_CHARS)
        if text:
            lines.append(text)
    return lines
