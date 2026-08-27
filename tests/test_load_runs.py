"""Run loading and path safety: the directory scan, its caches, and what it refuses.

Headless — nothing here touches Dear PyGui. The scan, the caches and the path
rules all live in `opentine_gui.sources` now, so that is what these tests import
and patch: `app` re-exports those names, but a patch applied to the re-export
leaves the real function running underneath it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import sources
from opentine_gui.graphmodel import _graph_stats, _matching_steps, _node_label
from opentine_gui.inspectors import _highlight_summary
from opentine_gui.query import _run_matches_filter
from opentine_gui.sources import (
    MAX_TINE_BYTES,
    DirectorySource,
    RepositorySource,
    _export_path,
    _is_v3_repository,
    _remember,
    _safe_run_path,
    _verify_cached,
    _verify_integrity_cached,
    load_runs,
    open_source,
)
from opentine_gui.text import _format_timestamp, _format_value, _mapping_lines, _truncate

requires_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating a symlink on Windows needs a privilege CI lacks"
)


def _run_with_steps(run_id: str, steps: list[Step], **fields) -> Run:
    graph = Graph()
    for step in steps:
        graph.add(step)
    return Run(id=run_id, graph=graph, **fields)


def _make_run(run_id: str, prompt: str = "hi") -> Run:
    steps = [
        Step(
            id="s1",
            parent_ids=[],
            kind=StepKind.think,
            inputs={"text": "plan"},
            duration=0.1,
        ),
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.tool,
            inputs={"name": "search", "arguments": {"q": "x"}},
            outputs={"result": "ok"},
            tool_info={"name": "search"},
            timestamp=0.1,
            duration=0.2,
            cost=0.001,
        ),
    ]
    return _run_with_steps(
        run_id,
        steps,
        status=RunStatus.completed,
        model_info="claude-sonnet-4-6",
        user_prompt=prompt,
    )


def _stamp(path: Path, when: float) -> None:
    """Give a file an exact mtime.

    The scan orders rows by mtime and binds a duplicate id to the newest file,
    so the tests that assert on that order have to control it. Sleeping between
    two saves does not: a 50 ms gap is below the mtime resolution of several
    filesystems the console is used on, and a tie there makes the assertion
    depend on glob order.
    """
    os.utime(path, (when, when))


def _count_run_loads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every real parse the scan performs, delegating to opentine's loader.

    `Run.load` is what the parsed-run cache exists to avoid, so counting calls to
    it is the only way to tell a cache hit from a re-decode that happens to
    produce an equal run.
    """
    calls: list[Path] = []
    original = sources.Run.load

    def counting(path):
        calls.append(Path(path))
        return original(path)

    monkeypatch.setattr(sources.Run, "load", staticmethod(counting))
    return calls


def test_load_runs_reports_a_missing_directory_instead_of_reading_as_empty(
    tmp_path: Path,
) -> None:
    # A typo'd --runs-dir used to render exactly like a directory with no runs
    # in it ("0 runs"), so the user waited for an agent whose output the console
    # was never going to look at.
    runs, errors, sig, paths = load_runs(tmp_path / "missing")
    assert runs == []
    assert paths == {}
    assert sig == ()
    assert any("no such directory" in e for e in errors)


def test_load_runs_treats_an_existing_empty_directory_as_no_news(tmp_path: Path) -> None:
    # The other half of the rule: a directory an agent has not written to yet is
    # the normal starting state, and must not be dressed up as a failure.
    runs, errors, sig, paths = load_runs(tmp_path)
    assert runs == []
    assert errors == []
    assert sig == ()
    assert paths == {}


# Regression guard. Before this was fixed:
# _dir_signature() answers () for a missing directory and for an existing empty one
    # alike, and the loader skips the scan whenever the signature is unchanged
    # (app.py:458). A console started on a runs directory that does not exist yet keeps
    # its 'no such directory' banner after the directory is created, until a .tine file
    # lands in it or the user forces a refresh -- and the mirror case, a runs directory
    # deleted while empty, is never reported at all. One 'exists' element in the
    # signature tuple would settle both.
def test_creating_the_missing_runs_directory_moves_the_signature(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    source = DirectorySource(runs_dir)
    missing = source.signature()
    assert any("no such directory" in e for e in source.scan().errors)

    runs_dir.mkdir()

    assert source.signature() != missing, "the loader will skip the rescan that clears the banner"


def test_load_runs_reports_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "notadir"
    target.write_text("i am a file")
    runs, errors, _, _ = load_runs(target)
    assert runs == []
    assert any("not a directory" in e for e in errors)


def test_load_runs_reads_saved(tmp_path: Path) -> None:
    r = _make_run("abc")
    r.save(tmp_path / "abc.tine")
    runs, errors, sig, paths = load_runs(tmp_path)
    assert errors == []
    assert [x.id for x in runs] == ["abc"]
    assert len(runs[0].steps) == 2
    assert len(sig) == 1
    assert sig[0][0] == "abc.tine"
    assert paths == {"abc": tmp_path / "abc.tine"}


def test_load_runs_sorted_newest_first(tmp_path: Path) -> None:
    _make_run("old").save(tmp_path / "old.tine")
    _make_run("new").save(tmp_path / "new.tine")
    _stamp(tmp_path / "old.tine", 1_000.0)
    _stamp(tmp_path / "new.tine", 2_000.0)
    runs, _, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["new", "old"]


def test_load_runs_reports_corrupt_files(tmp_path: Path) -> None:
    _make_run("good").save(tmp_path / "good.tine")
    (tmp_path / "bad.tine").write_bytes(b"not a valid msgpack")
    runs, errors, sig, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["good"]
    assert len(errors) == 1
    assert "bad.tine" in errors[0]
    # signature covers both files (it reflects on-disk state, not load success)
    assert {e[0] for e in sig} == {"good.tine", "bad.tine"}


def test_load_runs_skips_oversized(tmp_path: Path) -> None:
    _make_run("ok").save(tmp_path / "ok.tine")
    big = tmp_path / "huge.tine"
    big.write_bytes(b"\0" * (MAX_TINE_BYTES + 1))
    runs, errors, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["ok"]
    assert any("huge.tine" in e and "skipped" in e for e in errors)


def test_load_runs_signature_matches_what_was_loaded(tmp_path: Path) -> None:
    _make_run("a").save(tmp_path / "a.tine")
    _make_run("b").save(tmp_path / "b.tine")
    _, _, sig1, _ = load_runs(tmp_path)
    _, _, sig2, _ = load_runs(tmp_path)
    # Same directory state => identical signatures => no spurious reload loop.
    assert sig1 == sig2


def test_load_runs_keeps_newest_of_duplicate_ids_and_reports_the_shadowed_file(
    tmp_path: Path,
) -> None:
    _make_run("dup").save(tmp_path / "older-copy.tine")
    _make_run("dup").save(tmp_path / "newer-copy.tine")
    _stamp(tmp_path / "older-copy.tine", 1_000.0)
    _stamp(tmp_path / "newer-copy.tine", 2_000.0)
    runs, errors, _, paths = load_runs(tmp_path)
    # One row per id: a second row with the same id would be an unselectable
    # duplicate, since selection and every action resolve a run by its id.
    assert [r.id for r in runs] == ["dup"]
    # Actions must target the file selection binds to: the newest by mtime.
    assert paths["dup"] == tmp_path / "newer-copy.tine"
    # The collision is surfaced rather than silently dropped.
    assert any("duplicate run id" in e and "older-copy.tine" in e for e in errors)


def test_load_runs_flags_integrity_mismatch_but_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "tampered.tine"
    _make_run("tampered").save(path)
    raw = json.loads(path.read_text())
    step = next(iter(raw["graph"]["steps"].values()))
    step["outputs"]["result"] = "edited after signing"
    path.write_text(json.dumps(raw))
    runs, errors, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["tampered"]
    assert any("integrity" in e for e in errors)


def test_load_runs_migrates_legacy_v1_fixture(tmp_path: Path) -> None:
    legacy = Path(__file__).parent / "fixtures" / "legacy_v1.tine"
    assert json.loads(legacy.read_text())["format_version"] == 1
    shutil.copy2(legacy, tmp_path / "legacy.tine")
    _make_run("modern").save(tmp_path / "modern.tine")
    runs, errors, _, _ = load_runs(tmp_path)
    assert not any("legacy.tine" in e for e in errors)
    migrated = next(r for r in runs if r.id == "demo-complete")
    assert migrated.format_version == 2
    assert migrated.metadata.get("migration"), "v1 load should record migration provenance"


def test_load_runs_reports_unsupported_future_version(tmp_path: Path) -> None:
    (tmp_path / "future.tine").write_text(json.dumps({"format_version": 3}))
    runs, errors, _, _ = load_runs(tmp_path)
    assert runs == []
    assert any("future.tine" in e for e in errors)


def test_a_v3_repository_goes_to_the_read_only_reader_not_the_glob(tmp_path: Path) -> None:
    # Run.load redirects a repository DIRECTORY to Repo.open(...).load_run('heads/main'),
    # so globbing *.tine used to match the repo's own .tine/ directory and show one
    # run out of many — and Pause would have rewritten the repository's branch.
    from opentine.core import Repo

    work = tmp_path / "work"
    work.mkdir()
    Repo.init(work)
    runs, errors, sig, paths = load_runs(work)
    assert runs == [] and paths == {}
    assert any("v3 repository" in e for e in errors)
    assert sig == ()
    # The console no longer stops there: open_source hands a repository to the
    # reader that lists every run in it and refuses every write.
    source = open_source(work)
    assert isinstance(source, RepositorySource)
    assert source.writable is False
    assert source.scan().writable is False


def test_a_directory_named_like_a_run_is_skipped(tmp_path: Path) -> None:
    _make_run("real").save(tmp_path / "real.tine")
    (tmp_path / "notarun.tine").mkdir()
    runs, errors, _, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["real"]
    assert not errors, "a directory is not a corrupt run"


def test_is_v3_repository_detects_both_layouts(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / ".tine").mkdir(parents=True)
    (worktree / ".tine" / "config.json").write_text("{}")
    assert _is_v3_repository(worktree)
    # ...and the object directory itself, which Run.load also redirects on.
    assert _is_v3_repository(worktree / ".tine")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not _is_v3_repository(plain)
    assert not _is_v3_repository(tmp_path / "missing")


# --------------------------------------------------------- hostile directories


@requires_symlinks
def test_a_symlinked_run_file_is_listed_but_nothing_derived_follows_it_out(
    tmp_path: Path,
) -> None:
    # The scan globs and stats without resolving, so a symlink planted in the
    # runs directory is read like any other row — it names a file the user put
    # there themselves. What must not happen is a *derived* write following it
    # back out: a fork output or an export is placed by run id, and an id is
    # artifact-controlled text.
    outside = tmp_path / "outside"
    outside.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _make_run("escaped").save(outside / "real.tine")
    (runs_dir / "escaped.tine").symlink_to(outside / "real.tine")
    _make_run("inside").save(runs_dir / "inside.tine")

    runs, errors, _, paths = load_runs(runs_dir)
    assert sorted(r.id for r in runs) == ["escaped", "inside"]
    assert errors == []
    # The link, not its target. opentine saves by writing a temp file in the
    # target's own directory and os.replace-ing it, so an in-place Pause lands
    # inside the runs directory even here: it replaces the link.
    assert paths["escaped"] == runs_dir / "escaped.tine"
    for build_path in (_safe_run_path, _export_path):
        with pytest.raises(ValueError, match="escapes runs dir"):
            build_path(runs_dir, "escaped")


@requires_symlinks
def test_a_symlinked_runs_directory_is_read_through_the_name_it_was_given(
    tmp_path: Path,
) -> None:
    # ~/runs pointing at a project directory is an ordinary setup, and both the
    # listing and the write path have to agree on which name they are using —
    # _safe_run_path resolves before comparing, so a symlinked root must not
    # look like an escape from itself.
    real = tmp_path / "real"
    real.mkdir()
    _make_run("abc").save(real / "abc.tine")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    runs, errors, _, paths = load_runs(link)
    assert [r.id for r in runs] == ["abc"]
    assert errors == []
    assert paths["abc"] == link / "abc.tine"
    assert _safe_run_path(link, "abc") == real.resolve() / "abc.tine"


@requires_symlinks
def test_a_broken_symlink_is_reported_and_kept_out_of_the_signature(tmp_path: Path) -> None:
    # glob yields a dangling link without stat-ing it; the scan's own stat is
    # what fails. It must cost that one row, not the directory.
    (tmp_path / "ghost.tine").symlink_to(tmp_path / "nothing.tine")
    _make_run("real").save(tmp_path / "real.tine")
    runs, errors, sig, _ = load_runs(tmp_path)
    assert [r.id for r in runs] == ["real"]
    assert any("ghost.tine" in e for e in errors)
    # No stat, no signature entry: creating the target later changes the
    # directory fingerprint, which is what triggers the rescan that picks it up.
    assert [name for name, _, _ in sig] == ["real.tine"]


@requires_symlinks
def test_a_looping_directory_symlink_is_an_error_not_a_crash(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop, target_is_directory=True)
    runs, errors, sig, paths = load_runs(loop)
    assert runs == [] and paths == {} and sig == ()
    # Path.exists() swallows ELOOP and answers False, so the console calls this
    # a missing directory. Imprecise, but it is a message, not a traceback on
    # the loader thread.
    assert errors and str(loop) in errors[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_fifo_named_like_a_run_is_refused_rather_than_opened(tmp_path: Path) -> None:
    # Opening a FIFO with no writer blocks forever, and the scan runs on the
    # loader thread, which nothing can cancel: the console would go on drawing
    # while its run list quietly stopped updating for the rest of the session.
    os.mkfifo(tmp_path / "pipe.tine")
    _make_run("real").save(tmp_path / "real.tine")

    result: list[tuple] = []
    worker = threading.Thread(target=lambda: result.append(load_runs(tmp_path)), daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), "the scan blocked on a FIFO instead of refusing it"

    runs, errors, _, _ = result[0]
    assert [r.id for r in runs] == ["real"]
    assert any("pipe.tine" in e for e in errors)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else False, reason="root reads all")
def test_an_unreadable_run_file_costs_one_row_not_the_directory(tmp_path: Path) -> None:
    secret = tmp_path / "secret.tine"
    _make_run("secret").save(secret)
    _make_run("readable").save(tmp_path / "readable.tine")
    secret.chmod(0o000)
    try:
        runs, errors, sig, paths = load_runs(tmp_path)
    finally:
        secret.chmod(0o600)  # so tmp_path teardown can remove it
    assert [r.id for r in runs] == ["readable"]
    assert paths == {"readable": tmp_path / "readable.tine"}
    assert any("secret.tine" in e for e in errors)
    # stat() succeeded, so the file is still part of the directory fingerprint.
    assert {name for name, _, _ in sig} == {"secret.tine", "readable.tine"}


def test_a_file_rewritten_between_stat_and_parse_is_re_read_next_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loader walks a directory a live agent is writing to, and opentine saves
    # by atomic rename — so the inode the scan sized is not always the inode it
    # then parses. The parsed run gets cached under the revision the *stat* saw,
    # and the danger is pinning the newer bytes to that stale key forever.
    path = tmp_path / "live.tine"
    _make_run("live", prompt="first prompt").save(path)
    sized = path.stat().st_size
    original = sources.Run.load
    calls: list[Path] = []

    def rewrite_then_load(p):
        calls.append(Path(p))
        if len(calls) == 1:
            _make_run("live", prompt="second prompt, rather longer than the first").save(Path(p))
        return original(p)

    monkeypatch.setattr(sources.Run, "load", staticmethod(rewrite_then_load))

    runs, errors, sig, _ = load_runs(tmp_path)
    assert errors == []
    assert [r.user_prompt for r in runs] == ["second prompt, rather longer than the first"]
    # The signature records what the scan actually gated on, not what it read...
    ((name, _, size),) = sig
    assert (name, size) == (path.name, sized)
    assert path.stat().st_size != sized
    # ...so the next tick sees a changed directory, and re-reads rather than
    # serving the run it cached against a revision that never reached disk.
    runs_again, _, sig_again, _ = load_runs(tmp_path)
    assert sig_again != sig
    assert len(calls) == 2, "the mid-scan bytes stayed pinned to the stale revision key"
    assert [r.user_prompt for r in runs_again] == ["second prompt, rather longer than the first"]


def test_a_file_truncated_mid_scan_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not every writer in a runs directory is opentine's own atomic save: an
    # rsync, a half-finished copy or a crashed export can leave a file that is
    # whole at stat time and empty by the time it is read.
    doomed = tmp_path / "doomed.tine"
    _make_run("doomed").save(doomed)
    _make_run("intact").save(tmp_path / "intact.tine")
    original = sources.Run.load

    def truncate_then_load(p):
        if Path(p) == doomed:
            Path(p).write_bytes(b"")
        return original(p)

    monkeypatch.setattr(sources.Run, "load", staticmethod(truncate_then_load))
    runs, errors, _, paths = load_runs(tmp_path)
    assert [r.id for r in runs] == ["intact"]
    assert paths == {"intact": tmp_path / "intact.tine"}
    assert any("doomed.tine" in e for e in errors)


# ---------------------------------------------------------------- the caches


def test_a_second_scan_of_an_unchanged_file_does_not_re_parse_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The auto refresh re-reads the whole directory every few seconds. Decoding
    # every artifact each time is what made a directory of large runs peg a core
    # while the user was doing nothing at all.
    _make_run("abc").save(tmp_path / "abc.tine")
    calls = _count_run_loads(monkeypatch)
    first, _, _, _ = load_runs(tmp_path)
    second, _, _, _ = load_runs(tmp_path)
    assert len(calls) == 1
    assert second[0] is first[0], "an unchanged file must come back as the same parsed run"


def test_a_rewritten_file_is_parsed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The cache is keyed by file revision, not by path: a run an agent has since
    # written more steps into must not be served from the previous pass.
    path = tmp_path / "abc.tine"
    _make_run("abc", prompt="first").save(path)
    calls = _count_run_loads(monkeypatch)
    load_runs(tmp_path)
    _make_run("abc", prompt="second").save(path)
    runs, _, _, _ = load_runs(tmp_path)
    assert len(calls) == 2
    assert [r.user_prompt for r in runs] == ["second"]


def test_forget_run_drops_every_cached_revision_of_the_file_it_names(tmp_path: Path) -> None:
    # Pause and Resume rewrite a file in place. The revision key would usually
    # catch that on its own, but not everywhere: on Windows st_ctime is creation
    # time, so a same-size rewrite with mtime restored still hashes equal. The
    # write path therefore drops the entry outright instead of trusting the key.
    path = tmp_path / "abc.tine"
    other = tmp_path / "other.tine"
    _make_run("abc", prompt="first").save(path)
    _make_run("other").save(other)
    load_runs(tmp_path)
    _make_run("abc", prompt="second").save(path)
    load_runs(tmp_path)
    assert len([k for k in sources._RUN_CACHE if k[0] == str(path)]) == 2, "two revisions cached"

    sources._forget_run(path)
    assert not [k for k in sources._RUN_CACHE if k[0] == str(path)]
    assert [k for k in sources._RUN_CACHE if k[0] == str(other)], (
        "writing one file must not throw away every other run in the directory"
    )


def test_forget_run_makes_the_next_scan_re_read_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path)
    calls = _count_run_loads(monkeypatch)
    load_runs(tmp_path)
    load_runs(tmp_path)
    assert len(calls) == 1, "precondition: the second scan was a cache hit"
    sources._forget_run(path)
    load_runs(tmp_path)
    assert len(calls) == 2, "after a write the console must go back to disk"


def test_cache_eviction_is_bounded_and_oldest_first() -> None:
    # Not a full clear at the ceiling: that would make a directory holding more
    # revisions than the cap re-decode and re-hash everything on every pass —
    # the busy directory the cache exists for is exactly the worst case.
    cache: dict = {}
    for i in range(9):
        _remember(cache, ("k", i), i, 8)
    assert len(cache) == 7, "a full clear would have left one entry"
    assert list(cache) == [("k", i) for i in range(2, 9)], "the oldest two went, and only those"


def test_the_parsed_run_cache_stays_within_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long-lived console over a directory an agent keeps adding to must not
    # hold every run it has ever seen: the cache is a speed-up, not a store.
    monkeypatch.setattr(sources, "_RUN_CACHE_MAX", 4)
    for i in range(10):
        _make_run(f"run{i}").save(tmp_path / f"run{i}.tine")
    runs, errors, _, _ = load_runs(tmp_path)
    assert len(runs) == 10 and errors == []
    assert 0 < len(sources._RUN_CACHE) <= 4, "the cache must stay bounded without switching off"


def test_verify_cache_is_effective_for_an_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path)
    calls: list[Path] = []

    def counting(p: Path):
        calls.append(p)
        return Run.verify_integrity(p)

    for _ in range(5):
        _verify_cached(path, path.stat(), "integrity", counting)
    assert len(calls) == 1, "an unchanged file must be verified once, not per refresh"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "The cache key relies on st_ctime, which POSIX bumps on any write and no "
        "writer can backdate. On Windows st_ctime is creation time, so a same-size, "
        "mtime-restored rewrite is served from cache until the file changes again — "
        "the residual limitation documented on _verify_cached."
    ),
)
def test_verify_cache_detects_a_size_and_mtime_preserving_tamper(tmp_path: Path) -> None:
    # An integrity check exists to catch tampering, and os.utime lets a writer put
    # mtime back after a same-length edit. The cache key must not be fooled by that.
    path = tmp_path / "abc.tine"
    _make_run("abc").save(path)
    before = path.stat()
    assert _verify_integrity_cached(path, before)["ok"]

    raw = path.read_bytes()
    assert b'"ok"' in raw
    path.write_bytes(raw.replace(b'"ok"', b'"XX"', 1))  # byte-for-byte same length
    os.utime(path, (before.st_atime, before.st_mtime))
    after = path.stat()
    assert after.st_size == before.st_size and after.st_mtime == before.st_mtime

    assert not Run.verify_integrity(path).ok, "precondition: the file really is tampered"
    assert not _verify_integrity_cached(path, after)["ok"], "stale verdict served"


def test_search_cache_invalidates_when_status_changes_in_place() -> None:
    # Run search text is cached per Run for typing responsiveness, but pause()
    # mutates status on the same object — the cache must not keep saying "running".
    run = _make_run("abc")
    run.status = RunStatus.running
    assert _run_matches_filter(run, "running")
    assert not _run_matches_filter(run, "paused")
    run.status = RunStatus.paused
    assert _run_matches_filter(run, "paused")
    assert not _run_matches_filter(run, "running")


def test_filters_tolerate_null_model_info(tmp_path: Path) -> None:
    # Third-party .tine files can carry model_info: null; filters must not raise.
    path = tmp_path / "nullmodel.tine"
    _make_run("nullmodel").save(path)
    raw = json.loads(path.read_text())
    for step in raw["graph"]["steps"].values():
        step["model_info"] = None
    path.write_text(json.dumps(raw))
    runs, _, _, _ = load_runs(tmp_path)
    (run,) = runs
    assert _run_matches_filter(run, "nullmodel")
    assert not _run_matches_filter(run, "no-such-text")
    assert _matching_steps(run, "search") == ["s2"]


def test_format_timestamp_survives_out_of_range_values() -> None:
    assert _format_timestamp(0) == "(unknown)"
    assert _format_timestamp(1e30).startswith("(invalid timestamp")


def test_truncate_sanitizes_lone_surrogates() -> None:
    label = _truncate("run-\ud800-id", 50)
    assert "\ud800" not in label
    label.encode("utf-8")  # must be encodable for DPG's native renderer


def test_safe_run_path_accepts_normal_id(tmp_path: Path) -> None:
    p = _safe_run_path(tmp_path, "abc123")
    assert p == (tmp_path / "abc123.tine").resolve()


def test_safe_run_path_accepts_hyphen_underscore_dot(tmp_path: Path) -> None:
    p = _safe_run_path(tmp_path, "run_2026-04-15.v1")
    assert p.name == "run_2026-04-15.v1.tine"


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",
        "../../evil",
        "..\\evil",
        "/abs/path",
        "C:\\Windows\\evil",
        "sub/dir",
        "has space",
        "",
        ".hidden",
        "name\x00null",
        "abc\n",
    ],
)
def test_safe_run_path_rejects_traversal_and_unsafe(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _safe_run_path(tmp_path, bad)


def test_safe_run_path_resolved_stays_inside(tmp_path: Path) -> None:
    # Even if the regex somehow let something through, the resolve check blocks escape.
    # Confirm legitimate ID resolves under runs_dir.
    p = _safe_run_path(tmp_path, "legit")
    assert tmp_path.resolve() in p.parents


def test_run_filter_matches_status_prompt_and_step_payload() -> None:
    run = _make_run("demo-search", prompt="Build a timeline")
    assert _run_matches_filter(run, "completed")
    assert _run_matches_filter(run, "timeline")
    assert _run_matches_filter(run, "search")
    assert not _run_matches_filter(run, "missing")


def test_format_value_pretty_prints_nested_data() -> None:
    rendered = _format_value({"b": 2, "a": {"nested": True}}, 200)
    assert '"a": {' in rendered
    assert '"nested": true' in rendered


def test_mapping_lines_handles_empty_and_nested_values() -> None:
    assert _mapping_lines({}) == ["  (none)"]
    lines = _mapping_lines({"arguments": {"q": "x"}})
    assert lines[0] == "  arguments:"
    assert any('"q": "x"' in line for line in lines)


def test_graph_stats_describe_branched_run() -> None:
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(id="s2", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "search"}),
        Step(
            id="s3",
            parent_ids=["s1"],
            kind=StepKind.model,
            inputs={"text": "other branch"},
            outputs={"text": "summary"},
            model_info="m",
            duration=0.3,
            cost=0.002,
        ),
    ]
    run = _run_with_steps("branched", steps)
    assert _graph_stats(run) == {
        "roots": 1,
        "links": 2,
        "causal": 0,
        "branches": 1,
        "max_depth": 1,
    }


def test_graph_stats_counts_multi_parent_merge() -> None:
    # s3 merges two parents -> two links into one node, depth 2.
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(id="s2a", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "a"}),
        Step(id="s2b", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "b"}),
        Step(id="s3", parent_ids=["s2a", "s2b"], kind=StepKind.done, inputs={"text": "merged"}),
    ]
    run = _run_with_steps("merge", steps)
    assert _graph_stats(run) == {
        "roots": 1,
        "links": 4,
        "causal": 0,
        "branches": 1,
        "max_depth": 2,
    }


def test_graph_stats_counts_causal_edges_apart_from_lineage() -> None:
    # A fork keeps the causal closure, not the parent closure, so a summary that
    # counted only lineage would describe a smaller graph than Fork would copy.
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(id="s2", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "search"}),
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.model,
            inputs={"text": "answer"},
            causal_ids=["s1", "s2", "in-another-run"],
        ),
    ]
    run = _run_with_steps("causal", steps)
    # s2 is already drawn as lineage and the third id belongs to another run in
    # the v3 store: one edge is left worth drawing.
    assert _graph_stats(run) == {
        "roots": 1,
        "links": 2,
        "causal": 1,
        "branches": 0,
        "max_depth": 2,
    }


def test_step_filter_and_labels_support_graph_search() -> None:
    run = _make_run("demo-search")
    assert _matching_steps(run, "search") == ["s2"]
    assert _highlight_summary(run, {"s2"}) == "Matches: tool: search"
    assert _node_label(run.steps[1], highlighted=True) == "* tool: search"

    done = Step(
        id="s3",
        parent_ids=["s2"],
        kind=StepKind.done,
        inputs={},
        outputs={"answer": "final answer"},
    )
    assert _node_label(done) == "done: final answer"

    # done text may live in inputs (opentine's runtime writes it there)
    done_inputs = Step(id="s4", parent_ids=["s3"], kind=StepKind.done, inputs={"text": "all set"})
    assert _node_label(done_inputs) == "done: all set"

    # error steps carry their message in step.error, not inputs
    err = Step(
        id="e1",
        parent_ids=["s2"],
        kind=StepKind.error,
        inputs={},
        error={"type": "ValueError", "message": "boom"},
    )
    assert _node_label(err) == "error: boom"


def test_step_filter_matches_tool_info_and_error() -> None:
    steps = [
        Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}),
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.tool,
            inputs={"arguments": {"q": "x"}},
            tool_info={"name": "web_search"},
        ),
        Step(
            id="s3",
            parent_ids=["s2"],
            kind=StepKind.error,
            inputs={},
            error={"type": "TimeoutError", "message": "upstream timed out"},
        ),
    ]
    run = _run_with_steps("searchable", steps)
    assert _matching_steps(run, "web_search") == ["s2"]
    assert _matching_steps(run, "timeout") == ["s3"]
    assert _run_matches_filter(run, "upstream timed out")
