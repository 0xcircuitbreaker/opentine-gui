# opentine-gui

Desktop GUI for [opentine](https://github.com/0xcircuitbreaker/opentine) — browse, inspect,
compare and fork agent runs, built with Dear PyGui.

![The opentine run console: run list, run and step inspectors, and the step DAG](https://raw.githubusercontent.com/0xcircuitbreaker/opentine-gui/main/docs/assets/console.png)

Agent runs are recorded by [opentine](https://github.com/0xcircuitbreaker/opentine) as
content-addressed, integrity-checked `.tine` artifacts, or as objects in a v3 repository. This
is the desktop console for reading them: what the agent did, what each step cost, who served
it, where a run branched, and whether the artifact in front of you is what it claims to be.

## Install

```bash
pip install opentine-gui
tine-gui
```

Or from source with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run tine-gui
```

Requires Python 3.11+ and [`opentine`](https://github.com/0xcircuitbreaker/opentine) **0.7.2 or
newer**, which `pip` installs for you. That floor is a correctness requirement, not a
preference: opentine below 0.7.1 cannot read a step's `causal_ids` (so saving a run through an
older library silently erases the causal edges a v3-derived run carries) and cannot verify a
`tine-sig/2` signature (so it reports a valid signature as an error, which reads as tampering).

Runs on **Windows, macOS and Linux** — lint and the whole test suite run on all
three in CI.

## Usage

```bash
tine-gui                # reads last used dir, then ./.tine_runs
tine-gui path/to/runs   # a directory of .tine files
tine-gui path/to/repo   # an opentine v3 repository, opened read-only
```

### Keyboard

| Key | Action |
| --- | --- |
| `Up` / `Down` | Move through the visible run list |
| `Esc` | Close a dialog, else clear the DAG filter, else clear the run search |
| `Ctrl+F` | Focus the run search box |
| `Ctrl+C` | Copy the selected run id |
| `Ctrl+R` | Force a reload of the runs directory |
| `Ctrl+O` | Change the runs directory |
| `F1` | Keyboard and feature help |

On macOS the `Cmd` key works everywhere `Ctrl` does. Navigation keys stay out of the way while
a text field has focus or a dialog is open.

### Environment

| Variable | Effect |
| --- | --- |
| `OPENTINE_GUI_PREFS` | Preferences file location (default: the platform config dir, below) |
| `OPENTINE_GUI_SCALE` | UI scale factor, `0.5`–`3.0`. Overrides DPI auto-detection |
| `OPENTINE_GUI_FONT` | Path to a `.ttf`/`.ttc` to render with. Set this for CJK text |
| `OPENTINE_GUI_HMAC_KEY` | HMAC signing key, or a path to a file holding one, used to verify signatures |
| `OPENTINE_GUI_PUBLIC_KEY` | Path to an Ed25519 public key used to verify signatures |

Preferences live in `%APPDATA%\opentine-gui\` on Windows,
`~/Library/Application Support/opentine-gui/` on macOS and
`~/.config/opentine-gui/` on Linux; an explicit `XDG_CONFIG_HOME` wins on every
platform, matching opentine's own pricing-overlay convention.

The console picks up the display scale automatically (per-monitor DPI on
Windows, `GDK_SCALE`/`QT_SCALE_FACTOR` on Linux) and loads a system monospace
font so accented text, symbols and arrows in recorded output render properly.
CJK glyphs need a font that has them — point `OPENTINE_GUI_FONT` at e.g.
Noto Sans Mono CJK.

## Features

**Reading a run**

- Run list with status colours, model, step count, cost and age, sortable by any column
- Step DAG rendered as a real node editor: full multi-parent ancestry, per-kind colours,
  minimap, and **causal edges** drawn apart from execution lineage — those are the extra
  ancestors a fork keeps, so a graph that hid them would disagree with the fork it previews
- Run detail: model, provider, status, cost (with per-model and per-kind attribution), tokens,
  budget vs incurred, duration, tags, refs, format and migration provenance, integrity and
  signature state, prompt, system prompt, and full fork lineage
- Step detail: inputs, outputs, tool info, error payloads, usage, billing, provider,
  causally-required steps, timestamp and cost
- **Transcript** (`Run > Transcript...`) — the conversation that produced the graph, with tool
  calls, tool results and separated reasoning, each turn linked to the step it created
- Copy or expand any panel: ids, whole inspectors, prompts, payloads, diffs and transcripts

**Comparing, pricing and counting**

- Compare any two runs — common ancestor, steps only on each side, per-field before/after
  deltas, plus the fields opentine's own diff does not compare (`provider`, `causal_ids`)
- **Price this run** (`Run > Price this run...`) recomputes cost from the run's own record
  against opentine's signed catalog, as of a date you choose, and reports the catalog it used.
  An imported run that recorded no cost is reported as `unknown`, never as `$0.00`
- **Statistics** (`View > Statistics...`) — a `tine stats`-shaped rollup over what is loaded:
  run and step counts, cost total/mean/max, distinct models, grouped by model, status, tag,
  day, format version or provider. Figures that were never collected read `-`, never `0`

**Getting runs in and out**

- **Export as OpenTelemetry GenAI** (OTLP/JSON), the same document `tine export` writes, so a
  verified run can go to whatever observability backend runs beside it
- **Import a trace** (`File > Import a trace...`) — OTLP/JSON, opentine JSONL, LangChain,
  LlamaIndex, AutoGen, CrewAI and OpenAI-Agents logs become a `.tine` artifact this console
  can then open, fork and export. Import warnings are surfaced, not swallowed

**Sources**

- A directory of `.tine` files: readable and writable (pause, resume, fork)
- An **opentine v3 repository**: opened read-only, with its refs, branches, tags and
  promotions listed and every run in the object store browsable. Writing into a repository
  would append an object and move a branch, so those actions stay disabled here
- Live auto-refresh while agents write, on a worker thread so the UI never stalls on a scan
- Corrupt `.tine` files surfaced as load errors, not silently dropped

**Search**

- Plain words are a substring search over ids, prompts, tags, metadata, step payloads and
  recorded providers
- A field prefix switches to opentine's own grammar, the same one `tine ls` and `tine search`
  accept: `status:failed`, `model:opus`, `tag:bug`, `cost:>0.01`, `cost:0.01..1`,
  `after:2026-07-01`, `before:`, `text:`
- DAG search highlights matching steps and `Next match` walks them

**Trust**

- Integrity and signature state per artifact, with what each one actually covers stated:
  the integrity digest excludes `metadata`, and a `tine-sig/1` signature excludes `tags` and
  `fork_reason`, so neither is a blanket "this file is genuine"
- Point `OPENTINE_GUI_HMAC_KEY` or `OPENTINE_GUI_PUBLIC_KEY` at your key material and
  signatures are actually verified here rather than reported as unverifiable
- A fork reason is checked against the signed fork intent and labelled unverified when it does
  not reproduce; `verify_fork_id` flags a fork record edited after the fact
- Pause and Resume ask first when saving would drop a signature or a draft marker, because
  opentine rewrites the integrity block on every save and this console holds no signing key

Reads the current open-source [opentine](https://pypi.org/project/opentine/) `.tine` format
(`format_version == 2`) and opentine v3 repositories. Legacy `format_version == 1` files are
auto-migrated on load (pause/resume re-save the file as v2; fork writes its new artifact as v2
and leaves the source untouched). Try it with bundled demo runs:

```bash
uv run python demo/seed.py   # writes valid .tine fixtures into ./.tine_runs
uv run tine-gui
```

## Layout

```
┌─ File   Run   View   Help ───────────────────────────────────────┐
│ ┌── Runs ──────┐ ┌── Run ──────────┐ ┌── Step DAG ────────────┐ │
│ │ a3f8 complete│ │ id, model, cost │ │  ┌─think─┐   ┌─tool──┐ │ │
│ │ b7c1 complete│ │ steps, duration │ │  │ plan  │──▶│search │ │ │
│ │ c9d2 running │ │ prompt          │ │  └───────┘   └───────┘ │ │
│ │              │ │ ── Step ──      │ │                        │ │
│ └──────────────┘ └─────────────────┘ └────────────────────────┘ │
│ status line                                                      │
│ message log                                                      │
└──────────────────────────────────────────────────────────────────┘
```

## Development

```bash
uv sync --extra dev
uv run pytest -q
uvx ruff check opentine_gui tests demo scripts
```

The suite is headless: it never creates a Dear PyGui context. `tests/fakedpg.py` is a recording
stand-in for the library, installed for every test, so callbacks and widget construction are
covered without a display — and so a stray native call fails a test instead of segfaulting the
run.

`docs/design-notes.md` explains why the console is built the way it is, including the
constraints that are not obvious from the APIs. `docs/gui-qa.md` is the feature checklist.
`SECURITY.md` covers the threat model and how to report a vulnerability.

## Licence

Apache-2.0.
