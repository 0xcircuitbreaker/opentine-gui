# opentine-gui

Desktop GUI for [opentine](https://github.com/0xcircuitbreaker/opentine) — visual management console built with Dear PyGui.

## Install

```bash
pip install opentine-gui
```

Or from source with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run tine-gui
```

## Usage

```bash
tine-gui                # reads last used dir, then ./.tine_runs
tine-gui path/to/runs   # point at a different runs directory
```

## Features

- Run list with status colors (running / paused / completed / failed)
- Step DAG rendered as a real node editor (parent → child links, per-kind colors, minimap)
- Run detail + selected-step detail (inputs, outputs, cost, duration, model)
- Graph summaries, node highlighting, and search feedback for wiki-style trace navigation
- Live auto-refresh — picks up new `.tine` files while agents run
- Run actions: Pause / Resume / Fork-from-step (writes back to the runs dir)
- File menu: Refresh, Change runs dir, Quit
- Corrupt `.tine` files surfaced as load errors, not silently dropped
- Lightweight local preferences for the last runs directory and run search filter

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

Manual GUI QA coverage lives in [docs/gui-qa.md](docs/gui-qa.md).

## License

Apache 2.0
