# GUI QA Checklist

This checklist covers the current `opentine-gui` feature contract and the
graph-first expectations used for the Karpathy LLM Wiki-inspired pass.

## Baseline Data

Use a disposable runs directory with:

- `completed`, `running`, `paused`, and `failed` runs.
- `think`, `tool`, `model`, `done`, and `error` steps.
- Linear and branched DAGs.
- A legacy fork carrying only `metadata["forked_from"]`/`fork_point` (the shape
  written before opentine 0.4.0, and still written when a caller forces
  `new_run_id`, which suppresses the fork record entirely).
- A genuine 0.4.0 fork with a derived id, a recorded `metadata["fork"]` basis and
  an attested `fork_reason`.
- One corrupt `.tine` file to verify load errors.

## Feature Coverage

- [x] Run list shows every valid `.tine` run with status color, cost, and a
  visible status/cost summary.
- [x] Corrupt or oversized `.tine` files appear in the load-error panel.
- [x] Search filters by run id, status, model, prompt, system prompt, tags, run
  metadata, step id, step kind, and step input/output payload.
- [x] Search also accepts opentine's field grammar (`status:`, `model:`, `tag:`,
  `cost:`, `after:`, `before:`), combining fields as AND and matching opentine's
  own `match_entry` semantics. Plain text remains a substring search; a malformed
  field query reports why and falls back rather than matching nothing silently.
- [x] Selecting a run updates the detail panel and rebuilds the DAG.
- [x] Run details show model, status, created time, format + migration provenance,
  step count, step-kind summary, graph summary, total cost, cost attribution by
  model and by kind, total tokens, budget vs incurred, total duration, tags, refs,
  integrity and signature status, prompt, system prompt, and fork origin +
  fork point when present.
- [x] Compare (Diff) pairs the selected run with another and reports common
  ancestor, steps only in A / only in B, and per-field before/after deltas.
  A forked run defaults to comparing against its origin.
- [x] DAG nodes show kind-specific color, clearer kind-specific labels, duration,
  cost, parent-child links, minimap, and an Inspect action.
- [x] DAG search visually highlights matching nodes (starred label + brightened
  title bar) and reports match counts by id, kind, model, tool name, and
  input/output payload in the graph summary/status area.
- [x] Selecting a step shows formatted inputs and outputs.
- [x] `Run > Transcript...` lists every turn in order with its role, tool name and
  step link; `show step` selects that step and closes the dialog. A run with no
  transcript explains why rather than showing an empty panel.
- [x] `Run > Export as OpenTelemetry JSON` writes a valid OTLP document beside the
  run, leaves the artifact byte-identical, is id-safe, and degrades with a clear
  message on an opentine older than 0.5.0.
- [x] Pause is enabled only for running runs and writes the paused status to disk.
- [x] Resume is enabled only for paused runs and writes the running status to disk.
- [x] Fork is enabled only after a run and step are selected and writes a new run.
- [x] Forking the same step twice writes two distinct runs (opentine 0.4.0 fork
  identity) rather than silently overwriting the first.
- [x] `Run > Fork to branch...` sets the branch, an optional reason (capped at
  4096 chars, recorded at `metadata.fork_reason` and folded into the fork id via
  `intent`), and an optional reproducible id.
- [x] The run inspector reports fork lineage: origin, fork point, branch, and
  whether the fork act was unique or reproducible.
- [x] Refresh and auto-refresh preserve valid selected run/step state.
- [x] Change runs directory clears stale selection and search state.
- [x] The last runs directory and run search filter persist locally in the
  platform config directory (see Cross-platform Coverage) unless
  `OPENTINE_GUI_PREFS` points at another preference file.

## Trust and Provenance Coverage

- [x] A fork reason that does not reproduce the signed `metadata.fork.intent` digest
  renders as `Fork reason (unverified)`; a real one renders plain.
- [x] `verify_fork_id` flags a fork record edited after the fact, and confirms one
  that matches its recorded basis.
- [x] Stripping `forked_from` falls back to the fork record rather than presenting
  the run as a root.
- [x] A budget-killed run reports the breached dimension and the overage.
- [x] Forking refuses to overwrite an existing artifact (the reproducible-id case).
- [x] A v3 repository directory is reported as such, not half-opened.

## Keyboard Coverage

- [x] `Up`/`Down` move through the visible (filtered) run list and clamp at both ends;
  with nothing selected they select the first run.
- [x] Navigation keys are inert while a text field has focus or a modal is open.
- [x] `Esc` closes an open dialog first, then clears the DAG filter, then the run search.
- [x] `Ctrl+F` focuses search, `Ctrl+C` copies the selected run id, `Ctrl+R` forces a
  reload on the very next frame — including in an empty directory, where a
  cleared-signature approach would silently do nothing.
- [x] Keyboard-driven selection is deferred to the render thread, not applied from the
  key-handler thread.

## Cross-platform Coverage

- [x] Preferences resolve to the platform config dir (`%APPDATA%`, `~/Library/
  Application Support`, `~/.config`), with `XDG_CONFIG_HOME` and
  `OPENTINE_GUI_PREFS` overriding, and the pre-0.2 location still read on upgrade.
- [x] Preferences are written atomically and leave no temp files behind.
- [x] Windows device-name run ids (`CON`, `NUL`, `COM1`, `CONIN$`, ...) and
  trailing-space ids are refused as filenames on Windows and still allowed
  elsewhere. A trailing dot is fine: `<id>.tine` never ends in one.
- [x] Display scale is detected per platform and every layout dimension scales
  with it; `OPENTINE_GUI_SCALE` overrides and is clamped to 0.5–3.0.
- [x] A system monospace font is loaded per platform so non-ASCII output renders;
  the app falls back to the built-in font when no face is found.
- [x] CI runs lint + the full suite on ubuntu, windows and macos runners, and
  verifies the bundled fixtures with a shell-agnostic script.

## Verification

Every checklist item above is covered by the automated suite, by the scripted
GUI drive described in `docs/design-notes.md`, or both. CI runs lint and the
full suite on Windows, macOS and Linux across Python 3.11-3.14, and verifies
the bundled fixtures load and pass their integrity digests.

## Remaining Product Gaps

- True GUI automation is still manual because Dear PyGui is not covered by the
  headless pytest suite.
- The DAG is readable for small and medium runs; very large runs will need
  clustering, pan-to-selection, and graph search to feel like a high-end wiki UI.
- DAG search now highlights matching nodes (2026-07-29), but it does not pan/zoom
  to matches or provide next/previous match navigation.
- Run diff and budget landed 2026-07-30; branch-aware forking landed 2026-07-31.
  Still unsurfaced from opentine 0.3.0/0.4.0: `Run.transcript` (a linear
  conversational view), tag editing via `add_tag`/`remove_tag`, the query DSL
  (`RunIndex`/`parse_query`), and repository-backed runs (`Repo`).
- CJK text needs `OPENTINE_GUI_FONT` pointed at a CJK-capable face; Dear PyGui
  binds a single font atlas, so there is no automatic per-script fallback.
- There is no persistent user preference layer for window size or panel widths.
- Wiki-style traceability is limited to opentine run/step metadata. Future
  high-end passes should add richer source/citation links when opentine records
  those references in `.tine` files.
