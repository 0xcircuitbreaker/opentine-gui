"""What the console's action buttons and menu items actually do.

Every case here drives a real callback against the Dear PyGui stand-in, so it
covers the whole path a click takes: the guard, the confirmation modal, the
write, and what the widgets are told afterwards. Nothing is stubbed except the
graphics library itself.

Two rules run through the file. A write lands in the file its row was loaded
from, never in a path derived from something an artifact said; and a source this
console may not write to — a v3 repository — refuses every write action outright
rather than half-performing one.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind
from opentine.repo import Repo

from opentine_gui import app, otelio
from opentine_gui.app import OpentineGUI
from opentine_gui.sources import _export_path

#: opentine refuses an HMAC key shorter than 16 bytes, so this is the shortest
#: key that can produce a signed artifact at all.
SIGNING_KEY = b"0123456789abcdef0123"


def _make_run(run_id: str, status: RunStatus = RunStatus.running) -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.done,
            inputs={},
            outputs={"answer": "42"},
            timestamp=0.1,
            duration=0.1,
        )
    )
    return Run(
        id=run_id,
        graph=graph,
        status=status,
        model_info="m",
        user_prompt="hi",
    )


def _seed(directory: Path, run_id: str = "abc", status: RunStatus = RunStatus.running) -> Path:
    """One artifact on disk, written the way opentine writes one."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.tine"
    _make_run(run_id, status).save(path)
    return path


def _console(gui_factory, directory: Path, run_id: str = "abc", *, step: int | None = None):
    """A built, scanned console with a run — and optionally a step — selected."""
    gui = gui_factory(directory)
    gui._select_run(run_id)
    if step is not None:
        gui._selected_step = gui._selected_run.steps[step]
        gui._update_action_state()
    return gui


def _repository(root: Path, run_id: str = "abc") -> Path:
    """A v3 repository holding one run: the source shape that is read-only."""
    Repo.init(root).put_run(_make_run(run_id), ref="heads/main")
    return root


def _notes(gui) -> list[str]:
    """Everything the console has said in the message log."""
    return [message.text for message in gui._messages]


def _said(gui, needle: str) -> bool:
    return any(needle in text for text in _notes(gui))


def _table_ids(fake) -> list[str]:
    """The run id shown in each run-table row, top to bottom."""
    return [fake.labels(row)[0] for row in fake.children_of("run_table")]


def _tree(root: Path) -> list[tuple[str, str]]:
    """Every path under root with a digest of its bytes.

    Compared before and after an action, this is what "wrote nothing" means: a
    size alone would miss a same-length rewrite of a ref or an object.
    """
    return sorted(
        (
            str(path.relative_to(root)),
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>",
        )
        for path in root.rglob("*")
    )


# ------------------------------------------------------------- pause / resume


def test_pause_writes_the_paused_status_into_the_file_it_came_from(tmp_path: Path, gui_factory):
    path = _seed(tmp_path)
    gui = _console(gui_factory, tmp_path)
    gui._pause_selected()
    assert Run.load(path).status == RunStatus.paused
    assert Run.verify_integrity(path).ok, "the re-save stamps a fresh digest"
    assert _said(gui, "Paused abc")


def test_pause_says_so_when_the_run_is_not_running(tmp_path: Path, gui_factory, fake_dpg):
    path = _seed(tmp_path, status=RunStatus.completed)
    before = path.read_bytes()
    gui = _console(gui_factory, tmp_path)
    gui._pause_selected()
    assert fake_dpg.value("status_bar") == "Select a running run to pause"
    assert path.read_bytes() == before


def test_resume_writes_the_running_status_and_keeps_the_fresh_run_selected(
    tmp_path: Path, gui_factory
):
    path = _seed(tmp_path, status=RunStatus.paused)
    gui = _console(gui_factory, tmp_path)
    gui._resume_selected()
    assert Run.load(path).status == RunStatus.running
    assert gui._selected_run is not None
    assert gui._selected_run.status == RunStatus.running


def test_pause_and_resume_write_to_the_source_file_not_an_id_named_one(
    tmp_path: Path, gui_factory
):
    # A run whose filename differs from its id (renamed, or shared by hand) must
    # be paused in place, not duplicated as <id>.tine.
    source = tmp_path / "descriptive-name.tine"
    _make_run("abc").save(source)
    gui = _console(gui_factory, tmp_path)
    gui._pause_selected()
    assert not (tmp_path / "abc.tine").exists()
    assert Run.load(source).status == RunStatus.paused

    gui._refresh()
    gui._resume_selected()
    assert not (tmp_path / "abc.tine").exists()
    assert Run.load(source).status == RunStatus.running


def test_pause_reloads_disk_state_instead_of_the_cached_snapshot(tmp_path: Path, gui_factory):
    # The console's cached Run can be a scan interval old; pausing from it would
    # truncate steps a still-running agent wrote in the meantime.
    path = _seed(tmp_path)
    gui = _console(gui_factory, tmp_path)
    fresh = _make_run("abc")
    fresh.graph.add(
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.think,
            inputs={"text": "the agent wrote this after the console's last scan"},
        )
    )
    fresh.save(path)

    gui._pause_selected()
    on_disk = Run.load(path)
    assert on_disk.status == RunStatus.paused
    assert [step.id for step in on_disk.steps] == ["s1", "s2", "s3"]


def test_resume_refuses_when_the_disk_status_moved_past_paused(tmp_path: Path, gui_factory):
    # The console still says paused, but the agent completed the run on disk
    # since the last scan; resume must not rewrite a terminal artifact.
    path = _seed(tmp_path, status=RunStatus.paused)
    gui = _console(gui_factory, tmp_path)
    _make_run("abc", RunStatus.completed).save(path)

    gui._resume_selected()
    assert Run.load(path).status == RunStatus.completed
    assert _said(gui, "no longer paused")


def test_an_artifact_id_cannot_steer_a_pause_out_of_the_runs_directory(
    tmp_path: Path, gui_factory
):
    # Writes go to the file the row was loaded from, never to a path built from
    # the id inside it, so an id like "../evil" has nowhere to point.
    runs = tmp_path / "runs"
    hostile = _make_run("abc")
    hostile.run_id = "../evil"  # Run.id is a read-only property over run_id
    runs.mkdir()
    hostile.save(runs / "evil.tine")

    gui = _console(gui_factory, runs, "../evil")
    gui._pause_selected()
    assert Run.load(runs / "evil.tine").status == RunStatus.paused
    assert sorted(path.name for path in tmp_path.iterdir()) == ["runs"]


# ---------------------------------------------------- the confirmation dialog


def test_pausing_a_draft_checkpoint_asks_before_dropping_the_marker(
    tmp_path: Path, gui_factory, fake_dpg
):
    # Run.save recomputes the integrity block from scratch, which clears the
    # draft marker an autosave checkpoint carries. The console cannot put it
    # back, so it asks first.
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path, draft=True)
    before = path.read_bytes()
    gui = _console(gui_factory, tmp_path)

    gui._pause_selected()
    assert gui._modal_open() == "confirm_dialog"
    assert "draft/autosave marker" in fake_dpg.value("confirm_text")
    assert path.read_bytes() == before, "the question comes before the write"

    gui._confirm_accept()
    assert Run.load(path).status == RunStatus.paused
    assert not Run.verify_integrity(path).draft
    assert gui._modal_open() is None


def test_resuming_a_signed_artifact_asks_before_dropping_the_signature(
    tmp_path: Path, gui_factory, fake_dpg
):
    # opentine refuses to sign a non-terminal run, so a signed *paused* artifact
    # can only have come from another producer or a hand edit. Either way this
    # console holds no signing key, so re-saving it destroys the signature. The
    # edit below also breaks the digest, which the console lists as a load
    # problem; what the question is about is the signature.
    path = tmp_path / "abc.tine"
    _make_run("abc", RunStatus.completed).save(
        path, sign_key=SIGNING_KEY, key_id="k", signer="s"
    )
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["status"] = "paused"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    before = path.read_bytes()

    gui = _console(gui_factory, tmp_path)
    gui._resume_selected()
    assert gui._modal_open() == "confirm_dialog"
    assert "its signature" in fake_dpg.value("confirm_text")
    assert path.read_bytes() == before

    gui._confirm_accept()
    assert Run.load(path).status == RunStatus.running
    assert Run.verify_signature(path).state == "unsigned", "the warned-of loss really happens"


def test_cancelling_the_confirmation_disarms_the_pending_write(tmp_path: Path, gui_factory):
    # Cancel and Esc must drop the pending action along with the dialog: a write
    # left armed would fire on whatever the user confirms next.
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path, draft=True)
    before = path.read_bytes()
    gui = _console(gui_factory, tmp_path)

    gui._pause_selected()
    gui._confirm_cancel()  # the Cancel button
    assert gui._modal_open() is None
    assert gui._confirm_action is None
    assert path.read_bytes() == before

    gui._pause_selected()
    gui._on_escape()  # Esc over the same dialog
    assert gui._modal_open() is None
    assert path.read_bytes() == before

    gui._confirm_accept()  # a Continue that reaches a dialog nobody armed
    assert gui._confirm_action is None
    assert path.read_bytes() == before


def test_a_clean_artifact_is_paused_without_a_question(tmp_path: Path, gui_factory):
    path = _seed(tmp_path)
    gui = _console(gui_factory, tmp_path)
    assert gui._signature_at_risk(path) == ""
    gui._pause_selected()
    assert gui._modal_open() is None, "an unsigned, undrafted save loses nothing"
    assert Run.load(path).status == RunStatus.paused


def test_the_question_names_the_signature_a_save_would_drop(tmp_path: Path, gui_factory):
    signed = tmp_path / "signed.tine"
    _make_run("signed", RunStatus.completed).save(
        signed, sign_key=SIGNING_KEY, key_id="k", signer="s"
    )
    gui = gui_factory(tmp_path)
    question = gui._signature_at_risk(signed)
    assert "its signature" in question
    assert question.endswith("Continue?")
    assert gui._signature_at_risk(tmp_path / "gone.tine") == "", "a missing file loses nothing"


# ---------------------------------------------------------------------- forks


def test_fork_writes_a_new_artifact_and_leaves_the_original(tmp_path: Path, gui_factory):
    path = _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path, step=0)
    gui._fork_selected()

    names = sorted(p.name for p in tmp_path.glob("*.tine"))
    assert len(names) == 2 and "abc.tine" in names
    fork_path = next(p for p in tmp_path.glob("*.tine") if p.name != "abc.tine")
    forked = Run.load(fork_path)
    assert forked.metadata["forked_from"] == "abc"
    assert forked.metadata["fork_point"] == "s1"
    assert [step.id for step in forked.steps] == ["s1"]
    assert Run.verify_integrity(fork_path).ok
    assert [step.id for step in Run.load(path).steps] == ["s1", "s2"], "original untouched"

    # The console moves to the fork on the rescan that lists it, not inline —
    # see test_the_new_fork_becomes_the_selected_run for why.
    gui._refresh()
    assert gui._selected_run is not None and gui._selected_run.id == forked.id


def test_fork_needs_a_step_to_fork_from(tmp_path: Path, gui_factory, fake_dpg):
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path)
    gui._fork_selected()
    assert fake_dpg.value("status_bar") == "Select a step to fork from"
    assert [p.name for p in tmp_path.glob("*.tine")] == ["abc.tine"]


def test_the_fork_dialog_records_the_branch_and_the_reason(
    tmp_path: Path, gui_factory, fake_dpg
):
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path, step=0)
    gui._open_fork_dialog()
    assert "Fork abc at step s1" in fake_dpg.value("fork_subject")
    assert fake_dpg.value("fork_slice") == "Keeps 1 of 2 step(s)"

    fake_dpg.set_value("fork_branch", "experiment")
    fake_dpg.set_value("fork_reason", "try a stronger model")
    gui._confirm_fork()

    # Read back the artifact the dialog wrote. The console's selection does not
    # move to it until the next scan, and what the branch and reason have to
    # survive in is the file.
    fork_path = next(p for p in tmp_path.glob("*.tine") if p.name != "abc.tine")
    forked = Run.load(fork_path)
    assert forked.metadata["fork"]["branch"] == "experiment"
    # Matches opentine's own MCP convention: recorded in metadata (so a
    # signature would cover it) and folded into the fork identity via intent.
    assert forked.metadata["fork_reason"] == "try a stronger model"
    plain = Run.load(tmp_path / "abc.tine").fork("s1")
    assert forked.metadata["fork"]["intent"] != plain.metadata["fork"]["intent"]
    assert fake_dpg.config("fork_dialog")["show"] is False


def test_a_reproducible_fork_yields_a_stable_id(tmp_path: Path, gui_factory):
    # Same source, same point, same branch and reason => the same id. Each fork
    # gets its own directory: forking reproducibly twice into ONE directory is
    # the overwrite case, covered below.
    source = tmp_path / "_source.tine"
    # Written once, then copied: a fork id is derived from the source's digest,
    # and Run() stamps created_at, so re-saving would change the source.
    _make_run("abc", RunStatus.completed).save(source)
    ids = []
    for name, reproducible in (("one", True), ("two", True), ("three", False)):
        target = tmp_path / name
        target.mkdir()
        shutil.copy2(source, target / "abc.tine")
        gui = _console(gui_factory, target, step=0)
        gui._do_fork(reproducible=reproducible)
        # The id the fork was actually saved under: _do_fork names the file
        # after it, and the selection has not moved to it yet.
        written = [p for p in target.glob("*.tine") if p.name != "abc.tine"]
        assert len(written) == 1, [p.name for p in target.glob("*.tine")]
        ids.append(str(Run.load(written[0]).id))

    assert ids[0] == ids[1]
    assert ids[2] != ids[0], "without the flag a fresh nonce keeps forks distinct"


def test_a_reproducible_fork_refuses_to_overwrite_the_earlier_fork(tmp_path: Path, gui_factory):
    # The failure mode opentine 0.4.0's nonce exists to prevent, and that
    # nonce="" deliberately opts out of: two reproducible forks derive one id
    # and one filename, so the second save would destroy the first.
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path, step=0)
    gui._do_fork(reproducible=True)
    fork_path = next(p for p in tmp_path.glob("*.tine") if p.name != "abc.tine")

    work = Run.load(fork_path)  # work happens inside the fork
    work.add_step(StepKind.think, {"text": "hours of debugging live here"})
    work.save(fork_path)

    gui._refresh()
    gui._select_run("abc")
    gui._selected_step = gui._selected_run.steps[0]
    gui._do_fork(reproducible=True)

    assert _said(gui, "already exists")
    assert len(list(tmp_path.glob("*.tine"))) == 2, "no new file, and none destroyed"
    assert len(Run.load(fork_path).steps) == 2, "the earlier fork's work survived"


def test_fork_reason_length_is_capped_like_opentine(tmp_path: Path, gui_factory):
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path, step=0)
    gui._do_fork(reason="x" * 5000)
    assert _said(gui, "at most 4096")
    assert [p.name for p in tmp_path.glob("*.tine")] == ["abc.tine"], "no file written"


def test_the_new_fork_becomes_the_selected_run(tmp_path: Path, gui_factory, fake_dpg):
    # Forking is how a user starts working on the child, so the child is what
    # the panels must describe once it exists — and the console's two ideas of
    # "the selected run" have to name it together. Pointing _selected_run at the
    # fork inline, before any row matched it, is what made them disagree: the
    # inspector described the fork while every action that reads the selected
    # row answered "Select a run first". So the move waits for the scan.
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path, step=0)
    gui._fork_selected()

    fork_path = next(p for p in tmp_path.glob("*.tine") if p.name != "abc.tine")
    forked = str(Run.load(fork_path).id)
    assert gui._selected_key == "abc", "the parent stays selected until a row lists the fork"

    gui._refresh()  # the rescan that lists the new artifact

    assert gui._selected_key == forked
    assert gui._selected_run is not None and str(gui._selected_run.id) == forked
    assert f"Run: {forked[:12]}..." in fake_dpg.value("detail_text")
    # Both ideas agree, so the actions that read the selected row work on it.
    assert gui._selected_entry() is not None
    assert fake_dpg.config("btn_copy_run")["enabled"] is True
    assert fake_dpg.config("btn_expand_run")["enabled"] is True


def test_sibling_forks_of_one_step_do_not_overwrite(tmp_path: Path, gui_factory):
    # The 0.4.0 behaviour the console depends on: before it, a second fork of
    # the same step reused the first's id and filename and destroyed it.
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path, step=0)
    gui._do_fork()
    gui._refresh()
    gui._select_run("abc")
    gui._selected_step = gui._selected_run.steps[0]
    gui._do_fork()
    assert len(list(tmp_path.glob("*.tine"))) == 3  # source plus two distinct forks


# ------------------------------------------------------ a read-only repository


def test_a_repository_refuses_pause_resume_and_fork_and_says_why(tmp_path: Path, gui_factory):
    # Writing into a v3 store is not a file write: it appends an object and
    # moves a branch. The console reads one and never writes it.
    root = _repository(tmp_path / "store")
    before = _tree(root)
    gui = _console(gui_factory, root, step=0)

    gui._pause_selected()
    gui._resume_selected()
    gui._fork_selected()

    refusals = [m for m in gui._messages if "v3 repository" in m.text]
    # One row, not three: the log collapses a line repeated back to back and
    # counts it instead, so a refusal that fires on every click cannot push the
    # rest of the log out of reach. The count is the assertion that all three
    # actions refused.
    assert len(refusals) == 1, _notes(gui)
    assert refusals[0].repeats == 3, _notes(gui)
    assert "tine repo-fork" in refusals[0].text, "it names the tool that can"
    assert _tree(root) == before, "a refused write must not touch the object store"


def test_a_repository_disables_every_write_action(tmp_path: Path, gui_factory, fake_dpg):
    root = _repository(tmp_path / "store")
    gui = _console(gui_factory, root, step=0)
    assert gui._selected_run.status == RunStatus.running, "pause would be legal on a file"

    write_actions = (
        "btn_pause", "btn_resume", "btn_fork", "menu_pause", "menu_fork", "menu_import",
    )
    enabled = {tag: fake_dpg.config(tag)["enabled"] for tag in write_actions}
    assert not any(enabled.values()), enabled
    assert fake_dpg.config("menu_refs")["enabled"] is True, "reading a repository is fine"
    assert fake_dpg.value("source_badge") == "v3 repository (read-only)"


def test_exporting_from_a_repository_leaves_the_object_store_alone(tmp_path: Path, gui_factory):
    # Read-only is about writes *into* the store: an export reads a run and
    # writes its own document beside the repository.
    root = _repository(tmp_path / "store")
    store = _tree(root / ".tine")
    gui = _console(gui_factory, root)
    gui._export_otel()
    assert (root / "abc.otel.json").exists(), _notes(gui)
    assert _tree(root / ".tine") == store


def test_importing_into_a_repository_is_refused(tmp_path: Path, gui_factory, fake_dpg):
    root = _repository(tmp_path / "store")
    gui = _console(gui_factory, root)
    gui._open_import_dialog()
    assert _said(gui, "read-only")
    assert fake_dpg.config("panel_dialog")["show"] is False


# --------------------------------------------------------- OpenTelemetry export


def test_otel_export_writes_a_valid_document_and_never_touches_the_artifact(
    tmp_path: Path, gui_factory
):
    path = _seed(tmp_path, status=RunStatus.completed)
    before = path.read_bytes()
    gui = _console(gui_factory, tmp_path)

    gui._export_otel()

    out = tmp_path / "abc.otel.json"
    assert out.exists(), _notes(gui)
    document = json.loads(out.read_text(encoding="utf-8"))
    assert "resourceSpans" in document
    assert otelio._span_count(document) == 2  # one span per step
    assert _said(gui, "span(s)")
    # Export is read-only: the artifact and its digest are untouched.
    assert path.read_bytes() == before
    assert Run.verify_integrity(path).ok


def test_otel_export_requires_a_selected_run(tmp_path: Path, gui_factory, fake_dpg):
    _seed(tmp_path)
    gui = gui_factory(tmp_path)
    gui._export_otel()
    assert fake_dpg.value("status_bar") == "Select a run to export"
    assert not list(tmp_path.glob("*.otel.json"))


def test_otel_export_degrades_on_an_older_opentine(tmp_path: Path, gui_factory, monkeypatch):
    # The dependency floor is 0.7.2, but the exporter arrived in 0.5.0; against
    # an older library the menu item must explain itself, not raise in a
    # callback. Patched where it is defined — app only re-exports the name.
    _seed(tmp_path)
    gui = _console(gui_factory, tmp_path)
    monkeypatch.setattr(otelio, "_to_otel_genai_document", None)
    gui._export_otel()
    assert _said(gui, "0.5.0")
    assert not list(tmp_path.glob("*.otel.json"))


def test_otel_export_refuses_to_overwrite_until_it_is_confirmed(
    tmp_path: Path, gui_factory, fake_dpg
):
    # A second export of one run must not quietly destroy the first document,
    # which may be the one a collector already picked up.
    _seed(tmp_path, status=RunStatus.completed)
    out = tmp_path / "abc.otel.json"
    out.write_text("someone else's document", encoding="utf-8")
    gui = _console(gui_factory, tmp_path)

    gui._export_otel()
    assert gui._modal_open() == "confirm_dialog"
    assert "already exists" in fake_dpg.value("confirm_text")
    assert out.read_text(encoding="utf-8") == "someone else's document"

    gui._confirm_accept()
    assert otelio._span_count(json.loads(out.read_text(encoding="utf-8"))) == 2


def test_an_artifact_id_cannot_steer_an_export_out_of_the_runs_directory(
    tmp_path: Path, gui_factory
):
    runs = tmp_path / "runs"
    hostile = _make_run("abc", RunStatus.completed)
    hostile.run_id = "../escape"
    runs.mkdir()
    hostile.save(runs / "escape.tine")

    gui = _console(gui_factory, runs, "../escape")
    gui._export_otel()
    assert _said(gui, "unsafe run id")
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.parametrize("hostile", ["../escape", "sub/dir", "", "has space"])
def test_the_export_path_refuses_an_unsafe_id(tmp_path: Path, hostile: str):
    assert _export_path(tmp_path, "abc").name == "abc.otel.json"
    with pytest.raises(ValueError):
        _export_path(tmp_path, hostile)


def test_span_count_tolerates_any_document_shape():
    for document in ("a string", None, {}, {"resourceSpans": "x"}, {"resourceSpans": [{}]}, 42):
        assert isinstance(otelio._span_count(document), int)


# ---------------------------------------------------------------------- import


def test_import_writes_a_new_artifact_the_console_can_then_open(
    tmp_path: Path, gui_factory, fake_dpg
):
    runs = tmp_path / "runs"
    runs.mkdir()
    trace = tmp_path / "trace.otel.json"
    otelio.write_export(_make_run("source", RunStatus.completed), trace)
    gui = gui_factory(runs)

    gui._open_import_dialog()
    fake_dpg.set_value("import_path", str(trace))
    fake_dpg.set_value("import_format", "auto")
    gui._do_import()

    written = sorted(runs.glob("*.tine"))
    assert len(written) == 1, _notes(gui)
    # Named after the recorded object, not after anything the trace said: an id
    # out of the file would let it aim the write at an existing artifact.
    assert written[0].name.startswith("imported-")
    assert Run.verify_integrity(written[0]).ok
    assert _said(gui, "Imported 2 event(s)")
    assert fake_dpg.config("panel_dialog")["show"] is False
    assert trace.exists(), "the file being imported is never modified"

    gui._refresh()
    assert [entry.key for entry in gui._entries] == [written[0].stem]


def test_a_failed_import_is_recorded_in_the_message_log(tmp_path: Path, gui_factory, fake_dpg):
    # The status line is overwritten by the next scan; the log is where an
    # error the user still has to act on stays put.
    gui = gui_factory(tmp_path)
    gui._open_import_dialog()
    fake_dpg.set_value("import_path", str(tmp_path / "nope.json"))
    gui._do_import()

    assert _said(gui, "Cannot import nope.json")
    logged = fake_dpg.texts("message_log")
    assert any("Cannot import nope.json" in line for line in logged)
    line = fake_dpg.children_of("message_log")[-1]
    assert fake_dpg.config(line)["color"] == app.LEVEL_COLORS["error"]

    gui._set_status("something else entirely")
    assert fake_dpg.texts("message_log") == logged, "the log does not scroll away"
    assert fake_dpg.value("status_bar") == "something else entirely"


def test_import_needs_a_path_to_read(tmp_path: Path, gui_factory, fake_dpg):
    gui = gui_factory(tmp_path)
    gui._open_import_dialog()
    gui._do_import()
    assert fake_dpg.value("status_bar") == "Type the path of a trace file to import"
    assert not list(tmp_path.glob("*.tine"))


# --------------------------------------------------------------------- panels


def test_the_pricing_panel_quotes_the_run_without_touching_it(
    tmp_path: Path, gui_factory, fake_dpg
):
    path = _seed(tmp_path, status=RunStatus.completed)
    before = path.read_bytes()
    gui = _console(gui_factory, tmp_path)

    gui._open_pricing()
    assert fake_dpg.config("panel_dialog")["label"] == "Price this run"
    assert fake_dpg.config("panel_dialog")["show"] is True
    # Nothing in this run was billed, so the subject reports the absence rather
    # than a $0.0000 the artifact never claimed, and the body says the quote is
    # computed here and never written back.
    subject = fake_dpg.value("panel_subject")
    assert subject.startswith("abc - ") and "$0.0000" not in subject
    assert "never written back to the artifact" in fake_dpg.value("panel_text")
    assert gui._quote is not None and gui._quote_key == "abc"
    assert path.read_bytes() == before


def test_pricing_as_of_a_date_is_remembered(tmp_path: Path, gui_factory, fake_dpg):
    # A catalog is a moving target, so the date the quote was priced at is part
    # of the answer and worth keeping between runs of the console.
    _seed(tmp_path, status=RunStatus.completed)
    gui = _console(gui_factory, tmp_path)
    gui._open_pricing()
    fake_dpg.set_value("pricing_date", "2026-01-01")
    gui._recompute_price()
    assert "2026-01-01" in fake_dpg.value("panel_text")
    assert gui._preferences["pricing_as_of"] == "2026-01-01"


def test_pricing_needs_a_selected_run(tmp_path: Path, gui_factory, fake_dpg):
    _seed(tmp_path)
    gui = gui_factory(tmp_path)
    gui._open_pricing()
    assert fake_dpg.value("status_bar") == "Select a run to price"
    assert fake_dpg.config("panel_dialog")["show"] is False


def test_the_statistics_panel_summarises_the_visible_runs(
    tmp_path: Path, gui_factory, fake_dpg
):
    _seed(tmp_path, "alpha", RunStatus.completed)
    _seed(tmp_path, "beta", RunStatus.failed)
    gui = gui_factory(tmp_path)

    gui._open_stats()
    assert fake_dpg.config("panel_dialog")["label"] == "Statistics"
    assert "2 all run(s)" in fake_dpg.value("panel_subject")
    body = fake_dpg.value("panel_text")
    assert "By status" in body and "completed" in body and "failed" in body

    fake_dpg.set_value("stats_group", "model")
    gui._render_stats()
    assert "By model" in fake_dpg.value("panel_text")
    assert gui._preferences["stats_group_by"] == "model"


def test_statistics_follow_the_run_filter(tmp_path: Path, gui_factory, fake_dpg):
    # The panel summarises what the user is looking at, not the whole directory.
    _seed(tmp_path, "alpha", RunStatus.completed)
    _seed(tmp_path, "beta", RunStatus.failed)
    gui = gui_factory(tmp_path)
    gui._run_filter = "alpha"
    gui._open_stats()
    assert "1 filtered run(s)" in fake_dpg.value("panel_subject")


def test_statistics_of_nothing_says_so(tmp_path: Path, gui_factory, fake_dpg):
    gui = gui_factory(tmp_path)
    gui._open_stats()
    assert fake_dpg.value("status_bar") == "Nothing loaded to summarise"
    assert fake_dpg.config("panel_dialog")["show"] is False


def test_the_refs_panel_lists_every_ref_in_the_repository(
    tmp_path: Path, gui_factory, fake_dpg
):
    root = _repository(tmp_path / "store")
    gui = _console(gui_factory, root)
    gui._open_refs()
    assert fake_dpg.config("panel_dialog")["label"] == "Repository refs"
    assert "1 ref(s), 1 run(s)" in fake_dpg.value("panel_subject")
    body = fake_dpg.value("panel_text")
    assert "heads (1)" in body
    assert "heads/main" in body
    assert "run:" in body, "the object id, shortened the way tine renders it"


def test_refs_are_refused_for_a_directory_of_files(tmp_path: Path, gui_factory, fake_dpg):
    _seed(tmp_path)
    gui = _console(gui_factory, tmp_path)
    gui._open_refs()
    assert "v3 repository concept" in fake_dpg.value("status_bar")
    assert fake_dpg.config("panel_dialog")["show"] is False


# ------------------------------------------------------- view menu and the log


def test_auto_refresh_can_be_switched_off_and_back_on(tmp_path: Path, gui_factory, fake_dpg):
    # The loader keeps rescanning on its own thread; this menu item is the only
    # way to stop it walking a directory someone is actively writing into.
    gui = gui_factory(tmp_path)
    assert gui._loader.auto is True
    fake_dpg.set_value("menu_auto_refresh", False)
    gui._toggle_auto_refresh()
    assert gui._loader.auto is False
    assert _said(gui, "Auto-refresh off")

    fake_dpg.set_value("menu_auto_refresh", True)
    gui._toggle_auto_refresh()
    assert gui._loader.auto is True
    assert _said(gui, "Auto-refresh on")


def test_the_message_log_can_be_collapsed(tmp_path: Path, gui_factory, fake_dpg):
    gui = gui_factory(tmp_path)
    quiet = fake_dpg.config("panel_messages")["height"]

    # A strip with nothing in it stays one row: four reserved rows of empty
    # panel read as something that failed to load rather than as silence.
    gui._note("info", "something happened")
    opened = fake_dpg.config("panel_messages")["height"]
    assert opened > quiet
    assert fake_dpg.config("panel_runs")["height"] > fake_dpg.config("panel_runs")["height"] - 1

    fake_dpg.set_value("menu_messages", False)
    gui._toggle_messages()
    assert fake_dpg.config("message_log")["show"] is False
    assert fake_dpg.config("panel_messages")["height"] == quiet

    fake_dpg.set_value("menu_messages", True)
    gui._toggle_messages()
    assert fake_dpg.config("message_log")["show"] is True
    assert fake_dpg.config("panel_messages")["height"] == opened


# ---------------------------------------------------------------- keys and sort


def _three_runs(directory: Path) -> None:
    for run_id in ("aaa", "bbb", "ccc"):
        _seed(directory, run_id)


def test_arrow_keys_defer_the_selection_to_the_main_loop(tmp_path: Path, gui_factory):
    # Key handlers must not rebuild the DAG inline: holding a key down would
    # delete and recreate hundreds of node items several times per frame.
    _three_runs(tmp_path)
    gui = gui_factory(tmp_path)
    gui._sort_column, gui._sort_ascending = "id", True
    gui._render_run_table()
    gui._select_run("aaa")

    gui._move_selection(1)
    assert gui._pending_select == "bbb"
    assert gui._selected_key == "aaa", "nothing has been rebuilt yet"

    gui._apply_pending_input()  # what run()'s loop does each frame
    assert gui._selected_key == "bbb"
    assert gui._pending_select is None


def test_arrow_keys_clamp_at_both_ends(tmp_path: Path, gui_factory):
    _three_runs(tmp_path)
    gui = gui_factory(tmp_path)
    gui._sort_column, gui._sort_ascending = "id", True
    gui._render_run_table()

    gui._select_run("aaa")
    gui._move_selection(-1)
    assert gui._pending_select == "aaa", "already at the top"

    gui._select_run("ccc")
    gui._move_selection(1)
    assert gui._pending_select == "ccc", "already at the bottom"


def test_arrow_keys_select_the_first_visible_run_when_nothing_is_selected(
    tmp_path: Path, gui_factory
):
    _three_runs(tmp_path)
    gui = gui_factory(tmp_path)
    gui._sort_column, gui._sort_ascending = "id", True
    gui._render_run_table()
    gui._move_selection(1)
    assert gui._pending_select == "aaa"


def test_arrow_keys_are_inert_while_typing_or_behind_a_modal(
    tmp_path: Path, gui_factory, fake_dpg
):
    _three_runs(tmp_path)
    gui = gui_factory(tmp_path)
    gui._select_run("aaa")

    fake_dpg.focus_item("run_filter")
    gui._move_selection(1)
    assert gui._pending_select is None, "arrow keys must reach a focused text field"

    fake_dpg.focused = set()
    fake_dpg.configure_item("fork_dialog", show=True)
    gui._move_selection(1)
    assert gui._pending_select is None, "arrow keys must not move the list behind a modal"


def test_ctrl_r_asks_the_loader_for_an_immediate_rescan(tmp_path: Path, gui_factory, fake_dpg):
    # Without the force flag the reload waits behind the loader's own "has
    # anything changed?" gate for up to AUTO_REFRESH_SECONDS.
    gui = gui_factory(tmp_path)
    gui._loader._force = False
    gui._loader._wake.clear()

    fake_dpg.keys.add(fake_dpg.mvKey_ModCtrl)
    gui._on_ctrl_r()

    assert gui._loader._force is True
    assert gui._loader._wake.is_set(), "the worker is woken, not left on its timer"
    assert fake_dpg.value("status_bar") == "Refreshing..."


def test_the_forced_rescan_flag_is_spent_by_the_scan_it_triggers(tmp_path: Path, gui_factory):
    # The other half of Ctrl+R's contract. If the flag stayed set, every later
    # wake would rescan too: the loader would re-walk the directory, re-parse
    # every artifact and re-hash every file forever, which is the cost its
    # "has anything changed?" gate exists to avoid. Nothing here sleeps — the
    # queue get returns the moment the worker finishes the scan, and the worker
    # clears the flag before it puts the snapshot, so the read below cannot race.
    _seed(tmp_path)
    gui = gui_factory(tmp_path)
    loader = gui._loader
    loader.interval = 3600.0  # only an explicit request can wake it inside a test
    loader.drain()
    loader.start()
    try:
        loader.request(force=True)
        assert loader.results.get(timeout=10) is not None, "the forced scan really ran"
        assert loader._force is False, "spent by that scan, not left armed for every wake"
    finally:
        loader.stop()


def test_ctrl_r_is_inert_without_the_modifier(tmp_path: Path, gui_factory):
    gui = gui_factory(tmp_path)
    gui._loader._force = False
    gui._on_ctrl_r()
    assert gui._loader._force is False, "plain 'r' must not trigger a reload"


def test_escape_closes_a_modal_before_clearing_a_filter(tmp_path: Path, gui_factory, fake_dpg):
    gui = gui_factory(tmp_path)
    gui._step_filter = "tool"
    fake_dpg.configure_item("diff_dialog", show=True)
    gui._on_escape()
    assert fake_dpg.config("diff_dialog")["show"] is False
    assert gui._step_filter == "tool", "the modal takes priority over the filter"


def test_escape_clears_the_step_filter_then_the_run_filter(tmp_path: Path, gui_factory, fake_dpg):
    _seed(tmp_path)
    gui = _console(gui_factory, tmp_path)
    gui._step_filter, gui._run_filter = "tool", "abc"
    fake_dpg.set_value("step_filter", "tool")
    fake_dpg.set_value("run_filter", "abc")

    gui._on_escape()
    assert gui._step_filter == "" and fake_dpg.value("step_filter") == ""
    assert gui._run_filter == "abc", "one Escape clears one filter"

    gui._on_escape()
    assert gui._run_filter == "" and fake_dpg.value("run_filter") == ""


def test_the_sort_callback_maps_a_column_id_to_a_sort_key_and_remembers_it(
    tmp_path: Path, gui_factory, fake_dpg
):
    _three_runs(tmp_path)
    # The column id is read before the first table render: the stand-in keeps
    # columns and rows in one slot, so rendering the table deletes the columns
    # that real Dear PyGui keeps in a slot of their own.
    gui = gui_factory(tmp_path, scan=False)
    gui._on_table_sort("run_table", [[fake_dpg.get_alias_id("col_id"), 1]])

    assert gui._sort_column == "id"
    assert gui._sort_ascending is True
    assert gui._preferences["sort_column"] == "id"
    assert gui._preferences["sort_ascending"] == "1"

    gui._scan_now()
    assert _table_ids(fake_dpg) == ["aaa", "bbb", "ccc"]


def test_the_sort_callback_reverses_the_table(tmp_path: Path, gui_factory, fake_dpg):
    _three_runs(tmp_path)
    gui = gui_factory(tmp_path, scan=False)
    gui._on_table_sort("run_table", [[fake_dpg.get_alias_id("col_id"), -1]])
    gui._scan_now()
    assert gui._sort_ascending is False
    assert _table_ids(fake_dpg) == ["ccc", "bbb", "aaa"]


def test_the_sort_callback_ignores_a_spec_dear_pygui_could_not_fill_in(
    tmp_path: Path, gui_factory
):
    # Dear PyGui passes None when a click clears the sort; the order stands.
    _three_runs(tmp_path)
    gui = gui_factory(tmp_path)
    gui._sort_column, gui._sort_ascending = "id", True
    gui._on_table_sort("run_table", None)
    assert (gui._sort_column, gui._sort_ascending) == ("id", True)


# ------------------------------------------------- selection, refresh, sources


def test_selecting_a_run_fills_the_inspectors_and_draws_the_graph(
    tmp_path: Path, gui_factory, fake_dpg
):
    _seed(tmp_path)
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    gui._selected_step = gui._selected_run.steps[0]

    gui._select_run("abc")

    assert gui._selected_key == "abc"
    assert gui._selected_step is None, "a new selection starts without a step"
    assert "Run: abc" in fake_dpg.value("detail_text")
    assert fake_dpg.value("step_text") == "Select a step in the DAG"
    assert len(fake_dpg.find("node")) == 2  # one node per step


def test_action_state_matches_run_status_and_step_selection(
    tmp_path: Path, gui_factory, fake_dpg
):
    _seed(tmp_path, "running", RunStatus.running)
    _seed(tmp_path, "paused", RunStatus.paused)
    gui = _console(gui_factory, tmp_path, "running")

    assert fake_dpg.config("btn_pause")["enabled"] is True
    assert fake_dpg.config("btn_resume")["enabled"] is False
    assert fake_dpg.config("btn_fork")["enabled"] is False, "fork needs a step"

    gui._select_run("paused")
    gui._selected_step = gui._selected_run.steps[0]
    gui._update_action_state()
    assert fake_dpg.config("btn_pause")["enabled"] is False
    assert fake_dpg.config("btn_resume")["enabled"] is True
    assert fake_dpg.config("btn_fork")["enabled"] is True


def test_refresh_preserves_the_selected_run_and_step(tmp_path: Path, gui_factory, fake_dpg):
    _seed(tmp_path)
    gui = _console(gui_factory, tmp_path, step=1)
    gui._refresh()
    assert gui._selected_run is not None and gui._selected_run.id == "abc"
    assert gui._selected_step is not None and gui._selected_step.id == "s2"
    assert "Run: abc" in fake_dpg.value("detail_text")
    assert "ID: s2" in fake_dpg.value("step_text")


def test_opening_another_directory_clears_the_selection_and_is_remembered(
    tmp_path: Path, gui_factory, fake_dpg
):
    old, new = tmp_path / "old", tmp_path / "new"
    _seed(old, "abc")
    _seed(new, "zzz")
    gui = _console(gui_factory, old, step=0)
    gui._run_filter, gui._step_filter = "abc", "tool"

    gui._open_dir_picker()
    assert fake_dpg.value("dir_picker_input") == str(old)
    fake_dpg.set_value("dir_picker_input", str(new))
    gui._apply_dir()

    assert gui._runs_dir == new
    assert gui._selected_key is None
    assert gui._selected_run is None and gui._selected_step is None
    assert gui._run_filter == "" and gui._step_filter == ""
    assert fake_dpg.value("run_filter") == "" and fake_dpg.value("step_filter") == ""
    assert fake_dpg.value("detail_text") == "Select a run"
    assert fake_dpg.config("dir_picker")["show"] is False
    stored = json.loads(app._preferences_path().read_text(encoding="utf-8"))
    assert stored["last_runs_dir"] == str(new)

    gui._refresh()
    assert [entry.key for entry in gui._entries] == ["zzz"]


def test_opening_nothing_at_all_is_refused(tmp_path: Path, gui_factory, fake_dpg):
    gui = gui_factory(tmp_path)
    gui._open_dir_picker()
    fake_dpg.set_value("dir_picker_input", "   ")
    gui._apply_dir()
    assert fake_dpg.value("status_bar") == "Type a directory to open"
    assert gui._runs_dir == tmp_path
    assert fake_dpg.config("dir_picker")["show"] is True, "the picker stays open to be fixed"


# ------------------------------------------------------------ structural guards

#: Every bound method the console hands to Dear PyGui as a callback, and what it
#: is wired to. Deliberately a tripwire: a new callback has to be added here,
#: which is also what puts it under the arity check below.
CALLBACK_NAMES: dict[str, str] = {
    "_apply_dir": "Open, and Enter, in the runs-directory picker",
    "_clear_run_filter": "the x beside the run search",
    "_clear_step_filter": "Clear beside the DAG highlight box",
    "_compare_runs": "Compare, and Enter, in the diff dialog",
    "_confirm_accept": "Continue in the confirmation dialog",
    "_confirm_cancel": "Cancel in the confirmation dialog, and Esc over it",
    "_confirm_fork": "Fork in the fork dialog",
    "_copy_diff": "Copy in the diff dialog",
    "_copy_panel": "Copy in the shared read-only panel",
    "_copy_run_detail": "Copy all in the run inspector",
    "_copy_run_id": "Copy id in the run inspector",
    "_copy_step_detail": "Copy all in the step inspector",
    "_copy_step_id": "Copy id in the step inspector",
    "_copy_text_dialog": "Copy in the expanded-text dialog",
    "_copy_transcript": "Copy all in the transcript dialog",
    "_do_import": "Import in the import panel",
    "_expand_run_detail": "Expand in the run inspector",
    "_expand_step_detail": "Expand in the step inspector",
    "_export_otel": "Run > Export as OpenTelemetry JSON",
    "_fit_dag": "Fit above the DAG",
    "_focus_next_match": "Next match above the DAG",
    "_force_refresh": "File > Refresh",
    "_fork_selected": "the Fork button and Run > Fork from step",
    "_move_selection": "Up/Down, through a lambda that supplies the direction",
    "_on_ctrl_c": "Ctrl+C: copy the selected run id",
    "_on_ctrl_f": "Ctrl+F: focus the run search",
    "_on_ctrl_o": "Ctrl+O: open the runs-directory picker",
    "_on_ctrl_r": "Ctrl+R: reload the source now",
    "_on_escape": "Esc: close a modal, else clear a filter",
    "_on_filter_change": "typing in the run search",
    "_on_help_key": "F1: the help dialog",
    "_on_link_created": "a link dragged in the node editor",
    "_on_link_deleted": "a link deleted in the node editor (delink_callback)",
    "_on_run_selected": "clicking a row in the run table",
    "_on_step_filter_change": "Enter in the DAG highlight box",
    "_on_step_open": "clicking a node in the DAG",
    "_on_table_sort": "clicking a run-table column header",
    "_on_transcript_step": "clicking a step link in the transcript dialog",
    "_on_viewport_resize": "the viewport resize callback, not a widget",
    "_open_about": "Help > About",
    "_open_diff_dialog": "the Diff button and Run > Compare with...",
    "_open_dir_picker": "File > Change runs dir...",
    "_open_fork_dialog": "Run > Fork to branch...",
    "_open_help": "Help > Keyboard and features",
    "_open_import_dialog": "File > Import a trace...",
    "_open_pricing": "Run > Price this run...",
    "_open_refs": "View > Repository refs...",
    "_open_stats": "View > Statistics...",
    "_open_transcript": "Run > Transcript...",
    "_pause_selected": "the Pause button and Run > Pause",
    "_pick_recent_dir": "choosing a recent directory in the picker",
    "_quit": "File > Quit",
    "_recompute_price": "Recompute in the pricing panel",
    "_render_stats": "the Group by combo in the statistics panel",
    "_resume_selected": "the Resume button and Run > Resume",
    "_toggle_auto_refresh": "View > Auto-refresh",
    "_toggle_messages": "View > Message log",
}


def test_every_callback_survives_manual_callback_dispatch(tmp_path: Path):
    """The console runs with manual_callback_management, draining the queue itself.

    dpg.run_callbacks builds arguments as job[arg + 1] over a 4-tuple
    (callback, sender, app_data, user_data), so a callback taking more than
    three parameters raises IndexError at click time — never at import. It also
    calls inspect.signature, which some callables reject.
    """
    gui = OpentineGUI(tmp_path)
    for name in CALLBACK_NAMES:
        fn = getattr(gui, name)
        params = len(inspect.signature(fn).parameters)
        assert params <= 3, f"{name} takes {params} params; run_callbacks can supply 3"


def test_the_callback_inventory_matches_the_source(tmp_path: Path):
    # Keeps CALLBACK_NAMES honest in both directions as callbacks come and go.
    # The four patterns are the four ways this module registers one.
    source = Path(app.__file__).read_text(encoding="utf-8")
    registered: set[str] = set()
    for pattern in (
        r"callback=self\.([_a-zA-Z0-9]+)",  # widgets, menu items, delink_callback
        r"callback=lambda[^\n]*?self\.([_a-zA-Z0-9]+)\(",  # wrapped in a lambda
        r"set_viewport_resize_callback\(self\.([_a-zA-Z0-9]+)\)",
        r"_action_button\([^,]+,\s*self\.([_a-zA-Z0-9]+)",  # passed positionally
    ):
        registered |= set(re.findall(pattern, source))

    missing = registered - set(CALLBACK_NAMES)
    assert not missing, f"new callbacks not covered by the arity test: {sorted(missing)}"
    stale = set(CALLBACK_NAMES) - registered
    assert not stale, f"listed here but no longer registered: {sorted(stale)}"
