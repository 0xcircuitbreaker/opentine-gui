# Design notes

Why this console is built the way it is. Most of what follows was learned by
getting it wrong first — the constraints are not obvious from the APIs, and
several of them will bite anyone who changes the relevant code.

## The console renders artifacts it did not create

A `.tine` file holds recorded model output, tool arguments and prompts. None of
it is under our control, and it may be attacker-influenced if a user opens a run
that was sent to them. Two consequences shape the code:

**Artifact text must never occupy a row of its own.** The run and step inspectors
render as one flat text widget, so a newline inside an artifact-supplied field
opens a row that is pixel-identical to the console's own — including the
`Integrity:`, `Signature:` and `Fork id:` verdicts that describe that very
artifact. Every interpolated value goes through `_oneline()`, and payload blocks
indent every line they emit via `_indent_block()`. The text is still shown;
hiding it would be its own kind of lie. It is just folded into the field it came
from. See `test_artifact_text_cannot_forge_trust_lines`.

**Every field is untrusted, including the ones with types.** opentine validates
that keys are *present*, not that they hold the declared type — a third-party
`.tine` can carry `"model_info": null` and load cleanly. Anything read from an
artifact is coerced at the point of use, and every `metadata` reader fails open
rather than raising, because these run inside the render loop where an exception
blanks the panel.

## What opentine does and does not attest

`metadata` sits **outside** the integrity digest, by design, and opentine's
signed-metadata key set deliberately omits `fork_reason` for backwards
compatibility. So a signed, integrity-clean artifact can still carry an edited
reason or fork record.

The console does not paper over this. It re-derives what it can — a fork reason
is checked against the signed fork *intent* digest, and a fork id against its
recorded basis via `verify_fork_id` — and labels anything that does not reproduce
as unverified. `verify_fork_id` lives in a private opentine module and is
imported defensively: a rename must cost one advisory line, not the app.

## Dear PyGui constraints

These are not documented anywhere obvious and each one caused a real crash:

- **Callbacks run on a separate thread by default.** All of them, not just
  resize. Creating and deleting node-editor items from a callback races the
  renderer and crashes natively. The app sets `manual_callback_management=True`
  and drains the queue itself between frames, so every callback — key handlers
  included — actually runs on the render thread. The viewport resize callback is
  the one Dear PyGui may still deliver on its own thread, so it records intent
  and `_apply_pending_relayout` does the work.
- **Node-editor links live in slot 0, nodes in slot 1.** Deleting a node while a
  link still references it segfaults. `_clear_dag()` deletes links first.
- **The built-in font atlas is ASCII-only.** Accented text, dashes and arrows in
  recorded output render as `?`. A platform-appropriate TTF is loaded with
  explicit glyph ranges. Only one atlas can be bound, so there is no per-script
  fallback; CJK needs `OPENTINE_GUI_FONT`.
- **Lone UTF-16 surrogates crash the native text renderer**, so strings are
  sanitized before they reach any widget. opentine ≥ 0.3 refuses such artifacts
  at load, but paths from `argv` and preferences do not pass through opentine.
- **Item themes override the global theme**, so a scaled style var has to be
  repeated in every item theme or that widget silently ignores the display scale.
- **`dpg.output_frame_buffer` is flaky** — it aborts intermittently under a GIL
  assertion. That affects screenshot tooling only; the app never calls it.

## Scanning happens on a worker thread; drawing never does

Reading a runs directory means parsing every artifact and hashing every file for
its integrity digest, and a v3 repository read costs one to two orders of
magnitude more per item than a flat file. All of that used to happen inside the
frame loop, so the console stalled on every refresh tick and again on every
selection.

`app._Loader` owns a thread that computes a cheap source signature, rescans when
it changes, and puts a finished `Snapshot` on a queue. The render thread drains
that queue between frames. The rule that keeps this safe is absolute: **the
worker never touches Dear PyGui**. It returns plain data — runs, errors, paths —
and every widget call happens on the render thread.

Two caches make repeat scans cheap. Parsed runs are keyed by file revision
(path, mtime, ctime, size, inode), which is the same key the verification cache
uses, so an unchanged file is parsed once. v3 objects are content-addressed, so
their cache never needs invalidating at all. Both evict oldest-first rather than
clearing wholesale: a full clear at the cap makes a directory holding more
revisions than the cap re-do all its work on every pass, which is precisely the
busy directory the cache exists for.

The selected run's graph is rebuilt only when that run's own bytes change.
Rebuilding resets pan, zoom, node positions and node selection, and a directory
where one agent is writing changes its directory-wide signature every couple of
seconds — so the old behaviour threw the user's view away while they were
reading it.

## Two sources, one panel set

`sources.py` presents a directory of `.tine` files and a v3 repository as the
same `Snapshot` of `RunEntry` rows, so the panels do not branch on which is open.
The differences that matter are carried on the snapshot rather than inferred:

- `writable` is False for a repository, and every write action is disabled with a
  reason. `Run.save` into a repository appends an object *and moves
  `heads/main`* — a stray Pause click there is a branch move.
- A repository is attached with `Repo(<.tine dir>)`, never `Repo.open(path)`.
  `open` heals the layout, mkdir-ing any missing directory under `.tine/`, which
  leaves untracked directories in a repository someone committed to git. A
  console that only reads must leave no trace; `tests/test_sources.py` asserts
  the directory tree is byte-identical before and after a scan.
- Entry keys are run ids for a directory (ids are deduped at load) and object ids
  for a repository, where two runs may legitimately share a legacy run id.

## Causal edges are part of the graph, not a footnote

A step's `causal_ids` (opentine 0.7.1) names non-parent ancestors it required.
`Run.fork` keeps the *causal* closure, so a console that drew only `parent_ids`
showed a strict subgraph and then forked something wider than it had shown: the
preview and the result disagreed. They are now a second edge class in the node
editor, a term in the layout depth, a count in the graph summary, and a line in
the step inspector. The fork dialog states the slice size using opentine's own
`retained_closure`, because 0.6.0 extracted that helper precisely to stop a
second, independently computed preview from being wrong.

## Zero is a claim, and the console only makes it when the artifact does

`Run.total_cost` sums what each step recorded at capture. A run imported from
OpenTelemetry or a framework log recorded nothing, so it sums to `0.0` — and
rendering `$0.0000` states a spend the artifact never claimed. The console
distinguishes three cases: priced, partially priced (`>=`, opentine flagged
invocations it could not price), and nothing recorded at all (`no cost
recorded`). `Run > Price this run...` then answers the real question from
opentine's signed catalog, reporting the catalog id and hash beside the figure,
and reporting `unknown` for any step the catalog cannot answer for.

## Layout scales, it is not fixed

Every pixel dimension goes through `_px()`, which multiplies by a display scale
detected once at startup (per-monitor DPI on Windows, `GDK_SCALE`/
`QT_SCALE_FACTOR` then `Xft.dpi` on Linux). That includes the ImGui style vars —
padding and spacing left at 100% while text grows looks broken. The viewport is
clamped to the screen so a 200% display cannot open a window larger than the
monitor, and the minimum stays meaningfully below the opening size or the window
cannot be shrunk at all.

## Actions write to the file a run came from

`load_runs` returns a run-id → path map, and pause/resume/fork write back to that
path rather than `<id>.tine`. A renamed or shared artifact is otherwise
duplicated. Both reload from disk immediately before writing, because the
in-memory view can be a refresh interval stale and would otherwise truncate steps
a live agent has since written. A residual TOCTOU race remains; closing it needs
cooperative locking upstream.

Forking refuses to overwrite an existing artifact. opentine 0.4.0 gives each fork
act a distinct id, but `nonce=""` opts back into a reproducible one — and two
reproducible forks of the same step derive the same filename.

## Performance shapes that matter

A legal run can hold roughly 15,900 steps within the 10 MiB file cap, which makes
anything super-linear in step or depth count a UI freeze:

- DAG band layout buckets depths once instead of rescanning per band (this was
  quadratic: 5–12 s on a large run).
- Run search text is built once per run and cached, keyed on status since that is
  the one field the console mutates in place. Rebuilding it per keystroke cost
  ~95 ms on a large directory.
- Integrity and signature results are cached per file revision. The key includes
  inode and ctime, not just size and mtime, so a tampered file that restores its
  mtime is still caught on POSIX. See `SECURITY.md` for the Windows caveat.

## Saying what a verdict covers

Two of the console's trust rows are narrower than they look, and both are stated
rather than left to be assumed:

- The integrity digest is taken over the artifact body **excluding all of
  `metadata`**, by design — it means "internally consistent", not "genuine". So
  tags, a fork reason, replay records and budget state can all be rewritten with
  the digest still matching.
- A `tine-sig/1` signature (everything opentine 0.3.0–0.7.0 wrote) covers eleven
  metadata keys and deliberately excludes `tags` and `fork_reason`. `tine-sig/2`
  (0.7.1+) covers every metadata key except `integrity`. `SignatureResult`
  carries no scheme, so the console reads it out of the artifact's own JSON.

Trust-on-first-use renders differently from a real verification, because it is a
different claim: it says the file carries a key that matches its own signature,
not that the key is one you trust.

## Writes that cannot be undone from inside the app

`Run.save` recomputes `metadata.integrity` from scratch. That drops any signature
block the file carried and clears the draft marker an autosave checkpoint uses,
and the console holds no signing key, so it cannot put either back. Pause and
Resume therefore state what will be lost and ask first. Fork does not: a fork
writes a *new* artifact, and a fork legitimately has no signature of its own.

## What this console deliberately does not do

It is a read-mostly viewer. `Agent`, `Model`, `Recorder` and the sandbox policies
are about *executing* agents and are out of scope, as is `tine replay --verify`,
which re-executes work and cannot be an incidental click. `RunIndex` is not
adopted because `search()` writes an index file into the user's runs directory
and its indexed text is a lossy subset that drops error messages; the query
grammar is parsed with `parse_query` and evaluated in memory instead.

A v3 repository is read, never written. The v3 mutating verbs — `repo-fork`,
`repo-resume`, `attest`, `evaluate`, `promote` — are deliberate provenance acts
with CAS semantics, and a GUI button is the wrong shape for them.

There is no OTLP/HTTP push: exporting to a file is a local act, while pushing
ships prompts and completions to a network endpoint, and opentine's own refusal
rules for that (loopback or TLS, never a silent drop) belong where the operator
can see them.

Known gaps, roughly in value order: tag editing (which has to avoid `Run.save()`,
since that destroys the artifact's signature), a determinism badge from
`tine replay --verify`, and pan-to-node in the DAG for very large graphs.
