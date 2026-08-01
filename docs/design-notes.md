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

- **Callbacks run on a separate thread.** All of them, not just resize. Creating
  and deleting node-editor items from a callback races the renderer and crashes
  natively. The app sets `manual_callback_management=True` and drains the queue
  itself between frames, so every callback runs on the render thread.
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

## What this console deliberately does not do

It is a read-mostly viewer. `Agent`, `Model`, `Recorder` and the sandbox policies
are about *executing* agents and are out of scope. `RunIndex` is not adopted
because `search()` writes an index file into the user's runs directory and its
indexed text is a lossy subset that drops error messages. Repository (`.tine/`
v3) support is refused rather than half-implemented: the console detects a
repository and says so, because loading one would show a single run out of many
and `Pause` would rewrite its branch.

Known gaps, roughly in value order: `Run.transcript` (a linear conversational
view), tag editing (which must avoid `Run.save()`, since that destroys the
artifact's signature), and adopting opentine's query grammar for the filter box.
