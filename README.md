# opentine-gui

Desktop GUI for [opentine](https://github.com/0xcircuitbreaker/opentine) — visual management console with Dear PyGui.

## Install

```bash
pip install opentine-gui
```

## Usage

```bash
tine-gui
```

## Features

- Run list table with status colors
- Step DAG visualization
- Run detail panel (cost, duration, model, prompt)
- Menu bar with Refresh and Quit

## Layout

```
┌─ Menu: [Refresh] [Quit] ──────────────────────────────────────┐
│ ┌── Runs ──────┐ ┌── Details ──────┐ ┌── Step DAG ──────────┐ │
│ │ a3f8 complete│ │ Run: a3f8       │ │ [0] think: I'll...   │ │
│ │ b7c1 complete│ │ Model: claude   │ │ [1] tool: search(..) │ │
│ │ c9d2 running │ │ Steps: 4        │ │ [2] think: The...    │ │
│ │              │ │ Cost: $0.003    │ │ [3] done: The mass.. │ │
│ └──────────────┘ └─────────────────┘ └───────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

## License

Apache 2.0
