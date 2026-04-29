# GUI QA Checklist

This checklist covers the current `opentine-gui` feature contract and the
graph-first expectations used for the Karpathy LLM Wiki-inspired pass.

## Baseline Data

Use a disposable runs directory with:

- `completed`, `running`, `paused`, and `failed` runs.
- `think`, `tool`, `model`, `done`, and `error` steps.
- Linear and branched DAGs.
- A forked run with `metadata["forked_from"]`.
- One corrupt `.tine` file to verify load errors.

## Feature Coverage

- [x] Run list shows every valid `.tine` run with status color, cost, and a
  visible status/cost summary.
- [x] Corrupt or oversized `.tine` files appear in the load-error panel.
- [x] Search filters by run id, status, model, prompt, metadata, step id, step kind,
  and step input/output payload.
- [x] Selecting a run updates the detail panel and rebuilds the DAG.
- [x] Run details show model, status, created time, step count, step-kind summary,
  graph summary, total cost, total duration, prompt, and fork origin when present.
- [x] DAG nodes show kind-specific color, clearer kind-specific labels, duration,
  cost, parent-child links, minimap, and an Inspect action.
- [x] DAG search reports matching steps by id, kind, model, tool name, and
  input/output payload in the graph summary/status area.
- [x] Selecting a step shows formatted inputs and outputs.
- [x] Pause is enabled only for running runs and writes the paused status to disk.
- [x] Resume is enabled only for paused runs and writes the running status to disk.
- [x] Fork is enabled only after a run and step are selected and writes a new run.
- [x] Refresh and auto-refresh preserve valid selected run/step state.
- [x] Change runs directory clears stale selection and search state.
- [x] The last runs directory and run search filter persist locally in
  `~/.config/opentine-gui/preferences.json` unless `OPENTINE_GUI_PREFS` points
  at another preference file.

## QA Run

- `uv run pytest` and `uvx ruff check opentine_gui tests` could not run in this
  cloud image because `uv`/`uvx` were not installed.
- Installed `python3.12-venv`, created `.venv`, then installed the project with
  `.[dev]` and latest `ruff`.
- Passed: `.venv/bin/python -m pytest` (`34 passed`).
- Passed: `.venv/bin/python -m ruff check opentine_gui tests`.
- Passed demo data smoke:
  `.venv/bin/python -c "... demo.seed.main(); load_runs(Path('.tine_runs')) ..."`
  verified 5 valid demo runs, 1 corrupt-file load error, and 6 signature entries.
- GUI launch smoke:
  `timeout 5s xvfb-run -a .venv/bin/tine-gui .tine_runs` opened under a virtual
  display and was terminated by the timeout as expected for a long-running GUI.

## Remaining Product Gaps

- True GUI automation is still manual because Dear PyGui is not covered by the
  headless pytest suite.
- The DAG is readable for small and medium runs; very large runs will need
  clustering, pan-to-selection, and graph search to feel like a high-end wiki UI.
- DAG search now reports matching nodes, but it does not pan/zoom to matches,
  visually highlight nodes, or provide next/previous match navigation.
- There is no persistent user preference layer for window size or panel widths.
- Wiki-style traceability is limited to opentine run/step metadata. Future
  high-end passes should add richer source/citation links when opentine records
  those references in `.tine` files.
