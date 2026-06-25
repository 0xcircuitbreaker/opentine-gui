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
- Step DAG rendered as a real node editor (full multi-parent ancestry, per-kind colors, minimap)
- Run detail + selected-step detail (inputs, outputs, tool info, error payloads, cost, duration, model)
- Graph summaries and DAG search over ids, kinds, tool names, payloads, and error text
- Live auto-refresh — picks up new `.tine` files while agents run
- Run actions: Pause / Resume / Fork-from-step (writes back to the runs dir)
- File menu: Refresh, Change runs dir, Quit
- Corrupt `.tine` files surfaced as load errors, not silently dropped
- Lightweight local preferences for the last runs directory and run search filter

Reads the current open-source [opentine](https://pypi.org/project/opentine/) `.tine` format
(`format_version == 1`, opentine ≥ 0.1.1) — the same content-addressed, integrity-checked
artifacts agents and CLI harnesses produce. Try it with bundled demo runs:

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

Manual GUI QA coverage lives in [docs/gui-qa.md](docs/gui-qa.md).

## License

Apache 2.0
