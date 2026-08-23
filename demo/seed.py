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

#: `Step.provider` is post-0.7.2. Seeding it on an older opentine would raise,
#: so the demo records it only where the installed library has the field.
SUPPORTS_PROVIDER = "provider" in getattr(Step, "__dataclass_fields__", {})


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
    causal: list[str] | None = None,
    provider: str = "",
) -> Step:
    fields = dict(
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
        causal_ids=list(causal or []),
    )
    if provider and SUPPORTS_PROVIDER:
        fields["provider"] = provider
    return Step(**fields)


def _run(run_id: str, steps: list[Step], **fields) -> Run:
    graph = Graph()
    for step in steps:
        graph.add(step)
    return Run(id=run_id, graph=graph, **fields)


def _transcript(*turns: dict) -> list[dict]:
    """The shape opentine's runtime records: role/content, plus step_id on the
    turns that produced a step and name on tool results."""
    return list(turns)


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
        # A live agent run carries the conversation that produced the graph.
        transcript=_transcript(
            {"role": "user",
             "content": "How does Tine compare to the baseline benchmark?"},
            {"step_id": "s1", "role": "assistant",
             "content": "I'll search for published benchmark numbers first."},
            {"step_id": "s2", "role": "tool", "name": "web_search",
             "content": "3 results: throughput comparison, latency study, cost analysis"},
            {"step_id": "s3", "role": "assistant",
             "content": "Tine shows 2.3x throughput vs baseline."},
            {"step_id": "s4", "role": "assistant",
             "content": "Tine is 2.3x faster."},
        ),
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
    # nonce="" derives the id from the fork act alone, so reseeding rewrites the
    # same file instead of leaving a new one behind every time. It is also the
    # case worth demonstrating: a reproducible fork id is what makes the console
    # refuse to overwrite an existing artifact rather than silently replacing it.
    forked = source.fork("f4", branch="experiment", intent={"reason": reason}, nonce="")
    forked.metadata["fork_reason"] = reason
    return forked


def causal_run() -> Run:
    """A run whose graph is wider than its parent lineage.

    ``causal_ids`` names a step that was required but is not a parent — the
    shape a run exported out of a v3 repository carries. opentine's fork keeps
    that closure, so the console has to draw it: otherwise the picture and the
    fork disagree about what the run is.
    """
    steps = [
        _step("c1", [], StepKind.think, {"text": "Read the incident report."}),
        _step(
            "c2", ["c1"], StepKind.tool,
            {"name": "read_file", "arguments": {"path": "postmortem.md"}},
            {"content": "root cause: retry storm"},
            tool_info={"name": "read_file"}, duration=0.2,
        ),
        _step(
            "c3", ["c1"], StepKind.tool,
            {"name": "metrics", "arguments": {"window": "24h"}},
            {"p99_ms": 4100}, tool_info={"name": "metrics"}, duration=0.4,
        ),
        # c4's parent is c3, but it could not have been written without c2:
        # that is a causal edge, and a fork from c4 keeps c2 as well.
        _step(
            "c4", ["c3"], StepKind.model,
            {"text": "Write the mitigation plan."},
            {"text": "Cap retries at 3 with jittered backoff."},
            cost=0.009, duration=1.4, model="claude-opus-5", provider="anthropic",
            usage={"input": 3400, "output": 260, "total": 3660},
            billing={"known_subtotal_usd": 0.009, "status": "complete"},
            causal=["c2"],
        ),
        _step("c5", ["c4"], StepKind.done, {"text": "Mitigation plan ready."}),
    ]
    return _run(
        "demo-causal", steps,
        status=RunStatus.completed,
        model_info="claude-opus-5",
        user_prompt="Why did the checkout service melt down, and what do we change?",
        created_at=time.time() - 1800,
    )


def imported_unpriced() -> Run:
    """What an imported trace looks like: real token usage, no recorded cost.

    Every step here would sum to $0.00, which is not the same claim as "this run
    was free". The console says "no cost recorded" and offers to price it from
    the catalog instead.
    """
    steps = [
        _step(
            "i1", [], StepKind.model,
            {"text": "Draft the migration checklist."},
            {"text": "1. freeze writes 2. dual-write 3. backfill 4. cut over"},
            duration=2.2, model="claude-opus-5", provider="anthropic",
            usage={"input": 1000, "output": 500, "total": 1500},
            # A real timestamp matters here: opentine prices each step against
            # the rate card in force on the day it ran, so a step stamped 0
            # (which is what a thin OTel span imports as) prices as `unknown`.
            ts=time.time() - 900,
        ),
        _step(
            "i2", ["i1"], StepKind.model,
            {"text": "Estimate the backfill window."},
            {"text": "About 6 hours at current write volume."},
            duration=1.1, model="gpt-5.6", provider="openai",
            usage={"input": 800, "output": 220, "total": 1020},
            ts=time.time() - 880,
        ),
        _step("i3", ["i2"], StepKind.done, {"text": "Checklist and window agreed."}),
    ]
    return _run(
        "demo-imported", steps,
        status=RunStatus.completed,
        model_info="claude-opus-5",
        user_prompt="Plan the datastore migration.",
        created_at=time.time() - 900,
        metadata={"import": {"format": "otel-json", "source": "collector-export.json"}},
    )


#: Not a secret: a fixed demo key so `tine-gui` can be shown verifying a real
#: signature. Point OPENTINE_GUI_HMAC_KEY at it (or at a file holding it) to see
#: the trust panel say "verified" rather than "no key".
DEMO_HMAC_KEY = b"opentine-gui-demo-signing-key-0001"


def seed_repository(root: Path) -> Path:
    """A small v3 repository, so the read-only repository mode has something to show."""
    from opentine.repo import Repo

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    repo = Repo.init(root)
    main_run = completed_linear()
    main_run.run_id = "repo-main"
    repo.put_run(main_run, ref="heads/main")
    branch_run = causal_run()
    branch_run.run_id = "repo-experiment"
    repo.put_run(branch_run, ref="heads/experiment")
    return root


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
        causal_run(),
        imported_unpriced(),
    ]:
        path = runs_dir / f"{run.id}.tine"
        run.save(path)
        print(f"  wrote {path}  [{run.status.value}, {len(run.steps)} steps]")

    # A signed run, so the trust panel has something to verify. opentine refuses
    # to sign a run that has not finished, which is why this one is completed.
    signed = completed_linear()
    signed.run_id = "demo-signed"
    signed_path = runs_dir / "demo-signed.tine"
    signed.save(signed_path, sign_key=DEMO_HMAC_KEY, key_id="demo", signer="demo@opentine")
    print(f"  wrote {signed_path}  [signed with the demo key]")

    # Also drop a corrupt file to exercise the load-error path.
    (runs_dir / "zz-corrupt.tine").write_bytes(b"{ not valid json")
    print(f"  wrote {runs_dir / 'zz-corrupt.tine'}  [intentionally corrupt]")

    repo_dir = seed_repository(runs_dir.parent / ".tine_repo")
    print(f"  wrote {repo_dir}  [v3 repository: heads/main, heads/experiment]")

    print(f"\nSeeded {runs_dir.resolve()}")
    print(f"  tine-gui {runs_dir}          # the .tine directory")
    print(f"  tine-gui {repo_dir}   # the same console over a v3 repository")
    print("  OPENTINE_GUI_HMAC_KEY=opentine-gui-demo-signing-key-0001 tine-gui"
          "   # verifies demo-signed.tine")


if __name__ == "__main__":
    main()
