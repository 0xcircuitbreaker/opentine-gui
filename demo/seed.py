"""Seed ./.tine_runs with demo runs covering every status, kind, and DAG shape.

Run: `uv run python demo/seed.py`
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from opentine.core import Run, RunStatus, Step, StepKind


def _step(
    sid: str,
    parent: str | None,
    kind: StepKind,
    inputs: dict | None = None,
    outputs: dict | None = None,
    *,
    cost: float = 0.0,
    duration: float = 0.05,
    model: str = "",
    ts: float = 0.0,
) -> Step:
    return Step(
        id=sid,
        parent_id=parent,
        kind=kind,
        inputs=inputs or {},
        outputs=outputs or {},
        model_info=model,
        timestamp=ts,
        duration=duration,
        cost=cost,
    )


def completed_linear() -> Run:
    steps = [
        _step("s1", None, StepKind.think, {"text": "Plan the search strategy for Tine benchmarks."}),
        _step(
            "s2", "s1", StepKind.tool,
            {"name": "web_search", "arguments": {"q": "tine benchmark 2026"}},
            {"result": "3 results"},
            cost=0.0008, duration=0.42,
        ),
        _step(
            "s3", "s2", StepKind.model,
            {"text": "Summarize search results"},
            {"text": "Tine shows 2.3x throughput vs baseline."},
            cost=0.012, duration=1.7, model="claude-sonnet-4-6",
        ),
        _step("s4", "s3", StepKind.done, outputs={"answer": "Tine is 2.3x faster."}),
    ]
    return Run(
        id="demo-complete",
        steps=steps,
        status=RunStatus.completed,
        model_info="claude-sonnet-4-6",
        system_prompt="You are a research assistant.",
        user_prompt="How does Tine compare to the baseline benchmark?",
        created_at=time.time() - 3600,
        metadata={},
    )


def running_branched() -> Run:
    # root -> (tool_a, tool_b); tool_b -> model -> pending think
    steps = [
        _step("r1", None, StepKind.think, {"text": "Gather two independent data sources."}),
        _step("r2a", "r1", StepKind.tool, {"name": "fetch_docs", "arguments": {"topic": "api"}},
              {"pages": 12}, cost=0.0003, duration=0.9),
        _step("r2b", "r1", StepKind.tool, {"name": "db_query", "arguments": {"table": "events"}},
              {"rows": 5421}, cost=0.0001, duration=0.3),
        _step("r3", "r2b", StepKind.model,
              {"text": "Reduce events into a timeline."},
              {"text": "Generated 3 clusters"},
              cost=0.008, duration=1.1, model="claude-haiku-4-5-20251001"),
        _step("r4", "r3", StepKind.think, {"text": "Cross-reference docs with timeline…"}),
    ]
    return Run(
        id="demo-running",
        steps=steps,
        status=RunStatus.running,
        model_info="claude-sonnet-4-6",
        system_prompt="",
        user_prompt="Build a unified timeline from docs + events table.",
        created_at=time.time() - 120,
        metadata={},
    )


def paused_midflight() -> Run:
    steps = [
        _step("p1", None, StepKind.think, {"text": "Draft the refactor plan."}),
        _step("p2", "p1", StepKind.model, {"text": "Enumerate modules"},
              {"text": "8 modules identified"},
              cost=0.006, duration=0.8, model="claude-sonnet-4-6"),
        _step("p3", "p2", StepKind.tool, {"name": "read_file", "arguments": {"path": "core.py"}},
              {"content": "...truncated..."}, cost=0.0, duration=0.05),
    ]
    return Run(
        id="demo-paused",
        steps=steps,
        status=RunStatus.paused,
        model_info="claude-sonnet-4-6",
        system_prompt="",
        user_prompt="Refactor the core module for readability.",
        created_at=time.time() - 600,
        metadata={},
    )


def failed_deep() -> Run:
    # deeper chain ending in an error
    steps = [
        _step("f1", None, StepKind.think, {"text": "Deploy the release pipeline."}),
        _step("f2", "f1", StepKind.tool,
              {"name": "run_ci", "arguments": {"branch": "main"}},
              {"exit": 0}, cost=0.0, duration=2.4),
        _step("f3", "f2", StepKind.tool,
              {"name": "publish", "arguments": {"target": "pypi"}},
              {"exit": 0}, cost=0.0, duration=1.1),
        _step("f4", "f3", StepKind.model,
              {"text": "Write release notes"},
              {"text": "Release notes drafted"},
              cost=0.004, duration=0.6, model="claude-sonnet-4-6"),
        _step("f5", "f4", StepKind.tool,
              {"name": "create_tag", "arguments": {"name": "v0.1.0"}},
              {"error": "tag already exists"}, cost=0.0, duration=0.1),
        _step("f6", "f5", StepKind.error,
              inputs={"message": "git tag v0.1.0 already exists; aborting"}),
    ]
    return Run(
        id="demo-failed",
        steps=steps,
        status=RunStatus.failed,
        model_info="claude-opus-4-7",
        system_prompt="",
        user_prompt="Cut release v0.1.0.",
        created_at=time.time() - 7200,
        metadata={},
    )


def forked_run() -> Run:
    steps = [
        _step("k1", None, StepKind.think, {"text": "Retry with different strategy."}),
        _step("k2", "k1", StepKind.model,
              {"text": "Reason about alternatives"},
              {"text": "Plan B: incremental rollout"},
              cost=0.005, duration=0.9, model="claude-sonnet-4-6"),
    ]
    return Run(
        id="demo-fork-child",
        steps=steps,
        status=RunStatus.running,
        model_info="claude-sonnet-4-6",
        system_prompt="",
        user_prompt="Cut release v0.1.0.",
        created_at=time.time() - 60,
        metadata={"forked_from": "demo-failed@f4"},
    )


def main() -> None:
    runs_dir = Path(".tine_runs")
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    runs_dir.mkdir()

    for run in [
        completed_linear(),
        running_branched(),
        paused_midflight(),
        failed_deep(),
        forked_run(),
    ]:
        path = runs_dir / f"{run.id}.tine"
        run.save(path)
        print(f"  wrote {path}  [{run.status.value}, {len(run.steps)} steps]")

    # Also drop a corrupt file to exercise the load-error path.
    (runs_dir / "zz-corrupt.tine").write_bytes(b"not valid msgpack bytes")
    print(f"  wrote {runs_dir / 'zz-corrupt.tine'}  [intentionally corrupt]")

    print(f"\nSeeded {runs_dir.resolve()}")


if __name__ == "__main__":
    main()
