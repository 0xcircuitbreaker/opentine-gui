"""Where runs come from, and what may be written back to it.

The console reads two shapes of provenance store:

* a **directory of loose `.tine` artifacts** — the legacy portable format, which
  the console may also write to (pause, resume, fork, tag);
* an **opentine v3 repository** — a content-addressed object store with refs,
  which the console reads and never writes. `Run.save` into a repository would
  append an object and move `heads/main`, so a stray Pause click there is a
  branch move; the write actions stay disabled instead.

Both are presented to the app as a `Snapshot` of `RunEntry` rows, so the panels
above do not have to know which one they are looking at.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentine.core import Run

from opentine_gui.text import _oneline, _truncate

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")

#: Skip `.tine` files larger than this rather than decoding them. The auto
#: refresh re-reads the directory, so one enormous file would otherwise cost
#: that much CPU and memory on every tick.
MAX_TINE_BYTES = 10 * 1024 * 1024

#: How many run objects a repository listing will decode in one scan. A
#: repository read is one to two orders of magnitude dearer per item than
#: reading a flat file, so the ceiling is stated rather than discovered.
MAX_REPO_RUNS = 500

# Win32 device names. They resolve as devices whatever the extension or case, so
# "CON.tine" opens the console rather than a file.
WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _windows_unsafe_name(run_id: str) -> bool:
    """Whether <run_id>.tine is unusable as a filename on Windows.

    Pure and platform-independent so it can be tested anywhere; only enforced
    when actually running on Windows, since these names are valid elsewhere.
    """
    stem = run_id.split(".", 1)[0].upper()
    return stem in WINDOWS_RESERVED_STEMS or run_id.endswith(" ")


def _safe_run_path(runs_dir: Path, run_id: str) -> Path:
    """Return runs_dir/<id>.tine iff run_id is safe and resolves inside runs_dir."""
    if not SAFE_ID.fullmatch(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    if sys.platform == "win32" and _windows_unsafe_name(run_id):
        raise ValueError(f"run id is not a usable Windows filename: {run_id!r}")
    base = runs_dir.resolve()
    target = (base / f"{run_id}.tine").resolve()
    if base not in target.parents:
        raise ValueError(f"path escapes runs dir: {target}")
    return target


def _safe_sibling(runs_dir: Path, run_id: str, suffix: str) -> Path:
    """`<runs_dir>/<id><suffix>`, validated the same way a run path is.

    Reuses the run-id validation the write actions use, so an artifact cannot
    steer a derived file (an export, a report) outside the directory.
    """
    return _safe_run_path(runs_dir, run_id).with_suffix(suffix)


def _export_path(runs_dir: Path, run_id: str) -> Path:
    """Where an exported run lands: <runs_dir>/<id>.otel.json, id-safe."""
    return _safe_sibling(runs_dir, run_id, ".otel.json")


#: Digest and signature checks re-read the whole file, so results are memoised
#: against (path, mtime, size): an unchanged file is verified once, not on every
#: refresh of a directory a live agent keeps touching.
_VERIFY_CACHE: dict[tuple, dict[str, object]] = {}
_VERIFY_CACHE_MAX = 512


def _cache_key(path: Path, stat_result, kind: str) -> tuple:
    """Identity of one file *revision*, for a cache that must catch tampering.

    Inode and change time are part of it, not just size and mtime: an integrity
    check exists to catch tampering, and `os.utime` lets a writer restore mtime
    after a same-length edit. A filesystem that keeps an independent change time
    the writer cannot set — ext4, APFS, NTFS — therefore misses the cache on any
    rewrite.

    That is a property of the filesystem, not of POSIX. On Windows `st_ctime` is
    creation time, and on FAT/exFAT, CIFS and several FUSE mounts there is no
    independent change time at all (Linux reports `st_ctime == st_mtime`) — and
    removable or network media is exactly how an artifact someone sent you
    arrives. There a same-size, mtime-restored rewrite is served from cache
    until the file changes again. See SECURITY.md.
    """
    return (
        str(path),
        stat_result.st_mtime_ns,
        getattr(stat_result, "st_ctime_ns", 0),
        stat_result.st_size,
        stat_result.st_ino,
        kind,
    )


def _remember(cache: dict, key: tuple, value: Any, limit: int) -> Any:
    """Insert with a bounded, oldest-first eviction rather than a full clear.

    A full clear at the limit makes a directory holding more revisions than the
    cap re-hash everything on every pass — the worst case is exactly the busy
    directory the cache exists for. Insertion order is the eviction order, which
    for these caches is also arrival order.
    """
    if len(cache) >= limit:
        for stale in list(cache.copy())[: max(1, limit // 4)]:
            cache.pop(stale, None)
    cache[key] = value
    return value


def _verify_cached(path: Path, stat_result, kind: str, check) -> dict[str, object]:
    """Cached IntegrityResult/SignatureResult fields for one file revision."""
    key = _cache_key(path, stat_result, kind)
    hit = _VERIFY_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        result = check(path)
    except Exception as e:
        verdict: dict[str, object] = {"ok": False, "state": "error", "reason": f"check failed: {e}"}
    else:
        verdict = {
            "ok": bool(getattr(result, "ok", False)),
            "state": str(getattr(result, "state", "") or ""),
            "reason": str(getattr(result, "reason", "") or ""),
            "draft": bool(getattr(result, "draft", False)),
            "signer": getattr(result, "signer", None),
            "algorithm": getattr(result, "algorithm", None),
            "key_id": getattr(result, "key_id", None),
        }
    return _remember(_VERIFY_CACHE, key, verdict, _VERIFY_CACHE_MAX)


def _verify_integrity_cached(path: Path, stat_result) -> dict[str, object]:
    return _verify_cached(path, stat_result, "integrity", Run.verify_integrity)


def _signature_verdict(
    path: Path,
    stat_result,
    verifier: Callable | None = None,
    fingerprint: str = "",
) -> dict:
    """Signature state for a file, through whatever key material is configured.

    `verifier` is the key-bearing closure the trust settings hand down, and
    `fingerprint` names *which* key material that is. The fingerprint is part of
    the cache key, so configuring a key re-verifies every file instead of
    serving the keyless answer computed before it.
    """
    if verifier is None:
        return _verify_cached(path, stat_result, "signature", Run.verify_signature)
    return _verify_cached(path, stat_result, f"signature:{fingerprint}", verifier)


#: Step keys this build's `Step` can hold. `causal_ids` arrived in opentine
#: 0.7.1 and `provider` in 0.8.0, so the set grows with the installed library —
#: which is the point: what it cannot name, it cannot round-trip.
_KNOWN_STEP_FIELDS = frozenset(getattr(Run, "__dataclass_fields__", {})) | frozenset(
    getattr(__import__("opentine.core", fromlist=["Step"]).Step, "__dataclass_fields__", {})
)


def _fields_a_save_would_drop(path: Path) -> tuple[str, ...]:
    """Step keys in this file that the installed opentine cannot read back.

    opentine adds fields to a step within format v2 — `causal_ids`, then
    `provider` — and its reader ignores keys it does not know. So an older
    library loads a newer artifact happily, drops the field in memory, and
    *destroys it on disk* the moment this console saves the run: pause, resume
    or fork. That is the failure this console exists to prevent, so the write
    actions ask first, naming what would go.

    Cheap enough to run per write (writes are rare) and fail-open: an artifact
    this cannot parse is one the write path will fail on anyway.
    """
    try:
        if path.stat().st_size > MAX_TINE_BYTES:
            return ()
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = data["graph"]["steps"]
        records = steps.values() if isinstance(steps, dict) else steps
        unknown: set[str] = set()
        for record in records:
            if isinstance(record, dict):
                unknown |= set(record) - _KNOWN_STEP_FIELDS
        return tuple(sorted(unknown))
    except Exception:
        return ()


def _is_v3_repository(path: Path) -> bool:
    """Whether path is an opentine v3 repository, by the library's own rule.

    Matches both a worktree (<dir>/.tine/config.json) and the object directory
    itself (<dir>/config.json), which is what Run.load keys off when it silently
    redirects a directory to Repo.open(...).load_run('heads/main').
    """
    try:
        return path.is_dir() and (
            (path / "config.json").is_file() or (path / ".tine" / "config.json").is_file()
        )
    except OSError:
        return False


def _repository_dir(path: Path) -> Path:
    """The `.tine` object directory for a worktree, or the directory itself."""
    return path / ".tine" if (path / ".tine" / "config.json").is_file() else path


#: Parsed runs, keyed by file revision. A `.tine` file is immutable for as long
#: as its (path, mtime, size, inode) tuple is, and a v3 object is immutable
#: forever, so a hit is a genuine hit rather than a staleness bet. Actions never
#: read through this cache — they re-load from disk on purpose.
_RUN_CACHE: dict[tuple, Run] = {}
_RUN_CACHE_MAX = 256


def _load_run_cached(path: Path, stat_result) -> Run:
    key = _cache_key(path, stat_result, "run")
    hit = _RUN_CACHE.get(key)
    if hit is not None:
        return hit
    return _remember(_RUN_CACHE, key, Run.load(path), _RUN_CACHE_MAX)


def _forget_run(path: Path) -> None:
    """Drop every cached revision of one file, after the console wrote to it.

    Over a copy: the loader thread inserts into this cache while a write action
    on the render thread is dropping entries from it, and iterating the live
    dict raises "changed size during iteration" at exactly that moment.
    """
    prefix = str(path)
    for key in [k for k in _RUN_CACHE.copy() if k[0] == prefix]:
        _RUN_CACHE.pop(key, None)


def reset_caches() -> None:
    """Used by tests, and by a runs-directory change, to start from cold.

    Includes the repository cache: content addressing makes an object id a
    permanent name for its bytes, but "start from cold" has to mean cold, or a
    test that rewrites a store under one id is served the previous answer.
    """
    _RUN_CACHE.clear()
    _VERIFY_CACHE.clear()
    _REPO_RUN_CACHE.clear()


@dataclass
class RunEntry:
    """One row in the run list, and the identity every action targets."""

    #: Unique within a snapshot: the run id for a directory (ids are deduped on
    #: load), the object id for a repository (where two runs may legitimately
    #: share a legacy run id).
    key: str
    run: Run
    #: The file this run was loaded from, and the file a write must land in.
    #: None for a repository run, which this console does not write.
    path: Path | None = None
    #: Where it lives, for the user: a file name, or a ref/object id.
    location: str = ""
    #: v3 refs pointing at this run, if any.
    refs: tuple[str, ...] = ()
    size: int = 0
    mtime: float = 0.0

    @property
    def id(self) -> str:
        return str(self.run.id)


@dataclass
class Snapshot:
    """Everything one scan of a source produced."""

    kind: str = "directory"
    root: Path = field(default_factory=Path)
    entries: list[RunEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    signature: tuple = ()
    #: v3 only: every ref in the repository, and whether the clone is shallow.
    refs: dict[str, str] = field(default_factory=dict)
    shallow: bool = False
    #: False until a scan says otherwise. The placeholder a directory change
    #: installs is not evidence that the new source can be written to, and the
    #: actions read this to decide whether they may write at all.
    writable: bool = False
    note: str = ""

    @property
    def runs(self) -> list[Run]:
        return [entry.run for entry in self.entries]

    def entry(self, key: str) -> RunEntry | None:
        return next((e for e in self.entries if e.key == key), None)


def load_runs(
    runs_dir: Path,
) -> tuple[list[Run], list[str], tuple[tuple[str, float, int], ...], dict[str, Path]]:
    """Return (runs, errors, signature, paths) from one atomic directory scan.

    paths maps run.id to the file it was loaded from (newest mtime wins on
    duplicate ids) so actions write back to the real source file. Oversized
    files are skipped with an error rather than decoded, to bound memory/CPU
    on the auto-refresh loop. Integrity failures load but are surfaced as
    errors — a digest mismatch is a warning, not a parse failure.
    """
    runs: list[Run] = []
    errors: list[str] = []
    sig_entries: list[tuple[str, float, int]] = []
    paths: dict[str, Path] = {}
    try:
        exists = runs_dir.exists()
    except OSError as e:  # an unreadable parent, a dead mount
        return runs, [f"{runs_dir}: {e}"], (), paths
    if not exists:
        # A typo'd path and an empty directory used to look identical ("0 runs").
        return runs, [f"{runs_dir}: no such directory"], (), paths
    if not runs_dir.is_dir():
        return runs, [f"{runs_dir}: not a directory"], (), paths
    if _is_v3_repository(runs_dir):
        # Say so instead of half-opening it: Run.load on a repository directory
        # redirects to heads/main, which would show one run out of many, and
        # Run.save would write a new object and move the branch.
        errors.append(
            f"{runs_dir.name}: this is an opentine v3 repository; this console "
            "reads loose .tine files (use `tine repo-log` for repositories)"
        )
        return runs, errors, (), paths
    files = []
    try:
        candidates = list(runs_dir.glob("*.tine"))
    except OSError as e:
        return runs, [f"{runs_dir}: {e}"], (), paths
    for f in candidates:
        if f.is_dir():
            continue  # a repository's own .tine/ directory, not a run file
        try:
            st = f.stat()
        except OSError as e:
            errors.append(f"{f.name}: {e}")
            continue
        files.append((f, st))
        sig_entries.append((f.name, st.st_mtime, st.st_size))
    files.sort(key=lambda pair: pair[1].st_mtime, reverse=True)
    for f, st in files:
        if st.st_size > MAX_TINE_BYTES:
            errors.append(f"{f.name}: skipped ({st.st_size} bytes > {MAX_TINE_BYTES})")
            continue
        try:
            run = _load_run_cached(f, st)
        except Exception as e:
            errors.append(f"{f.name}: {e}")
            continue
        if run.id in paths:
            # Two files claiming one run id: actions can only target one path, and
            # a second identical row would be an unselectable dead click. Keep the
            # newest (files are mtime-sorted) and say which file is shadowed.
            # Short id: real ids are 64 hex chars, and the panel truncates rows.
            errors.append(
                f"{f.name}: duplicate run id {_truncate(run.id, 14)}, "
                f"shadowed by {paths[run.id].name}"
            )
            continue
        runs.append(run)
        paths[run.id] = f
        verdict = _verify_integrity_cached(f, st)
        if not verdict["ok"]:
            errors.append(f"{f.name}: integrity {verdict['reason']}")
    sig_entries.sort()
    return runs, errors, tuple(sig_entries), paths


def _dir_signature(runs_dir: Path) -> tuple[tuple[str, float, int], ...]:
    """Cheap fingerprint of a runs directory, to skip an unchanged rescan.

    The first element records whether the directory exists at all. Without it a
    missing directory and an empty one share the signature `()`, so a console
    started before the directory was created never notices it appear, and one
    whose directory is deleted while empty never reports that either.
    """
    entries: list[tuple[str, float, int]] = []
    try:
        if not runs_dir.exists():
            return (("", 0.0, 0),)
        entries.append(("", 0.0, 1))
        for f in runs_dir.glob("*.tine"):
            if f.is_dir():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            entries.append((f.name, st.st_mtime, st.st_size))
    except OSError:
        return (("", 0.0, 0),)
    entries.sort()
    return tuple(entries)


class DirectorySource:
    """A directory of loose `.tine` artifacts. Readable and writable."""

    kind = "directory"
    writable = True

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def label(self) -> str:
        return str(self.root)

    def signature(self) -> tuple:
        return _dir_signature(self.root)

    def scan(self) -> Snapshot:
        runs, errors, _files, paths = load_runs(self.root)
        # `load_runs` reports the files it saw; the change gate needs the same
        # fingerprint the loader polls with, or every tick counts as a change and
        # re-parses the whole directory forever.
        signature = self.signature()
        entries: list[RunEntry] = []
        for run in runs:
            path = paths.get(run.id)
            size = mtime = 0
            if path is not None:
                try:
                    st = path.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    pass
            entries.append(
                RunEntry(
                    key=str(run.id),
                    run=run,
                    path=path,
                    location=path.name if path else "",
                    size=size,
                    mtime=mtime,
                )
            )
        return Snapshot(
            kind=self.kind,
            root=self.root,
            entries=entries,
            errors=errors,
            signature=signature,
            writable=True,
        )


class RepositorySource:
    """An opentine v3 repository, read-only.

    Construction goes through `Repo(<.tine dir>)` rather than `Repo.open(path)`
    on purpose: `open` heals the layout, mkdir-ing any missing directory, which
    would leave untracked directories in a repository someone committed to git
    or mounted read-only. This console has no business writing anything there.
    """

    kind = "repository"
    writable = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tine_dir = _repository_dir(root)

    @property
    def label(self) -> str:
        return f"{self.root} (v3 repository)"

    def signature(self) -> tuple:
        """Refs plus object shards: what changes when a repository is written to.

        Every shard directory is stat-ed, not just `objects/`. A new object lands
        in `objects/<type>/<shard>/`, which does not move the mtime of any parent
        once that shard exists, so a coarser fingerprint went blind to every run
        after the first that no ref points at.
        """
        entries: list[tuple[str, float, int]] = []
        try:
            refs_dir = self.tine_dir / "refs"
            for path in sorted(refs_dir.rglob("*")):
                if path.is_file() and not path.name.casefold().endswith(".lock"):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    entries.append((str(path.relative_to(refs_dir)), st.st_mtime, st.st_size))
        except OSError:
            pass
        objects = self.tine_dir / "objects"
        try:
            # Two levels of scandir: object types, then their 256 shards. Bounded
            # and cheap, unlike walking every object file.
            for type_entry in sorted(os.scandir(objects), key=lambda e: e.name):
                if not type_entry.is_dir():
                    continue
                for shard in sorted(os.scandir(type_entry.path), key=lambda e: e.name):
                    try:
                        st = shard.stat()
                    except OSError:
                        continue
                    entries.append((f"{type_entry.name}/{shard.name}", st.st_mtime, st.st_size))
        except OSError:
            pass
        return tuple(entries)

    def scan(self) -> Snapshot:
        errors: list[str] = []
        try:
            from opentine.repo import Repo
        except Exception as e:  # pragma: no cover - opentine always ships it
            return Snapshot(
                kind=self.kind, root=self.root, errors=[f"v3 repositories need opentine: {e}"],
                writable=False,
            )
        try:
            repo = Repo(self.tine_dir)
        except Exception as e:
            return Snapshot(
                kind=self.kind,
                root=self.root,
                errors=[f"{self.root.name}: not a readable v3 repository ({_oneline(e)})"],
                writable=False,
            )
        refs: dict[str, str] = {}
        try:
            refs = repo.list_refs()
        except Exception as e:
            errors.append(f"refs unreadable: {_oneline(e)}")
        shallow = False
        try:
            shallow = bool(repo.shallow_oids())
        except Exception:
            shallow = False
        oids, scan_error = _run_oids(repo)
        if scan_error:
            errors.append(scan_error)
        by_run: dict[str, list[str]] = {}
        for name, oid in refs.items():
            if isinstance(oid, str) and oid.startswith("run:"):
                by_run.setdefault(oid, []).append(name)
        # A ref whose object is missing is a broken repository, not a run the
        # console failed to draw. Said once here rather than left as a row in the
        # refs panel pointing at nothing.
        for name, oid in sorted(refs.items()):
            try:
                if isinstance(oid, str) and not repo.has(oid):
                    errors.append(
                        f"{_oneline(name)}: dangling ref, "
                        f"object {_short_oid(oid)} is missing"
                    )
            except Exception:
                continue
        entries: list[RunEntry] = []
        for oid in oids:
            try:
                run = _load_repo_run(repo, oid)
            except Exception as e:
                errors.append(f"{_short_oid(oid)}: {_oneline(e)}")
                continue
            entries.append(
                RunEntry(
                    key=oid,
                    run=run,
                    path=None,
                    location=_short_oid(oid),
                    refs=tuple(sorted(by_run.get(oid, ()))),
                    mtime=float(getattr(run, "created_at", 0.0) or 0.0),
                )
            )
        entries.sort(key=lambda e: e.mtime, reverse=True)
        note = ""
        if shallow:
            note = "shallow clone: history and diffs are truncated"
        return Snapshot(
            kind=self.kind,
            root=self.root,
            entries=entries,
            errors=errors,
            signature=self.signature(),
            refs=refs,
            shallow=shallow,
            writable=False,
            note=note,
        )


#: Repository runs are content-addressed, so an object id identifies its bytes
#: forever: this cache never goes stale and is never invalidated by a rescan.
_REPO_RUN_CACHE: dict[str, Run] = {}
_REPO_RUN_CACHE_MAX = 256


def _load_repo_run(repo, oid: str) -> Run:
    hit = _REPO_RUN_CACHE.get(oid)
    if hit is not None:
        return hit
    return _remember(_REPO_RUN_CACHE, oid, repo.load_run(oid), _REPO_RUN_CACHE_MAX)


def _run_oids(repo) -> tuple[list[str], str]:
    """Every run object in the store, newest-scan-order, plus an error string."""
    try:
        from opentine.repository._objects import iter_typed_object_oids
    except Exception:
        iter_typed_object_oids = None
    if iter_typed_object_oids is not None:
        try:
            found = []
            for oid in iter_typed_object_oids(repo.path, {"run"}, limit=100_000):
                found.append(oid)
                if len(found) >= MAX_REPO_RUNS:
                    return found, f"listing capped at {MAX_REPO_RUNS} runs"
            return found, ""
        except Exception as e:
            return [], f"object scan failed: {_oneline(e)}"
    try:  # portable fallback: the public iterator, then filter by object type
        oids = [o for o in repo.iter_oids(limit=100_000) if str(o).startswith("run:")]
        if len(oids) > MAX_REPO_RUNS:
            return oids[:MAX_REPO_RUNS], f"listing capped at {MAX_REPO_RUNS} runs"
        return oids, ""
    except Exception as e:
        return [], f"object scan failed: {_oneline(e)}"


def _short_oid(oid: str) -> str:
    """`run:sha256:24a7f6…` rendered the way opentine's own CLI renders it."""
    text = str(oid)
    head, _, digest = text.rpartition(":")
    if head and digest:
        return f"{head.split(':')[0]}:{digest[:12]}"
    return _truncate(text, 20)


def open_source(root: Path) -> DirectorySource | RepositorySource:
    """The right reader for whatever the user pointed the console at."""
    if _is_v3_repository(root):
        return RepositorySource(root)
    return DirectorySource(root)
