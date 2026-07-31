# opentine-gui

Desktop GUI for [opentine](https://github.com/0xcircuitbreaker/opentine) — visual management console built with Dear PyGui.

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

Requires Python 3.11+ and [`opentine`](https://github.com/0xcircuitbreaker/opentine) 0.4.0
or newer, which `pip` installs for you.

Runs on **Windows, macOS and Linux** — lint and the whole test suite run on all
three in CI.

## Usage

```bash
tine-gui                # reads last used dir, then ./.tine_runs
tine-gui path/to/runs   # point at a different runs directory
```

### Keyboard

| Key | Action |
| --- | --- |
| `Up` / `Down` | Move through the visible run list |
| `Esc` | Close a dialog, else clear the DAG filter, else clear the run search |
| `Ctrl+F` | Focus the run search box |
| `Ctrl+C` | Copy the selected run id |
| `Ctrl+R` | Force a reload of the runs directory |

Navigation keys stay out of the way while a text field has focus or a dialog is open.

### Environment

| Variable | Effect |
| --- | --- |
| `OPENTINE_GUI_PREFS` | Preferences file location (default: the platform config dir, below) |
| `OPENTINE_GUI_SCALE` | UI scale factor, `0.5`–`3.0`. Overrides DPI auto-detection |
| `OPENTINE_GUI_FONT` | Path to a `.ttf`/`.ttc` to render with. Set this for CJK text |

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

- Run list with status colors (running / paused / completed / failed)
- Step DAG rendered as a real node editor (full multi-parent ancestry, per-kind colors, minimap)
- Run detail: model, status, cost (with per-model / per-kind attribution), tokens, budget vs
  incurred, duration, tags, refs/branches, format + migration provenance, integrity and
  signature status, prompt + system prompt, and fork lineage — origin, fork point, branch,
  and whether the fork act was unique or reproducible
- Step detail: inputs, outputs, tool info, error payloads, usage, billing, timestamp, cost
- Compare any two runs — common ancestor, steps only on each side, and per-field
  before/after deltas including cost, model and token usage
- DAG search with visual highlight over ids, kinds, tool names, payloads, and error text
- Live auto-refresh — picks up new `.tine` files while agents run
- Run actions: Pause / Resume / Fork-from-step / Fork-to-branch / Compare (writes back to the file each run was
  loaded from; pause and resume reload disk state first so a stale GUI snapshot cannot
  truncate or clobber what an agent wrote since — a small write race remains without
  file locking)
- File menu: Refresh, Change runs dir, Quit
- Corrupt `.tine` files surfaced as load errors, not silently dropped; integrity-digest
  mismatches are flagged in the error panel and the run inspector
- Lightweight local preferences for the last runs directory and run search filter

Reads the current open-source [opentine](https://pypi.org/project/opentine/) `.tine` format
(`format_version == 2`, opentine ≥ 0.4.0) — the same content-addressed, integrity-checked
artifacts agents and CLI harnesses produce. Legacy `format_version == 1` files are
auto-migrated on load (pause/resume re-save the file as v2; fork writes its new
artifact as v2 and leaves the source untouched). Try it with bundled
demo runs:

```bash
uv run python demo/seed.py   # writes valid .tine fixtures into ./.tine_runs
uv run tine-gui
```

## Layout

```
┌─ File   Run ─────────────────────────────────────────────────────┐
│ ┌── Runs ──────┐ ┌── Run ──────────┐ ┌── Step DAG ────────────┐ │
│ │ a3f8 complete│ │ id, model, cost │ │  ┌─think─┐   ┌─tool──┐ │ │
│ │ b7c1 complete│ │ steps, duration │ │  │ plan  │──▶│search │ │ │
│ │ c9d2 running │ │ prompt          │ │  └───────┘   └───────┘ │ │
│ │              │ │ ── Step ──      │ │                        │ │
│ │              │ │ inputs/outputs  │ │                        │ │
│ └──────────────┘ └─────────────────┘ └────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Development

```bash
uv sync --extra dev
uv run pytest
uvx ruff check opentine_gui tests
```

Manual GUI QA coverage lives in [docs/gui-qa.md](https://github.com/0xcircuitbreaker/opentine-gui/blob/main/docs/gui-qa.md).

## License

Apache 2.0
