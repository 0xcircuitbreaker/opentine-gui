# GUI QA Checklist

This checklist is the console's feature contract: every box below is either
covered by the automated suite or is a manual check with a stated reason why it
cannot be automated. It targets **opentine 0.7.2**.

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
- A run carrying `causal_ids` (a step that required a non-parent step).
- A run with model steps, real token usage and no recorded cost at all — what an
  imported trace looks like.
- A signed run (`Run.save(path, sign_key=...)`), to exercise the trust panel and
  the confirmation before a save that would drop the signature.
- A v3 repository with at least two refs, to exercise the read-only source.

`demo/seed.py` writes all of the above, including the repository beside the runs
directory.

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

## Sources

- [x] A directory of `.tine` files is readable and writable.
- [x] A v3 repository opens read-only: its runs are listed, the refs pointing at
  each run are shown, `View > Repository refs...` groups heads/tags/promotions,
  and pause, resume and fork are disabled with a reason rather than silently
  inert.
- [x] Opening a repository writes nothing to it — asserted by comparing the whole
  directory tree before and after a scan, because `Repo.open` would heal the
  layout and leave untracked directories behind.
- [x] A missing or unreadable runs directory is reported as an error, not shown
  as an empty one.
- [x] Scanning happens on a worker thread; the UI stays responsive while a large
  directory is read, and the worker never touches Dear PyGui.
- [x] An unchanged file is not re-parsed on the next tick, and the selected run's
  graph is not rebuilt (losing pan, zoom and node positions) unless that run's
  own bytes changed.

## Cost and Pricing Coverage

- [x] A run that recorded no price at all reads `no cost recorded`, not `$0.0000`.
- [x] A partially priced run reads `>=`, with the count of unpriced invocations.
- [x] `Run > Price this run...` recomputes from opentine's signed catalog, names
  the catalog id and hash, and reports `unknown` (never zero) for a step the
  catalog cannot answer for.
- [x] Pricing accepts an as-of date, and reports which rule was applied
  (`recorded` versus a pinned day).
- [x] A step with no recorded provider can be priced against a provider the
  reader picks, and every figure derived from that choice is marked assumed.
- [x] `View > Statistics...` groups by model, status, tag, day, format version and
  provider; a figure that was never collected renders `-` and never sums.

## Interop Coverage

- [x] `Run > Export as OpenTelemetry JSON` writes the same document `tine export`
  writes, refuses to overwrite without confirmation, writes atomically, and
  leaves the artifact byte-identical.
- [x] `File > Import a trace...` turns an OTLP/JSON, JSONL or framework log into a
  `.tine` artifact, refuses an unsafe or colliding destination, cleans up its
  temporary workspace on every path, and surfaces opentine's import warnings.

## Trust and Provenance Coverage

- [x] A fork reason that does not reproduce the signed `metadata.fork.intent` digest
  renders as `Fork reason (unverified)`; a real one renders plain.
- [x] `verify_fork_id` flags a fork record edited after the fact, and confirms one
  that matches its recorded basis.
- [x] Stripping `forked_from` falls back to the fork record rather than presenting
  the run as a root.
- [x] A budget-killed run reports the breached dimension and the overage.
- [x] Forking refuses to overwrite an existing artifact (the reproducible-id case).
- [x] The integrity row states that the digest excludes `metadata`, and the
  signature row names the scheme (`tine-sig/1` covers eleven metadata keys and
  excludes `tags` and `fork_reason`; `tine-sig/2` covers all but `integrity`).
- [x] With `OPENTINE_GUI_HMAC_KEY` or `OPENTINE_GUI_PUBLIC_KEY` configured, a
  genuine signature reads `verified`; a wrong key reads as a mismatch; a
  trust-on-first-use result is rendered differently from a real verification.
- [x] Pause and Resume ask before a save that would drop a signature or a draft
  marker, and write nothing if the confirmation is declined.
- [x] Causal edges are drawn apart from lineage, counted in the graph summary, and
  listed in the step inspector; the fork dialog states the slice the fork will
  keep using opentine's own `retained_closure`.
- [x] Compare reports `provider` and `causal_ids` differences, which opentine's
  own `Run.diff` does not compare.

## Keyboard Coverage

- [x] `Up`/`Down` move through the visible (filtered) run list and clamp at both ends;
  with nothing selected they select the first run.
- [x] Navigation keys are inert while a text field has focus or a modal is open.
- [x] `Esc` closes an open dialog first, then clears the DAG filter, then the run search.
- [x] `Ctrl+F` focuses search, `Ctrl+C` copies the selected run id, `Ctrl+R` forces a
  reload on the very next frame — including in an empty directory, where a
  cleared-signature approach would silently do nothing. `Ctrl+O` opens the
  directory picker and `F1` opens help.
- [x] On macOS the same chords work with `Cmd`.
- [x] `Ctrl+F` does not focus the search box behind an open dialog.
- [x] Every modal — including the transcript, the text viewer, the panel views and
  the confirmation — counts as a modal for `Esc` and for navigation keys.
- [x] Keyboard-driven selection is applied between frames rather than mid-frame.

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

Every checklist item above is covered by the automated suite. Widget-level
behaviour is covered too: `tests/fakedpg.py` is a recording stand-in for Dear
PyGui, installed for every test, so callbacks, table rendering, DAG construction
and modal behaviour are all exercised without a display. CI runs lint and the
full suite on Windows, macOS and Linux across Python 3.11-3.14, and verifies the
bundled fixtures load and pass their integrity digests.

What the stand-in cannot check is what the native library actually draws:
layout, hit testing, fonts and the node editor's own interaction. Those remain a
manual pass against `demo/seed.py` output.

## Remaining Product Gaps

- Real rendering is still verified by eye. The stand-in proves the console asks
  for the right widgets, not that Dear PyGui draws them correctly.
- Very large runs are drawn up to a stated cap; clustering and pan-to-node would
  be needed to make a several-thousand-step graph genuinely navigable. `Next
  match` scrolls to a highlighted step, but there is no zoom-to-fit.
- Not surfaced from opentine: tag editing (`add_tag`/`remove_tag` — needs a save
  path that does not destroy a signature), `tine replay --verify` as a
  determinism badge, the v3 mutating verbs (`repo-fork`, `attest`, `evaluate`,
  `promote`), and OTLP/HTTP push. The reasons for each are in
  `docs/design-notes.md`.
- CJK text needs `OPENTINE_GUI_FONT` pointed at a CJK-capable face; Dear PyGui
  binds a single font atlas, so there is no automatic per-script fallback.
- Window size and panel widths are not persisted; the runs directory, filter,
  sort order and pricing choices are.
