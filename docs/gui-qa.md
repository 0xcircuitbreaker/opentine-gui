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

- Run list shows every valid `.tine` run with status color and cost.
- Corrupt or oversized `.tine` files appear in the load-error panel.
- Search filters by run id, status, model, prompt, metadata, step id, step kind,
  and step input/output payload.
- Selecting a run updates the detail panel and rebuilds the DAG.
- Run details show model, status, created time, step count, step-kind summary,
  total cost, total duration, prompt, and fork origin when present.
- DAG nodes show kind-specific color, label, duration, cost, parent-child links,
  minimap, and an Inspect action.
- Selecting a step shows formatted inputs and outputs.
- Pause is enabled only for running runs and writes the paused status to disk.
- Resume is enabled only for paused runs and writes the running status to disk.
- Fork is enabled only after a run and step are selected and writes a new run.
- Refresh and auto-refresh preserve valid selected run/step state.
- Change runs directory clears stale selection and search state.

## Remaining Product Gaps

- True GUI automation is still manual because Dear PyGui is not covered by the
  headless pytest suite.
- The DAG is readable for small and medium runs; very large runs will need
  clustering, pan-to-selection, and graph search to feel like a high-end wiki UI.
- There is no persistent user preference layer for window size, last runs
  directory, filters, or panel widths.
