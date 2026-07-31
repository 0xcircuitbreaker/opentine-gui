# Production Readiness Audit - 2026-06-25

Audited the GUI against the **released open-source opentine 0.1.1** (PyPI; `.tine`
`format_version == 1`), incorporating opentine PR #2 ("Release audit fixes + 0.1.1",
merged and tested). The app's *runtime accessors* already matched the real
`Run`/`Step` API, but the **data fixtures and tests were written against an API and
file format that opentine never shipped**, so the product was broken end-to-end on a
clean install.

## Findings (all fixed)

1. **Bundled `.tine` fixtures used an obsolete layout** — flat top-level `steps` list,
   singular `parent_id`, `type` discriminators, no `format_version`/`graph`/integrity.
   Released `Run.load()` rejected all five (`Unsupported .tine format_version='missing';
   expected 1`), so the GUI showed zero runs and five load errors out of the box.
2. **`demo/seed.py` crashed** under real opentine: `Step(parent_id=…)` and `Run(steps=…)`
   are not valid constructors (`Step.parent_ids: list[str]`; `Run.__init__` rejects
   unknown kwargs). Rewritten to build `Graph`/`Step`/`Run` correctly and save valid,
   integrity-checked artifacts. Fixtures regenerated.
3. **Test suite (18/34 failing)** shared the same invalid constructors, plus mutated the
   read-only `Run.id` property and appended to the read-only `Run.steps` view. Rewritten;
   suite is now 36/36 green against opentine 0.1.1.
4. **Multi-parent graphs under-rendered.** opentine steps carry `parent_ids` (a list);
   `Step.parent_id` returns only the *last* parent. The DAG, depth, and graph-stats logic
   used the singular accessor, so merge nodes silently dropped edges. Now every parent is
   drawn and counted.
5. **Step content didn't match opentine conventions.** Error messages live in `step.error`,
   tool names in `step.tool_info`, and `done` text often in `inputs.text`. Node labels, the
   step inspector, and search now read those fields.

## Verification

- `pytest`: 36 passed. `ruff check`: clean.
- All five regenerated `.tine_runs/*.tine` fixtures load and pass `Run.verify_integrity`.
- Full GUI launched on a real display: themes, node editor, tables, run/step selection, and
  the DAG built and rendered without error.
- Added `.github/workflows/ci.yml` (ruff + headless pytest + fixture-load proof on 3.11/3.12)
  and a `CHANGELOG.md`. Dependency pinned to `opentine >= 0.1.1`; `uv.lock` refreshed.

---

# GUI Audit - 2026-04-29

## Smoke Results

- Existing headless suite passes: `29 passed`.
- Local Ollama service is reachable at `http://localhost:11434`.
- Verified installed model tag: `qwen3.6:27b`.
- Created a real opentine run with `ollama/qwen3.6:27b`; it completed with one `done` step and was saved under `.tmp_gui_qa_runs/`.
- Launched `tine-gui .tmp_gui_qa_runs` for a native startup smoke; the process stayed running and was closed cleanly.

## Current Product Shape

The app is currently a native Dear PyGui run inspector:

- It loads `.tine` files from a runs directory.
- It lists runs with status and cost.
- It shows run metadata and selected-step details.
- It renders a step DAG with node colors and parent-child links.
- It supports search, refresh, pause, resume, fork, and runs-directory switching.

That is a useful base, but it is not yet close to a Karpathy LLM Wiki-style GUI. It does not yet create runs, stream live agent progress, organize knowledge/workspaces, or make large execution graphs easy to navigate.

## Tolaria Reference Direction

Tolaria's UI quality comes from a disciplined product system rather than any single decorative trick:

- Quiet semantic surfaces: app, sidebar, panel, card, input, and popover colors are tokenized.
- Compact information architecture: fixed-width side panels, a strong center workspace, and a right inspector.
- Small, consistent typography: 11-13 px metadata and controls, with restrained title treatment.
- Subtle interaction states: hover, selected, active, borders, and focus rings are present but not loud.
- Keyboard-first workflows: command palette, quick open, search, and compact icon controls are central.
- AI is a first-class panel, not a bolt-on: contextual chat has its own header, context bar, history, and composer.

The first opentine pass now borrows that direction inside Dear PyGui's native constraints: semantic dark palette, compact panel headers, bordered work surfaces, quieter buttons, and a cleaner run/detail/DAG workspace.

## Main Gaps

- Active run control is shallow: pause/resume currently writes status to disk, but the opentine runtime does not appear to stream/save step-by-step or check that status while a model run is executing.
- No in-GUI run launcher: the user cannot pick `ollama/qwen3.6:27b`, enter a prompt, run an agent, and watch the trace fill in from inside the GUI.
- No automated GUI coverage: tests cover loading and business logic, but not Dear PyGui rendering, clicks, dialogs, node selection, or layout regressions.
- DAG navigation is basic: large runs need graph search, clustering, pan-to-step, collapse/expand, breadcrumbs, and better detail affordances.
- Step details are plain text: tool arguments, JSON outputs, model text, errors, and diffs need dedicated viewers.
- No persistence layer for user preferences such as last runs directory, window size, filter text, and panel widths.
- No wiki/workspace model yet: there are no concepts for notebooks, pages, source documents, references, saved answers, semantic search, or reusable run collections.

## Recommended Next Milestones

1. Stabilize the inspector.
   Add a repeatable GUI smoke harness, robust DAG layout for larger or out-of-order traces, better error surfacing, and preference persistence.

2. Add a local run launcher.
   Build a left/top control surface for model selection, prompt entry, tool enablement, max steps, and run start/stop. Start with Ollama and default to `qwen3.6:27b` when present.

3. Make runs live.
   Update the opentine execution path so it saves incremental steps while running and honors pause/stop state. The GUI can then auto-refresh into a true live trace.

4. Upgrade the DAG into a workbench.
   Add graph search, node filtering, collapse/expand, pan-to-selected, grouped tool/model spans, step timeline, and split-pane output viewers.

5. Move toward LLM Wiki.
   Add persistent workspaces, page/source collections, saved summaries, backlinks between runs and artifacts, full-text/semantic search, and replay/fork/diff workflows that feel like browsing a knowledge graph.

# opentine 0.3.0 Re-target Audit - 2026-07-29

Audited against the **local unpushed opentine 0.3.0** (`/home/circuitbreaker/opentine`,
editable-installed; portable `.tine` format v2, v1 auto-migrated on `Run.load`).
Method: 6-dimension multi-agent audit (API contract, Dear PyGui usage, docs-vs-code,
robustness, tests/fixtures, new 0.3.0 surface) with adversarial verification and
executed repros, plus a scripted-GUI screenshot pass on a real display.

## Crashes fixed

1. **Run-switch segfault.** `_clear_dag` deleted DAG nodes (slot 1) while their links
   (slot 0) still referenced them; Dear PyGui crashed natively in `delete_item` on the
   first run switch after a DAG had rendered. Links are now deleted first.
2. **`model_info: null` filter crash.** 0.3.0 validates presence, not type, of step
   `model_info`; `None` reached `"\n".join(...)` in the run/step filters and killed the
   app (startup, if a filter was persisted). All haystack fields are now coerced.
3. **Lone UTF-16 surrogates segfault DPG.** Any displayed string containing a lone
   surrogate escape crashed the process natively. All strings are sanitized
   (`utf-8`/`replace`) before reaching DPG.
4. **Out-of-range `created_at`/`timestamp` crash.** Epoch-nanosecond-scale values pass
   0.3.0 validation but blew up `time.localtime`; `_format_timestamp` now falls back.
5. **Main-loop hardening.** `_auto_refresh_tick` is wrapped so one bad `.tine` reports
   to the status bar instead of terminating the console.
6. **Second `run()` in one process.** Cached theme ids died with the DPG context;
   caches are now reset per `run()`.

## Action-path correctness

- Pause/resume/fork now target the **file each run was loaded from** (`load_runs`
  returns a run-id -> path map), not `<run.id>.tine` — renamed/shared files are no
  longer duplicated, and resume no longer raises uncaught `FileNotFoundError`.
- **Pause reloads disk state first**: pausing from the GUI's (up to one refresh
  interval stale) snapshot truncated steps a live agent had written. Fork likewise
  reloads and verifies the fork step still exists. Remaining TOCTOU with a concurrent
  writer needs cooperative locking upstream.
- All three actions surface `OSError`/`ValueError` in the status bar.
- `SAFE_ID` uses `fullmatch` (trailing-newline ids no longer slip through).

## Contract / docs / fixtures

- Dependency floor `opentine>=0.3.0` with a local `tool.uv.sources` override until
  0.3.0 is published; `uv.lock` refreshed. CI's fixture check needs published 0.3.0.
- Fixtures regenerated as v2 (`demo/seed.py` docstring fixed; fork fixture now uses the
  real `{"forked_from", "fork_point"}` shape; steps carry `usage`/`billing`).
  v1 migration covered via `tests/fixtures/legacy_v1.tine`.
- Integrity is now actually checked: digest mismatches land in the error panel and the
  run inspector shows `Integrity: ok/FAILED` (metadata is outside digest coverage by
  design upstream).
- Run search covers run metadata, system prompt, and tags; fork origin shows the fork
  point.

## UI/UX (from the screenshot pass)

- Status bar was rendered **below the visible window** at every size (all
  `_set_status` feedback invisible); panels now reserve the bottom strip.
- Enabled/disabled affordance was **inverted** for the action buttons (ghost theme bg
  matched the panel; DPG 2.x ignores `enabled=` for click-blocking on buttons).
  Enabled buttons are now visible, disabled ones dim, and a disabled wrapper group
  actually blocks clicks; menu items gray out via a disabled-state theme.
- DAG nodes overlapped (240px column pitch vs ~360px nodes from 40-char labels) and
  the step-filter "Highlight" did nothing visually. Labels are tighter, pitch is
  250/170, and matches are starred + brightened, surviving refresh and re-select.
- Fixed 340/480 panel widths crushed the DAG at small sizes; panels and text wraps now
  scale with the viewport (min 960x600).
- Stale step inspector after fork; long (64-char) fork ids overflowing the run table;
  clipped filter hint; empty startup DAG summary — all fixed.

## New 0.3.0 surface exposed

Run inspector: tokens, tags, refs/branches, format + migration provenance, integrity,
system prompt, fork point. Step inspector: timestamp, usage, billing. Node fallback
labels use `Step.short_id` (matches CLI).

Deferred (recommended next): run diff (`Run.diff`), transcript pane
(`Run.transcript`), budget/breach display (`Run.budget()`), signature status
(`Run.verify_signature`), tag editing, branch-aware fork dialog.

## Verification

- `pytest`: 47 passed (new coverage: v1 migration, future-format error, integrity
  mismatch warning, null `model_info` filters, duplicate-id path binding, fork
  lineage/loadability, source-path pause/resume, stale-snapshot protection,
  timestamp fallback, surrogate sanitization). `ruff check`: clean.
- All five regenerated v2 fixtures load and pass `Run.verify_integrity`.
- 16-scenario scripted GUI drive re-captured clean after fixes.

# opentine round-10 + Cross-platform Audit - 2026-07-30

Audited against the local opentine 0.3.0 working tree **including its uncommitted
"round 10" release-audit changes** (`_unicode_text.py`, `_canon_redact.py`,
`_cli_flags.py`, and the `TINE_FORMAT.md` text-validity section). Method: a
5-dimension multi-agent audit (round-10 impact, cross-platform portability, GUI
improvements, regression risk, UI quality) with adversarial verification and
executed repros, then a second adversarial pass over the resulting diff.

## opentine round-10 impact

The GUI needed no API changes — 107 tests and all fixtures pass unchanged against
the round-10 tree. Two behavioural consequences were handled:

- **Lone surrogates are now refused at the reader**, with a ~390-character message
  naming the field path, instead of loading. `load_runs` already surfaces that as a
  per-file error, so such a file degrades to one error row. Each error message is
  now truncated to 160 characters so ten of them cannot bury the run table, and the
  GUI's own `_sanitize` remains for strings that never pass through opentine at all
  (the runs-dir path from argv or preferences).
- **Cost can be a lower bound.** `manifest.pricing.complete == False` means opentine
  could not price every invocation, so `total_cost` understates real spend. All five
  cost render sites (run table, run inspector, run-list summary, budget line, run
  diff) now prefix `>=` and the inspector explains how many invocations were
  unpriced. The check fails open on any manifest shape, since nothing validates it
  and an exception there would blank the run list on the refresh loop.

## Cross-platform (Windows / macOS / Linux)

- **Preferences** move to each platform's own directory (`%APPDATA%`,
  `~/Library/Application Support`, `~/.config`); an explicit `XDG_CONFIG_HOME` still
  wins everywhere, matching opentine's pricing-overlay convention. The pre-0.2
  location is still read, so upgrading loses nothing. Writes are atomic
  (temp + `os.replace`).
- **Windows device names** (`CON`, `NUL`, `COM1`, `AUX`, `CONIN$`, ...) and
  trailing-space ids are refused as filenames on Windows, where they resolve to
  devices rather than files. They remain legal on macOS and Linux. A trailing dot is
  *not* rejected: `<id>.tine` never ends in one.
- **HiDPI**: display scale is detected per platform (per-monitor DPI on Windows,
  `GDK_SCALE`/`QT_SCALE_FACTOR` then `Xft.dpi` on Linux) and applied through `_px()`
  to every layout constant *and* the ImGui style vars — padding, spacing, rounding
  and scrollbars, which would otherwise stay at 100% while text grew. The initial
  and minimum viewport sizes are clamped to the actual screen, so a 200% scale
  cannot open a window larger than the display with an unsatisfiable minimum.
  `OPENTINE_GUI_SCALE` overrides.
- **Fonts**: Dear PyGui's built-in atlas is ASCII-only, so accented text, dashes,
  arrows and symbols in recorded output rendered as `?`. A platform-appropriate
  monospace TTF is loaded with Latin-Extended, punctuation, currency, arrow, math,
  box-drawing and check-mark ranges. CJK needs a CJK face via `OPENTINE_GUI_FONT`;
  Dear PyGui binds one atlas, so there is no automatic per-script fallback.
- **Packaging/CI**: a `gui-scripts` entry point (`tine-gui-w`) avoids the persistent
  console window a console script leaves behind on Windows. CI runs lint and the
  full suite on ubuntu, windows and macos, and verifies fixtures through
  `scripts/verify_fixtures.py` rather than a bash-only heredoc.

## Features added

Compare two runs (`Run.diff`) with common ancestor, per-side step lists and
per-field before/after deltas rendered on single lines; signature state; budget vs
incurred; cost attribution by model and by step kind; clipboard copy of full run and
step ids (they are hashes and are elided on screen).

## Defects found and fixed

1. **Correctly signed files reported as `Signature: INVALID`** — a signed artifact
   with no key available returns `ok=False, state='no-key'`, which the first draft
   rendered as an alarm. Rendering now keys on `state`, and only a real `mismatch`
   or malformed block reads as invalid.
2. **Action explanations were erased by the refresh on the next line** — "no longer
   running", "no longer paused" and "step no longer exists" all set the status bar
   and then called `_refresh()`, which writes its own status. Order swapped.
3. **Duplicate run ids produced a dead row** — two files claiming one id rendered two
   identical rows, both marked selected, with the second unselectable. `load_runs`
   now keeps the newest and reports the shadowed file as a warning.
4. **Run search re-serialised every payload on every keystroke.** Payload
   serialisation now happens once per run instead of once per keystroke; the search
   text is cached per Run and keyed by status (the one field the GUI mutates in
   place). Over 40 runs x 120 steps of chunky payloads the first keystroke still
   costs ~95 ms to build the corpus, and each subsequent one drops to a substring
   scan that does not register on the same measurement.
5. **Verification re-read every file on every refresh.** Integrity and signature
   results are memoised per (path, mtime, size).
6. `TEXT_MUTED` failed WCAG AA on the panel background (3.61:1); raised to 4.92:1.
7. Error panel now separates fatal load failures from warnings (integrity,
   duplicate ids) and counts them in its header, fatal first.
8. **Table and DAG were rebuilt inside the viewport-resize callback.** Dear PyGui
   delivers that callback off the render thread, so creating and deleting hundreds
   of node items from it races the renderer. The callback now only records what
   changed; the main loop applies it between frames.

## Note on the screenshot harness

The scripted-GUI harness aborts intermittently (~1 run in 3) inside
`dpg.output_frame_buffer` with a GIL assertion. Isolated by re-running the same
19-scenario drive with capture disabled: 5/5 clean, versus intermittent aborts with
capture on. This is a defect in Dear PyGui's framebuffer readback, exercised only by
the screenshot tooling — the application never calls it. Re-run the capture if it
aborts.

## Verification

- `pytest`: 121 passed (new: cross-platform config/filename/scaling/font behaviour,
  viewport clamping and minimum-size sanity, the full signature state table, pricing
  manifest shapes, diff formatting, budget, cost attribution, search-cache
  invalidation, duplicate-id dedup, load-problem classification, Xft.dpi parsing).
  `tests/conftest.py` isolates every test from the developer's real preferences file
  and from the host display, so the suite no longer depends on the machine it runs on.
- `ruff check opentine_gui tests demo scripts`: clean.
- `scripts/verify_fixtures.py`: 5 v2 fixtures + the v1 migration fixture.
- 19-scenario scripted GUI drive (adds Unicode content and the compare dialog) at
  three viewport sizes, plus a full pass at a simulated 150% display scale.

## Second verification round

A second adversarial pass over the day's own diff found nine further issues, all fixed:

1. **CI could not install on any runner.** `[tool.uv.sources]` resolves opentine from
   `../opentine`, which no GitHub checkout provides, so all six matrix jobs would have
   died at `uv sync` — including the ubuntu job that used to pass. CI now checks the
   opentine repo out as a sibling; verified by reproducing the runner layout locally
   and running sync, the suite and the fixture verifier from it.
2. **A test asserted the reviewer's own display DPI.** `_detect_ui_scale`'s fallback
   shells out to `xrdb`, so the suite failed on any HiDPI X session. The DPI sources
   are now stubbed, and `tests/conftest.py` also redirects `OPENTINE_GUI_PREFS` so no
   test reads or writes the real user profile.
3. **`OPENTINE_GUI_PREFS` was ignored when the file did not exist yet**, silently
   importing the default profile the user had redirected away from.
4. **DPI awareness was skipped whenever `OPENTINE_GUI_SCALE` was set** — the documented
   escape hatch left Windows bitmap-stretching an already-scaled window. It is now a
   separate call made before any window exists, and prefers per-monitor v2.
5. **The viewport minimum could equal its opening size**, making the window
   unshrinkable — the opposite of what the clamp is for.
6. **macOS ran `system_profiler` on the startup path**: seconds slow, and it reports
   backing pixels while the viewport is sized in points. Dropped; AppKit already keeps
   windows on screen. Linux now prefers the primary output over the virtual bounding box.
7. **`_button_theme` hardcoded unscaled padding**, and an item theme overrides the
   global one, so every action button ignored the display scale.
8. **The four-button action row overflowed the panel** at the supported minimum width,
   clipping the new Diff button; the row now budgets spacing, inspector subtitles
   collapse when narrow, and the Copy buttons size to their labels.
9. **Run ids collapsed to identical `demo-...` rows** in a narrow table. Ids now elide
   in the middle, so `demo…lete`, `demo…ning` and `demo…hild` stay distinguishable.

## Third verification round (the dimension a failed reviewer left uncovered)

The correctness dimension of the second round died on an API error and was re-run
directly. One real regression surfaced, introduced by that round's own caching work:

- **The verification cache could serve a stale `Integrity: ok`.** The key was
  (path, mtime, size), but `os.utime` lets a writer restore mtime after a
  same-length edit — exactly the tampering an integrity check exists to catch.
  Reproduced byte-for-byte: a rewritten file with identical size and mtime kept
  reporting `ok` while `Run.verify_integrity` said otherwise. The key now also
  includes inode and `st_ctime_ns`, which POSIX will not let a writer backdate.
  Verified that the cache still resolves an unchanged file with a single check, so
  the refresh-cost win is intact. Residual, documented in the docstring: on Windows
  `st_ctime` is creation time, so a same-size mtime-restored rewrite there can still
  be served from cache until the file changes again.

Checked and clean: `_elide_middle` boundary behaviour (never exceeds its budget, at
n = 0..len and on non-ASCII); `_split_load_problems` against the real message shapes
opentine round-10 emits (unsupported version, surrogate refusal, oversize and JSON
errors classify as fatal; integrity and duplicate-id as warnings); the Linux
`_screen_size` regex against multi-head `xrandr` output (picks the primary output's
2560x1440 rather than the 5120x1440 virtual span); `_VERIFY_CACHE` bounded growth;
and duplicate-id dedup against selection rebinding (one row per id, so the id-based
rebind in `_refresh` is unambiguous).
