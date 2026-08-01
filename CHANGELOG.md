# Changelog

All notable changes to opentine-gui are documented here.

## [0.2.0] - 2026-07-31

Targets **opentine 0.4.0**. This release re-targets the console across three opentine
releases, adds first-class Windows and macOS support, and fixes several ways the console
could mislead you about a run — or lose one.

**Requires `opentine >= 0.4.0`**, and is tested against 0.5.0. Below 0.4.0, forking the
same step twice derives a single id and the second fork silently overwrites the first.
opentine 0.5.0 is additive — it changes nothing about what is written — so the console
reads 0.4.0 and 0.5.0 artifacts identically.

### Added

- **Compare two runs.** `Diff`, or `Run > Compare with...`, reports the common ancestor,
  the steps unique to each side, and per-field before/after deltas including cost, model
  and token usage. A forked run defaults to comparing against its origin.
- **Fork to a branch, with a reason.** The `Fork` button still forks onto `main` in one
  click; `Run > Fork to branch...` adds a branch name, an optional reason, and a
  reproducible-id option. In opentine 0.4.0 the branch and reason are part of the fork
  id, and the reason is recorded the same way opentine's own MCP fork records it.
- **Windows, macOS and Linux support.** Preferences live in each platform's own config
  directory (`%APPDATA%`, `~/Library/Application Support`, `~/.config`), with
  `XDG_CONFIG_HOME` honoured everywhere and the previous location still read on upgrade.
  Lint and the full suite run on all three systems in CI.
- **HiDPI displays.** The console detects the display scale — per-monitor DPI on Windows,
  `GDK_SCALE`/`QT_SCALE_FACTOR` then `Xft.dpi` on Linux — and scales the whole layout with
  it. `OPENTINE_GUI_SCALE` overrides.
- **Readable non-ASCII output.** Dear PyGui's built-in font is ASCII-only, so accented
  text, dashes, arrows and symbols in recorded agent output rendered as `?`. A system
  monospace font is now loaded per platform; `OPENTINE_GUI_FONT` selects another (set it
  to a CJK face for CJK text).
- **Keyboard navigation.** `Up`/`Down` move through the run list, `Esc` backs out of a
  dialog or filter, `Ctrl+F` focuses search, `Ctrl+C` copies the selected run id and
  `Ctrl+R` forces a reload. Keys stay inert while a text field has focus or a dialog is
  open.
- **More of each run is visible**: token totals, tags, refs and branches, format and
  migration provenance, integrity and signature state, system prompt, cost attribution by
  model and by step kind, budget versus incurred, and full fork lineage. Per step:
  timestamp, token usage and billing. `Copy id` copies the full id, which the table elides.
- Cost is marked `>=` when opentine reports its pricing as incomplete, so a partially
  priced run is never shown as an exact total.

### Changed

- Fork lineage now distinguishes sibling forks. Since 0.4.0 a fork id identifies the fork
  *act*, so two forks of one step share `forked_from` and `fork_point` while being
  different runs; the inspector reports the branch and whether the act was unique or
  reproducible. Pre-0.4.0 forks show just the origin line.
- Run search covers system prompt, tags and run metadata, and is cached per run so typing
  stays responsive on large directories.
- `demo/seed.py` seeds both fork shapes — a legacy lineage-only artifact and a genuine
  0.4.0 fork with a recorded, verifiable basis.

### Fixed

- **Forking could silently destroy an earlier fork.** A reproducible fork derives the same
  id every time, so a second one resolved to the same filename and overwrote the first,
  along with any work done inside it. Forking now refuses to overwrite an existing
  artifact, as opentine's own CLI and MCP fork do.
- **A fork reason was displayed as though it were attested.** opentine leaves
  `metadata.fork_reason` out of its signed metadata keys, and metadata sits outside the
  integrity digest, so the text can be rewritten on a signed, integrity-clean artifact —
  and it rendered directly beneath `Signature: verified`. The inspector now re-derives the
  signed fork-intent digest and labels anything that does not reproduce it as
  `Fork reason (unverified)`. `verify_fork_id` additionally flags a fork record edited
  after the fact.
- **An opentine v3 repository is refused rather than half-opened.** A repository's own
  `.tine/` directory matched the run glob, so pointing the console at a worktree showed
  one run out of many, a false integrity failure, and a `Pause` button that rewrote the
  repository's branch.
- **A budget-killed run says so**, reporting the breached dimension and the overage
  instead of a bare `failed`.
- **Crashes on third-party artifacts.** A null `model_info`, a lone UTF-16 surrogate in
  any displayed string, an out-of-range timestamp, or a deep step graph could terminate
  the console; a corrupt file now becomes one row in the error panel. Duplicate run ids
  are reported instead of producing an unselectable row.
- **Actions write back to the file the run was loaded from**, so a renamed or shared
  artifact is no longer duplicated. `Pause` and `Resume` reload from disk first, so a
  stale view cannot truncate steps a running agent has since written.
- **Rendering races.** Dear PyGui dispatches callbacks on a separate thread; selecting a
  run, typing in the graph filter or confirming a fork rebuilt hundreds of graph items
  mid-frame. Callbacks now run between frames. Switching runs also no longer crashes the
  node editor.
- Integrity and signature results are cached per file revision, keyed so that a tampered
  file which restores its timestamp is still caught.
- Preferences are written atomically, so a crash mid-write cannot truncate them.
- Run ids that are Windows device names (`CON`, `NUL`, `COM1`, ...) are refused as
  filenames on Windows, where they resolve to devices rather than files.
- Layout fixes: the status bar is visible, disabled actions look and behave disabled,
  graph nodes no longer overlap, the graph search actually highlights its matches, and
  panels scale with the window instead of clipping.

## [0.1.0] - 2026-06-25

First production-ready release. Audited and aligned against the released
open-source [opentine 0.1.1](https://pypi.org/project/opentine/) (`.tine`
`format_version == 1`).

### Fixed
- **Demo fixtures and seed script were written against a non-existent opentine
  API and an obsolete `.tine` layout.** Under released opentine, `Run.load()`
  rejected every bundled `.tine` file (`Unsupported .tine format_version=…`) and
  `demo/seed.py` crashed (`Step.__init__() got an unexpected keyword 'parent_id'`,
  `Run(steps=…)`). The seed now builds runs via the real `Run`/`Graph`/`Step`
  API and emits valid, integrity-checked `format_version == 1` artifacts;
  `.tine_runs/` fixtures were regenerated.
- **Test suite used the same invalid constructors** (`Step(parent_id=…)`,
  `Run(steps=…)`, mutating the read-only `Run.id`, appending to the read-only
  `Run.steps` view). Rewritten against the real API.

### Changed
- **DAG renders full ancestry.** Steps with multiple `parent_ids` (graph merges)
  now draw a link from every parent; depth and graph stats account for all
  parents instead of only the last one.
- **Step rendering matches opentine conventions.** Error steps surface
  `step.error` (type/message), tool steps surface `step.tool_info`, and `done`
  steps fall back to `inputs.text`. The step inspector shows dedicated Tool and
  Error sections.
- **Search covers tool names and error text** (`tool_info`/`error` added to run
  and step filter haystacks).
- Pinned `opentine >= 0.1.1`; refreshed `uv.lock`.

### Removed
- Dead `_on_run_click` handler (the run table uses button callbacks).
