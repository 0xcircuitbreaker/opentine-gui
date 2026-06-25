# Changelog

All notable changes to opentine-gui are documented here.

## [0.1.0] - 2026-06-25

First production-ready release. Audited and aligned against the released
open-source [opentine 0.1.1](https://pypi.org/project/opentine/) (`.tine`
`format_version == 1`).

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
