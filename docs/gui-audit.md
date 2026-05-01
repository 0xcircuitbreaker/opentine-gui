# GUI Audit - 2026-04-29

## Smoke Results

- Existing headless suite passes: `29 passed`.
- Local Ollama service is reachable at `http://localhost:11434`.
- Verified installed model tag: `qwen3.6:27b`.
- Created a real opentine run with `ollama/qwen3.6:27b`; it completed with one `done` step and was saved under `.tmp_gui_qa_runs/`.
- Launched `tine-gui .tmp_gui_qa_runs` for a native startup smoke; the process stayed running and was closed cleanly.

## Current Product Shape

The app is currently a native Dear PyGui run inspector:

- It loads `.tine` files from a runs directory.
- It lists runs with status and cost.
- It shows run metadata and selected-step details.
- It renders a step DAG with node colors and parent-child links.
- It supports search, refresh, pause, resume, fork, and runs-directory switching.

That is a useful base, but it is not yet close to a Karpathy LLM Wiki-style GUI. It does not yet create runs, stream live agent progress, organize knowledge/workspaces, or make large execution graphs easy to navigate.

## Tolaria Reference Direction

Tolaria's UI quality comes from a disciplined product system rather than any single decorative trick:

- Quiet semantic surfaces: app, sidebar, panel, card, input, and popover colors are tokenized.
- Compact information architecture: fixed-width side panels, a strong center workspace, and a right inspector.
- Small, consistent typography: 11-13 px metadata and controls, with restrained title treatment.
- Subtle interaction states: hover, selected, active, borders, and focus rings are present but not loud.
- Keyboard-first workflows: command palette, quick open, search, and compact icon controls are central.
- AI is a first-class panel, not a bolt-on: contextual chat has its own header, context bar, history, and composer.

The first opentine pass now borrows that direction inside Dear PyGui's native constraints: semantic dark palette, compact panel headers, bordered work surfaces, quieter buttons, and a cleaner run/detail/DAG workspace.

## Main Gaps

- Active run control is shallow: pause/resume currently writes status to disk, but the opentine runtime does not appear to stream/save step-by-step or check that status while a model run is executing.
- No in-GUI run launcher: the user cannot pick `ollama/qwen3.6:27b`, enter a prompt, run an agent, and watch the trace fill in from inside the GUI.
- No automated GUI coverage: tests cover loading and business logic, but not Dear PyGui rendering, clicks, dialogs, node selection, or layout regressions.
- DAG navigation is basic: large runs need graph search, clustering, pan-to-step, collapse/expand, breadcrumbs, and better detail affordances.
- Step details are plain text: tool arguments, JSON outputs, model text, errors, and diffs need dedicated viewers.
- No persistence layer for user preferences such as last runs directory, window size, filter text, and panel widths.
- No wiki/workspace model yet: there are no concepts for notebooks, pages, source documents, references, saved answers, semantic search, or reusable run collections.

## Recommended Next Milestones

1. Stabilize the inspector.
   Add a repeatable GUI smoke harness, robust DAG layout for larger or out-of-order traces, better error surfacing, and preference persistence.

2. Add a local run launcher.
   Build a left/top control surface for model selection, prompt entry, tool enablement, max steps, and run start/stop. Start with Ollama and default to `qwen3.6:27b` when present.

3. Make runs live.
   Update the opentine execution path so it saves incremental steps while running and honors pause/stop state. The GUI can then auto-refresh into a true live trace.

4. Upgrade the DAG into a workbench.
   Add graph search, node filtering, collapse/expand, pan-to-selected, grouped tool/model spans, step timeline, and split-pane output viewers.

5. Move toward LLM Wiki.
   Add persistent workspaces, page/source collections, saved summaries, backlinks between runs and artifacts, full-text/semantic search, and replay/fork/diff workflows that feel like browsing a knowledge graph.
