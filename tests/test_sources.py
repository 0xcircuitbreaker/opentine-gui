"""Where runs come from, and what the console is allowed to do to what it finds.

Two sources, two contracts. A directory of loose `.tine` files is the console's
own working store: it may be written to, so every row has to carry the exact
file a write must land in. An opentine v3 repository is somebody's provenance
history, usually committed to git or served from a read-only mount: the console
reads it and leaves it byte for byte alone.

That second contract is the one worth tests. `Repo.open(path)` heals a
repository layout on the way in, mkdir-ing whatever is missing, so merely
*looking* at a repository through the obvious API drops untracked directories
into someone's checkout — the two tests that snapshot the whole tree around a
scan are what hold `RepositorySource` to `Repo(<.tine dir>)` instead.

Repository fixtures are built with `opentine.repo.Repo` rather than hand-written
bytes, so the object layout, the ref encoding and the envelope framing are
whatever the library really writes and not what this file believes about them.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind
from opentine.repo import Repo

from opentine_gui import sources
from opentine_gui.sources import (
    DirectorySource,
    RepositorySource,
    _export_path,
    _safe_run_path,
    _safe_sibling,
    _short_oid,
    open_source,
)


def _golden_repo() -> Path | None:
    """The v0.7.0 compatibility repository from a local opentine checkout.

    A real repository with two runs, an annotation ref and a fork, none of
    which this file could plausibly hand-build. Found through `OPENTINE_SRC`,
    or as a checkout sitting beside this one — never a hard-coded home
    directory, so the test means the same thing on anybody's machine. Optional:
    the library checkout is not part of this package's test environment, and
    the one test that wants it skips rather than fails without it.
    """
    roots = [Path(os.environ["OPENTINE_SRC"])] if os.environ.get("OPENTINE_SRC") else []
    # The sibling checkout: <parent>/opentine beside <parent>/opentine-gui.
    roots.append(Path(__file__).resolve().parent.parent.parent / "opentine")
    for root in roots:
        repo = root / "tests" / "fixtures" / "compat" / "v0_7_0" / "repo"
        if (repo / ".tine" / "config.json").is_file():
            return repo
    return None


GOLDEN_REPO = _golden_repo()

needs_golden_repo = pytest.mark.skipif(
    GOLDEN_REPO is None,
    reason="no opentine checkout with the v0_7_0 golden repository (set OPENTINE_SRC)",
)

posix_permissions = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permission bits, and root ignores them",
)


def _run(run_id: str, prompt: str = "hi") -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": prompt}))
    graph.add(
        Step(id="s2", parent_ids=["s1"], kind=StepKind.done, inputs={}, outputs={"text": "ok"})
    )
    return Run(
        id=run_id,
        graph=graph,
        status=RunStatus.completed,
        model_info="claude-sonnet-4-6",
        user_prompt=prompt,
    )


def _tree(root: Path) -> dict[str, tuple]:
    """Every path under root, with its type, size and nanosecond mtime.

    lstat rather than stat so a symlink is compared as a symlink, and the root
    directory itself is included so a file created directly beside `config.json`
    shows up as a changed directory mtime even if nothing else moved.
    """
    st = root.lstat()
    snapshot = {"": (True, 0, st.st_mtime_ns)}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        snapshot[str(path.relative_to(root))] = (path.is_dir(), info.st_size, info.st_mtime_ns)
    return snapshot


# --- choosing a source ------------------------------------------------------


def test_a_directory_of_tine_files_opens_as_a_writable_directory_source(tmp_path: Path) -> None:
    _run("abc").save(tmp_path / "abc.tine")

    source = open_source(tmp_path)

    assert isinstance(source, DirectorySource)
    assert source.kind == "directory"
    assert source.writable is True
    assert source.label == str(tmp_path)


def test_a_v3_worktree_opens_as_a_read_only_repository_source(tmp_path: Path) -> None:
    # This used to be refused outright ("this console reads loose .tine files").
    # It is now opened, and read-only-ness is what keeps it safe.
    worktree = tmp_path / "project"
    Repo.init(worktree).put_run(_run("in-repo"), ref="heads/main")

    source = open_source(worktree)

    assert isinstance(source, RepositorySource)
    assert source.kind == "repository"
    assert source.writable is False
    assert source.tine_dir == worktree / ".tine"
    assert "v3 repository" in source.label


def test_a_bare_object_directory_opens_as_a_repository_source(tmp_path: Path) -> None:
    # A bare repository has no `.tine/` wrapper: config.json sits in the
    # directory the user pointed at, and that directory *is* the object store.
    bare = tmp_path / "bare"
    Repo.init(bare, bare=True).put_run(_run("in-bare"), ref="heads/main")

    source = open_source(bare)

    assert isinstance(source, RepositorySource)
    assert source.tine_dir == bare
    assert [entry.id for entry in source.scan().entries] == ["in-bare"]


def test_a_path_that_does_not_exist_opens_as_a_directory_and_says_so(tmp_path: Path) -> None:
    source = open_source(tmp_path / "typo")

    assert isinstance(source, DirectorySource)
    snapshot = source.scan()
    assert snapshot.entries == []
    assert any("no such directory" in error for error in snapshot.errors)


# --- directory scans --------------------------------------------------------


def test_a_directory_row_carries_the_file_a_write_would_land_in(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run("abc").save(path)

    snapshot = DirectorySource(tmp_path).scan()

    assert snapshot.kind == "directory"
    assert snapshot.root == tmp_path
    assert [entry.key for entry in snapshot.entries] == ["abc"]
    entry = snapshot.entries[0]
    assert entry.id == "abc"
    assert entry.path == path
    assert entry.location == "abc.tine"
    assert entry.size == path.stat().st_size
    assert entry.mtime == pytest.approx(path.stat().st_mtime)
    assert entry.refs == ()


def test_a_directory_snapshot_is_writable_and_offers_its_runs(tmp_path: Path) -> None:
    _run("one").save(tmp_path / "one.tine")
    _run("two").save(tmp_path / "two.tine")

    snapshot = DirectorySource(tmp_path).scan()

    assert snapshot.writable is True
    assert sorted(str(run.id) for run in snapshot.runs) == ["one", "two"]
    assert snapshot.entry("two") is not None
    assert snapshot.entry("nope") is None
    assert snapshot.refs == {}
    assert snapshot.shallow is False
    assert snapshot.note == ""


def test_a_directory_signature_moves_when_a_run_is_added(tmp_path: Path) -> None:
    # The loader skips a rescan when the signature is unchanged, so a signature
    # that missed a new file would freeze the run list of a live agent's output.
    source = DirectorySource(tmp_path)
    before = source.signature()

    _run("fresh").save(tmp_path / "fresh.tine")

    assert source.signature() != before
    assert source.scan().signature == source.signature()


# --- repository scans -------------------------------------------------------


def test_a_repository_scan_finds_every_run_object_keyed_by_object_id(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path / "project")
    first = repo.put_run(_run("alpha"), ref="heads/main").run_id
    second = repo.put_run(_run("beta"), ref="heads/experiment").run_id

    snapshot = open_source(tmp_path / "project").scan()

    assert snapshot.errors == []
    assert {entry.key for entry in snapshot.entries} == {first, second}
    assert {entry.id for entry in snapshot.entries} == {"alpha", "beta"}
    # Spelled out rather than compared against _short_oid(): a row must show
    # the CLI's short form, and asserting it through the same helper the code
    # uses would survive that helper being replaced by identity.
    for entry in snapshot.entries:
        assert entry.location == f"run:{entry.key.rpartition(':')[2][:12]}"


def test_a_repository_row_has_no_file_and_its_snapshot_is_not_writable(tmp_path: Path) -> None:
    # `path is None` plus `writable False` is what disables Pause/Resume/Fork:
    # a Run.save into a repository would append an object and move a branch.
    Repo.init(tmp_path / "project").put_run(_run("alpha"), ref="heads/main")

    snapshot = open_source(tmp_path / "project").scan()

    assert snapshot.writable is False
    assert all(entry.path is None for entry in snapshot.entries)


def test_repository_rows_carry_the_refs_that_point_at_them(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path / "project")
    tip = repo.put_run(_run("tip"), ref="heads/main").run_id
    repo.update_ref("heads/release", tip)
    repo.put_run(_run("side"), ref="heads/experiment")

    snapshot = open_source(tmp_path / "project").scan()

    by_id = {entry.id: entry for entry in snapshot.entries}
    assert by_id["tip"].refs == ("heads/main", "heads/release")
    assert by_id["side"].refs == ("heads/experiment",)
    assert snapshot.refs["heads/main"] == tip


def test_an_unreferenced_run_object_still_appears_with_no_refs(tmp_path: Path) -> None:
    # The listing walks objects, not refs: a run whose branch was deleted is
    # still history, and dropping it would silently hide it from the console.
    repo = Repo.init(tmp_path / "project")
    repo.put_run(_run("kept"), ref="heads/main")
    repo.put_run(_run("orphan"))

    snapshot = open_source(tmp_path / "project").scan()

    by_id = {entry.id: entry for entry in snapshot.entries}
    assert set(by_id) == {"kept", "orphan"}
    assert by_id["orphan"].refs == ()


def test_two_runs_sharing_one_legacy_run_id_both_appear(tmp_path: Path) -> None:
    # A directory dedupes by run id, because two files claiming one id fight
    # over the same write target. A repository must not: the id is the agent's
    # label, the object id is the identity, and re-running a script that hard
    # codes its run id is ordinary.
    repo = Repo.init(tmp_path / "project")
    first = repo.put_run(_run("same-id", "first attempt"), ref="heads/main").run_id
    second = repo.put_run(_run("same-id", "second attempt"), ref="heads/retry").run_id

    snapshot = open_source(tmp_path / "project").scan()

    assert first != second
    assert {entry.key for entry in snapshot.entries} == {first, second}
    assert [entry.id for entry in snapshot.entries] == ["same-id", "same-id"]


def test_scanning_a_repository_writes_nothing_to_it(tmp_path: Path) -> None:
    """THE contract of RepositorySource, asserted byte for byte."""
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    repo.put_run(_run("alpha"), ref="heads/main")
    repo.put_run(_run("beta"), ref="heads/experiment")
    before = _tree(worktree)

    snapshot = open_source(worktree).scan()

    assert len(snapshot.entries) == 2
    # No new object, no moved ref, no reflog line, no stale lock file.
    assert _tree(worktree) == before


def test_scanning_a_git_checked_out_repository_does_not_heal_its_layout(tmp_path: Path) -> None:
    # The mistake this guards against, in the shape it really arrives in: git
    # cannot store an empty directory, so a repository committed to a project
    # arrives with `packs/`, `indexes/`, `logs/` and `refs/tags/` missing, and
    # `Repo.open(path)` mkdirs every one of them on the way in. That is four
    # untracked directories in someone's working tree — and an outright failure
    # on the read-only mount an audit copy is usually served from. The source
    # constructs `Repo(<.tine dir>)` directly to avoid it.
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    repo.put_run(_run("alpha"), ref="heads/main")
    for name in ("logs", "packs", "indexes", "refs/tags"):
        directory = worktree / ".tine" / name
        if directory.is_dir():
            shutil.rmtree(directory)
    before = _tree(worktree)

    snapshot = open_source(worktree).scan()

    assert [entry.id for entry in snapshot.entries] == ["alpha"]
    assert _tree(worktree) == before


@needs_golden_repo
def test_a_real_repository_from_the_opentine_checkout_reads_without_touching_it(
    tmp_path: Path,
) -> None:
    assert GOLDEN_REPO is not None  # guaranteed by the skip marker
    worktree = tmp_path / "golden"
    shutil.copytree(GOLDEN_REPO, worktree)
    before = _tree(worktree)

    snapshot = open_source(worktree).scan()

    assert _tree(worktree) == before
    assert snapshot.errors == []
    assert len(snapshot.entries) == 2
    assert {ref for ref in snapshot.refs} >= {"heads/main", "heads/experiment"}
    # An annotation ref points at an annotation object, not a run: it belongs in
    # the refs panel but must not be mistaken for a run row.
    annotations = [name for name in snapshot.refs if name.startswith("annotations/")]
    assert annotations
    assert all(entry.key.startswith("run:") for entry in snapshot.entries)
    tips = {ref for entry in snapshot.entries for ref in entry.refs}
    assert tips == {"heads/main", "heads/experiment"}


def test_repository_rows_are_newest_first(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path / "project")
    older = _run("older")
    older.created_at = 1000.0
    newer = _run("newer")
    newer.created_at = 2000.0
    repo.put_run(older, ref="heads/older")
    repo.put_run(newer, ref="heads/newer")

    snapshot = open_source(tmp_path / "project").scan()

    assert [entry.id for entry in snapshot.entries] == ["newer", "older"]


def test_a_repository_signature_moves_when_a_ref_moves(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    repo.put_run(_run("first"), ref="heads/main")
    source = open_source(worktree)
    before = source.signature()

    repo.put_run(_run("second"), ref="heads/main")

    assert source.signature() != before


def test_a_repository_signature_moves_when_an_object_arrives_without_a_ref(
    tmp_path: Path,
) -> None:
    # A run can be written into a repository without any branch moving (an
    # import, a `put_run` with no ref). The scan lists those runs, so the
    # signature has to notice them arriving — otherwise the console skips the
    # rescan and the run never appears.
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    source = open_source(worktree)
    # Backdate the object store, so this asserts that the write moved the
    # signature rather than that two calls fell in different clock ticks.
    os.utime(worktree / ".tine" / "objects", (0, 0))
    before = source.signature()

    repo.put_run(_run("orphan", "no ref at all"))

    assert source.signature() != before
    assert [entry.id for entry in source.scan().entries] == ["orphan"]


# Regression guard. Before this was fixed:
# RepositorySource.signature() stats only the top objects/ directory, whose mtime
    # does not move when an object lands in an already-existing objects/run/<shard>/.
    # Once a repository holds one run, further unreferenced runs are invisible to the
    # change check, so the auto refresh never rescans and they never reach the list.
def test_a_repository_signature_moves_when_a_later_object_arrives_without_a_ref(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    repo.put_run(_run("first"), ref="heads/main")
    source = open_source(worktree)
    os.utime(worktree / ".tine" / "objects", (0, 0))
    before = source.signature()

    repo.put_run(_run("orphan", "no ref at all"))

    assert source.signature() != before


def test_a_ref_lock_file_does_not_count_as_a_repository_change(tmp_path: Path) -> None:
    # A writer holding `heads/main.lock` would otherwise make the signature
    # differ on every tick, so the console would rescan the repository forever.
    worktree = tmp_path / "project"
    Repo.init(worktree).put_run(_run("first"), ref="heads/main")
    source = open_source(worktree)
    before = source.signature()

    (worktree / ".tine" / "refs" / "heads" / "main.lock").write_text("held")

    assert source.signature() == before


def test_a_shallow_clone_is_scanned_with_a_stated_warning(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    oid = repo.put_run(_run("tip"), ref="heads/main").run_id
    # The on-disk shallow boundary, in the format opentine's own reader parses:
    # a real shallow clone comes from a depth-limited fetch, which needs a peer.
    # newline="\n" because opentine's reader splits on a bare newline, and on
    # Windows the default translation would write "\r\n" and leave every oid
    # with a trailing carriage return that matches nothing.
    (worktree / ".tine" / "shallow").write_text(f"{oid}\n", newline="\n")

    snapshot = open_source(worktree).scan()

    assert snapshot.shallow is True
    assert "shallow" in snapshot.note
    assert [entry.id for entry in snapshot.entries] == ["tip"]


def test_the_listing_is_capped_and_says_that_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A repository read is one to two orders of magnitude dearer per item than
    # reading a flat file, and the auto refresh repeats it; the ceiling must be
    # visible rather than silently truncating someone's history.
    monkeypatch.setattr(sources, "MAX_REPO_RUNS", 2)
    repo = Repo.init(tmp_path / "project")
    for n in range(4):
        repo.put_run(_run(f"run-{n}"), ref=f"heads/r{n}")

    snapshot = open_source(tmp_path / "project").scan()

    assert len(snapshot.entries) == 2
    assert any("capped at 2 runs" in error for error in snapshot.errors)


# --- repositories that are broken -------------------------------------------


def test_a_directory_that_is_not_a_repository_is_an_error_row(tmp_path: Path) -> None:
    snapshot = RepositorySource(tmp_path).scan()

    assert snapshot.entries == []
    assert snapshot.writable is False
    assert any("not a readable v3 repository" in error for error in snapshot.errors)


def test_a_corrupt_object_is_an_error_row_and_the_other_runs_still_load(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    doomed = repo.put_run(_run("doomed", "corrupt me"), ref="heads/main").run_id
    repo.put_run(_run("healthy", "leave me alone"), ref="heads/other")
    digest = doomed.rpartition(":")[2]
    object_path = worktree / ".tine" / "objects" / "run" / digest[:2] / digest[2:]
    object_path.write_bytes(b"not an envelope")

    snapshot = open_source(worktree).scan()

    assert [entry.id for entry in snapshot.entries] == ["healthy"]
    assert any(_short_oid(doomed) in error for error in snapshot.errors)


def test_a_dangling_ref_does_not_stop_the_scan(tmp_path: Path) -> None:
    # A ref left behind by a failed fetch points at an object that is not
    # there. The runs that *are* there still have to list.
    worktree = tmp_path / "project"
    Repo.init(worktree).put_run(_run("live"), ref="heads/main")
    # newline="\n" for the same reason the shallow file uses it: a ref is a line
    # of text opentine parses, and only a POSIX host writes "\n" by default.
    (worktree / ".tine" / "refs" / "heads" / "ghost").write_text(
        f"run:sha256:{'aa' * 32}\n", newline="\n"
    )

    snapshot = open_source(worktree).scan()

    assert [entry.id for entry in snapshot.entries] == ["live"]
    assert "heads/ghost" in snapshot.refs
    # Nothing claims the ghost, so no row pretends the object was read.
    assert all(entry.refs != ("heads/ghost",) for entry in snapshot.entries)


# Regression guard. Before this was fixed:
# RepositorySource.scan() reports nothing for a ref whose object is missing. The
    # refs panel renders `heads/ghost -> run:aaaaaaaaaaaa` with no run row and no
    # warning, which reads as a run the console failed to draw rather than as a broken
    # repository.
def test_a_dangling_ref_is_called_out(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    Repo.init(worktree).put_run(_run("live"), ref="heads/main")
    # newline="\n" for the same reason the shallow file uses it: a ref is a line
    # of text opentine parses, and only a POSIX host writes "\n" by default.
    (worktree / ".tine" / "refs" / "heads" / "ghost").write_text(
        f"run:sha256:{'aa' * 32}\n", newline="\n"
    )

    snapshot = open_source(worktree).scan()

    assert any("ghost" in error for error in snapshot.errors)


# Regression guard. Before this was fixed:
# reset_caches() clears _RUN_CACHE and _VERIFY_CACHE but not _REPO_RUN_CACHE, so
    # 'start from cold' is not cold for a repository: a run object already read is
    # served from memory however the store changed underneath.
def test_resetting_the_caches_re_reads_a_repository_object(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    repo = Repo.init(worktree)
    oid = repo.put_run(_run("doomed", "read me, then break me"), ref="heads/main").run_id
    source = open_source(worktree)
    assert [entry.id for entry in source.scan().entries] == ["doomed"]

    digest = oid.rpartition(":")[2]
    (worktree / ".tine" / "objects" / "run" / digest[:2] / digest[2:]).write_bytes(b"broken")
    sources.reset_caches()

    snapshot = source.scan()

    assert snapshot.entries == []
    assert any(_short_oid(oid) in error for error in snapshot.errors)


def test_an_unreadable_ref_is_reported_and_the_runs_still_load(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    Repo.init(worktree).put_run(_run("live"), ref="heads/main")
    (worktree / ".tine" / "refs" / "heads" / "weird").write_text("!!! not an object id")

    snapshot = open_source(worktree).scan()

    assert [entry.id for entry in snapshot.entries] == ["live"]
    assert any("refs unreadable" in error for error in snapshot.errors)


@posix_permissions
def test_an_unreadable_repository_is_an_error_row_not_an_exception(tmp_path: Path) -> None:
    worktree = tmp_path / "project"
    Repo.init(worktree).put_run(_run("live"), ref="heads/main")
    objects = worktree / ".tine" / "objects"
    objects.chmod(0o000)
    try:
        snapshot = open_source(worktree).scan()
    finally:
        objects.chmod(0o755)

    assert snapshot.entries == []
    assert snapshot.writable is False
    assert snapshot.errors
    assert all("\n" not in error for error in snapshot.errors)


# --- rendering an object id -------------------------------------------------


@pytest.mark.parametrize(
    ("oid", "expected"),
    [
        (
            "run:sha256:24a7f681b2214c0178861ddce142bd3e3e93ecab5c9971355192053f4c309a1d",
            "run:24a7f681b221",
        ),
        (
            "annotation:sha256:04e7a4341a83a23d879c92920780f177805d7837a873a",
            "annotation:04e7a4341a83",
        ),
        # No algorithm segment: still shortened, still prefixed by the type.
        ("run:deadbeefcafebabe0123456789", "run:deadbeefcafe"),
        # Not an object id at all — a truncation, not a crash.
        ("nonsense", "nonsense"),
        ("x" * 40, "x" * 17 + "..."),
    ],
)
def test_an_object_id_renders_the_way_the_opentine_cli_renders_it(oid: str, expected: str) -> None:
    assert _short_oid(oid) == expected


# --- path safety ------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../escape",
        "../../etc/passwd",
        "a/../../b",
        "sub/dir",
        "..\\windows",
        "/etc/passwd",
        "",
        " ",
        "a\x00b",
        ".hidden",
        "x" * 200,
        "id with spaces",
        "id;with;semicolons",
    ],
)
def test_a_hostile_run_id_never_becomes_a_path(tmp_path: Path, hostile: str) -> None:
    # Run ids come out of artifacts, and every write action derives its target
    # file from one. A traversal here writes outside the runs directory.
    with pytest.raises(ValueError):
        _safe_run_path(tmp_path, hostile)


def test_a_derived_file_is_validated_exactly_like_the_run_file(tmp_path: Path) -> None:
    assert _safe_sibling(tmp_path, "abc", ".md") == tmp_path / "abc.md"
    assert _export_path(tmp_path, "abc") == tmp_path / "abc.otel.json"
    # An export used to be the way around the run-id check: it builds its own
    # filename, so it has to reuse the same validation.
    for hostile in ("../escape", "a\x00b", "/etc/passwd", ""):
        with pytest.raises(ValueError):
            _safe_sibling(tmp_path, hostile, ".md")
        with pytest.raises(ValueError):
            _export_path(tmp_path, hostile)


def test_an_id_that_already_has_a_suffix_keeps_it_in_the_derived_name(tmp_path: Path) -> None:
    # `.with_suffix` replaces only the last component, so "run.v2" must not
    # become "run.otel.json" and collide with a different run.
    assert _export_path(tmp_path, "run.v2").name == "run.v2.otel.json"


def test_a_symlinked_runs_directory_resolves_to_the_directory_it_points_at(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    # real.resolve(), because the check resolves: on a machine whose temporary
    # directory is itself a symlink (macOS /var) the unresolved path differs.
    assert _safe_run_path(link, "abc") == real.resolve() / "abc.tine"


def test_a_symlink_inside_the_runs_directory_cannot_aim_a_write_outside(tmp_path: Path) -> None:
    # The check resolves the target, so an artifact named after a planted
    # symlink cannot make Pause overwrite a file elsewhere on the machine.
    runs = tmp_path / "runs"
    runs.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not clobber me")
    (runs / "trap.tine").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes runs dir"):
        _safe_run_path(runs, "trap")


def test_a_windows_device_name_is_refused_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "CON.tine" opens the console device rather than a file, whatever the
    # extension. Elsewhere it is an ordinary name and refusing it would make a
    # portable artifact unreadable — so both halves are asserted, each with the
    # platform forced, rather than one of them depending on the host the suite
    # happens to run on.
    #
    # sources.sys *is* the stdlib module, so these patch sys.platform for the
    # process; monkeypatch puts it back at the end of the test.
    monkeypatch.setattr(sources.sys, "platform", "linux")
    assert _safe_run_path(tmp_path, "CON").name == "CON.tine"

    monkeypatch.setattr(sources.sys, "platform", "win32")
    with pytest.raises(ValueError, match="Windows filename"):
        _safe_run_path(tmp_path, "CON")
    with pytest.raises(ValueError, match="Windows filename"):
        _export_path(tmp_path, "LPT1.log")
