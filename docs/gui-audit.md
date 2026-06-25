# Production Readiness Audit - 2026-06-25

Audited the GUI against the **released open-source opentine 0.1.1** (PyPI; `.tine`
`format_version == 1`), incorporating opentine PR #2 ("Release audit fixes + 0.1.1",
merged and tested). The app's *runtime accessors* already matched the real
`Run`/`Step` API, but the **data fixtures and tests were written against an API and
file format that opentine never shipped**, so the product was broken end-to-end on a
clean install.

## Findings (all fixed)

1. **Bundled `.tine` fixtures used an obsolete layout** — flat top-level `steps` list,
   singular `parent_id`, `type` discriminators, no `format_version`/`graph`/integrity.
   Released `Run.load()` rejected all five (`Unsupported .tine format_version='missing';
   expected 1`), so the GUI showed zero runs and five load errors out of the box.
2. **`demo/seed.py` crashed** under real opentine: `Step(parent_id=…)` and `Run(steps=…)`
   are not valid constructors (`Step.parent_ids: list[str]`; `Run.__init__` rejects
   unknown kwargs). Rewritten to build `Graph`/`Step`/`Run` correctly and save valid,
   integrity-checked artifacts. Fixtures regenerated.
3. **Test suite (18/34 failing)** shared the same invalid constructors, plus mutated the
   read-only `Run.id` property and appended to the read-only `Run.steps` view. Rewritten;
   suite is now 36/36 green against opentine 0.1.1.
4. **Multi-parent graphs under-rendered.** opentine steps carry `parent_ids` (a list);
   `Step.parent_id` returns only the *last* parent. The DAG, depth, and graph-stats logic
   used the singular accessor, so merge nodes silently dropped edges. Now every parent is
   drawn and counted.
5. **Step content didn't match opentine conventions.** Error messages live in `step.error`,
   tool names in `step.tool_info`, and `done` text often in `inputs.text`. Node labels, the
   step inspector, and search now read those fields.

## Verification

- `pytest`: 36 passed. `ruff check`: clean.
- All five regenerated `.tine_runs/*.tine` fixtures load and pass `Run.verify_integrity`.
- Full GUI launched on a real display: themes, node editor, tables, run/step selection, and
  the DAG built and rendered without error.
- Added `.github/workflows/ci.yml` (ruff + headless pytest + fixture-load proof on 3.11/3.12)
  and a `CHANGELOG.md`. Dependency pinned to `opentine >= 0.1.1`; `uv.lock` refreshed.

---

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
