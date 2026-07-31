"""Seed ./.tine_runs with demo runs covering every status, kind, and DAG shape.

Runs are built with the real opentine API and written in the current portable
``.tine`` format of the installed opentine (``format_version == 2`` as of
opentine 0.3.0, integrity digest included), so the GUI loads them exactly like
runs produced by live agents. Legacy v1 files are auto-migrated by Run.load;
a committed v1 sample lives in tests/fixtures/legacy_v1.tine.

Run: `uv run python demo/seed.py`
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from opentine.core import Graph, Run, RunStatus, Step, StepKind


def _step(
    sid: str,
    parents: list[str],
    kind: StepKind,
    inputs: dict | None = None,
    outputs: dict | None = None,
    *,
    cost: float = 0.0,
    duration: float = 0.05,
    model: str = "",
    tool_info: dict | None = None,
    error: dict | None = None,
    ts: float = 0.0,
    usage: dict | None = None,
    billing: dict | None = None,
) -> Step:
    return Step(
        id=sid,
        parent_ids=list(parents),
        kind=kind,
        inputs=inputs or {},
        outputs=outputs or {},
        model_info=model,
        tool_info=tool_info or {},
        error=error or {},
        timestamp=ts,
        duration=duration,
        cost=cost,
        usage=usage or {},
        billing=billing or {},
    )


def _run(run_id: str, steps: list[Step], **fields) -> Run:
    graph = Graph()
    for step in steps:
        graph.add(step)
    return Run(id=run_id, graph=graph, **fields)


def completed_linear() -> Run:
    steps = [
        _step("s1", [], StepKind.think, {"text": "Plan the search strategy for Tine benchmarks."}),
        _step(
            "s2", ["s1"], StepKind.tool,
            {"name": "web_search", "arguments": {"q": "tine benchmark 2026"}},
            {"result": "3 results"},
            tool_info={"name": "web_search"}, cost=0.0008, duration=0.42,
        ),
        _step(
            "s3", ["s2"], StepKind.model,
            {"text": "Summarize search results"},
            {"text": "Tine shows 2.3x throughput vs baseline."},
            cost=0.012, duration=1.7, model="claude-sonnet-4-6",
            usage={"input": 1200, "output": 180, "total": 1380},
            billing={"known_subtotal_usd": 0.012},
        ),
        _step("s4", ["s3"], StepKind.done, {"text": "Tine is 2.3x faster."}),
    ]
    return _run(
        "demo-complete", steps,
        status=RunStatus.completed,
        model_info="claude-sonnet-4-6",
        system_prompt="You are a research assistant.",
        user_prompt="How does Tine compare to the baseline benchmark?",
        created_at=time.time() - 3600,
    )


def running_branched() -> Run:
    # root -> (tool_a, tool_b); both -> model (merge) -> pending think
    steps = [
        _step("r1", [], StepKind.think, {"text": "Gather two independent data sources."}),
        _step("r2a", ["r1"], StepKind.tool, {"name": "fetch_docs", "arguments": {"topic": "api"}},
              {"pages": 12}, tool_info={"name": "fetch_docs"}, cost=0.0003, duration=0.9),
        _step("r2b", ["r1"], StepKind.tool, {"name": "db_query", "arguments": {"table": "events"}},
              {"rows": 5421}, tool_info={"name": "db_query"}, cost=0.0001, duration=0.3),
        _step("r3", ["r2a", "r2b"], StepKind.model,
              {"text": "Merge docs + events into a timeline."},
              {"text": "Generated 3 clusters"},
              cost=0.008, duration=1.1, model="claude-haiku-4-5-20251001",
              usage={"input": 5400, "output": 320, "cache_read": 2100, "total": 7820}),
        _step("r4", ["r3"], StepKind.think, {"text": "Cross-reference docs with timeline…"}),
    ]
    return _run(
        "demo-running", steps,
        status=RunStatus.running,
        model_info="claude-sonnet-4-6",
        user_prompt="Build a unified timeline from docs + events table.",
        created_at=time.time() - 120,
    )


def paused_midflight() -> Run:
    steps = [
        _step("p1", [], StepKind.think, {"text": "Draft the refactor plan."}),
        _step("p2", ["p1"], StepKind.model, {"text": "Enumerate modules"},
              {"text": "8 modules identified"},
              cost=0.006, duration=0.8, model="claude-sonnet-4-6"),
        _step("p3", ["p2"], StepKind.tool, {"name": "read_file", "arguments": {"path": "core.py"}},
              {"content": "...truncated..."}, tool_info={"name": "read_file"}, duration=0.05),
    ]
    return _run(
        "demo-paused", steps,
        status=RunStatus.paused,
        model_info="claude-sonnet-4-6",
        user_prompt="Refactor the core module for readability.",
        created_at=time.time() - 600,
    )


def failed_deep() -> Run:
    # deeper chain ending in an error step (message in step.error, per opentine)
    steps = [
        _step("f1", [], StepKind.think, {"text": "Deploy the release pipeline."}),
        _step("f2", ["f1"], StepKind.tool,
              {"name": "run_ci", "arguments": {"branch": "main"}},
              {"result": "exit 0"}, tool_info={"name": "run_ci"}, duration=2.4),
        _step("f3", ["f2"], StepKind.tool,
              {"name": "publish", "arguments": {"target": "pypi"}},
              {"result": "exit 0"}, tool_info={"name": "publish"}, duration=1.1),
        _step("f4", ["f3"], StepKind.model,
              {"text": "Write release notes"},
              {"text": "Release notes drafted"},
              cost=0.004, duration=0.6, model="claude-sonnet-4-6"),
        _step("f5", ["f4"], StepKind.tool,
              {"name": "create_tag", "arguments": {"name": "v0.1.1"}},
              {"result": "failed"}, tool_info={"name": "create_tag"},
              error={"type": "GitError", "message": "tag already exists"}, duration=0.1),
        _step("f6", ["f5"], StepKind.error, {},
              error={"type": "ReleaseAborted",
                     "message": "git tag v0.1.1 already exists; aborting"}),
    ]
    return _run(
        "demo-failed", steps,
        status=RunStatus.failed,
        model_info="claude-opus-4-8",
        user_prompt="Cut release v0.1.1.",
        created_at=time.time() - 7200,
    )


def legacy_forked_run() -> Run:
    """A fork in the pre-0.4.0 shape: lineage keys only, no fork record.

    Still produced today whenever a caller passes an explicit ``new_run_id``,
    and the shape of every artifact written before 0.4.0, so the console has to
    keep rendering it.
    """
    steps = [
        _step("k1", [], StepKind.think, {"text": "Retry with different strategy."}),
        _step("k2", ["k1"], StepKind.model,
              {"text": "Reason about alternatives"},
              {"text": "Plan B: incremental rollout"},
              cost=0.005, duration=0.9, model="claude-sonnet-4-6"),
    ]
    return _run(
        "demo-fork-child", steps,
        status=RunStatus.running,
        model_info="claude-sonnet-4-6",
        user_prompt="Cut release v0.1.1.",
        created_at=time.time() - 60,
        metadata={"forked_from": "demo-failed", "fork_point": "f4"},
    )


def real_fork(source: Run) -> Run:
    """A genuine opentine 0.4.0 fork: derived id, recorded basis, verifiable.

    Its id is a content hash rather than a friendly name, which is exactly the
    point: the id commits to the fork act, so sibling forks of one step no
    longer collide.
    """
    reason = "retry the release with a slower, more careful model"
    forked = source.fork("f4", branch="experiment", intent={"reason": reason})
    forked.metadata["fork_reason"] = reason
    return forked


def main(runs_dir: Path | str = Path(".tine_runs")) -> None:
    runs_dir = Path(runs_dir)
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    runs_dir.mkdir(parents=True)

    failed = failed_deep()
    for run in [
        completed_linear(),
        running_branched(),
        paused_midflight(),
        failed,
        legacy_forked_run(),
        real_fork(failed),
    ]:
        path = runs_dir / f"{run.id}.tine"
        run.save(path)
        print(f"  wrote {path}  [{run.status.value}, {len(run.steps)} steps]")

    # Also drop a corrupt file to exercise the load-error path.
    (runs_dir / "zz-corrupt.tine").write_bytes(b"{ not valid json")
    print(f"  wrote {runs_dir / 'zz-corrupt.tine'}  [intentionally corrupt]")

    print(f"\nSeeded {runs_dir.resolve()}")


if __name__ == "__main__":
    main()
