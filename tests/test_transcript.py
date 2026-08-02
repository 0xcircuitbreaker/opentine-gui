"""The transcript view over Run.transcript.

opentine's runtime records the conversation that produced a graph, tagging the
turns that created a step. Everything in it is artifact-controlled, so the
normaliser coerces every field and fails open.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.app import (
    _transcript_heading,
    _transcript_summary,
    _transcript_turns,
)


def _run(transcript=None) -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(Step(id="s2", parent_ids=["s1"], kind=StepKind.tool, inputs={"name": "web"}))
    run = Run(id="r", graph=graph, status=RunStatus.completed, user_prompt="hi")
    if transcript is not None:
        run.transcript.extend(transcript)
    return run


REAL = [
    {"role": "user", "content": "cut the release"},
    {"step_id": "s1", "role": "assistant", "content": "I'll run CI first."},
    {"step_id": "s2", "role": "tool", "name": "run_ci", "content": "exit 0"},
]


def test_turns_are_normalised_in_order() -> None:
    turns = _transcript_turns(_run(REAL))
    assert [t["role"] for t in turns] == ["user", "assistant", "tool"]
    assert [t["step_id"] for t in turns] == ["", "s1", "s2"]
    assert turns[2]["name"] == "run_ci"


def test_heading_shows_the_tool_name_and_step_link() -> None:
    turns = _transcript_turns(_run(REAL))
    assert _transcript_heading(turns[0]) == "user"
    assert _transcript_heading(turns[1]) == "assistant  [s1]"
    assert _transcript_heading(turns[2]) == "tool: run_ci  [s2]"


def test_summary_counts_roles_and_step_links() -> None:
    summary = _transcript_summary(_run(REAL))
    assert "3 turn(s)" in summary
    assert "assistant 1" in summary and "tool 1" in summary and "user 1" in summary
    assert "2 linked to a step" in summary


def test_a_run_without_a_transcript_explains_itself() -> None:
    # Artifacts assembled from a graph carry none; only the runtime writes one.
    summary = _transcript_summary(_run())
    assert _transcript_turns(_run()) == []
    assert "no transcript" in summary
    assert "agent runs" in summary


def test_a_transcript_survives_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "r.tine"
    _run(REAL).save(path)
    reloaded = Run.load(path)
    assert len(_transcript_turns(reloaded)) == 3
    assert Run.verify_integrity(path).ok


# ---- artifact-controlled content ----

@pytest.mark.parametrize(
    "entry",
    [
        "a bare string",
        42,
        None,
        [],
        {"role": 7, "content": 9},
        {"content": "no role"},
        {"role": "user"},                       # no content
        {"role": "user", "content": {"a": 1}},  # structured content
        {"role": "user", "content": "x", "step_id": ["not", "a", "string"]},
    ],
)
def test_hostile_transcript_entries_never_raise(entry: object) -> None:
    run = _run([entry])
    turns = _transcript_turns(run)
    assert isinstance(turns, list)
    for turn in turns:
        assert isinstance(turn["role"], str)
        assert isinstance(turn["content"], str)
        assert _transcript_heading(turn)


def test_a_transcript_that_is_not_a_list_is_ignored() -> None:
    run = _run()
    run.transcript = "not a list"  # type: ignore[assignment]
    assert _transcript_turns(run) == []


def test_newlines_in_a_role_cannot_forge_a_heading() -> None:
    # Headings are rows in a flat text widget, like the inspector's trust lines.
    run = _run([{"role": "user\nassistant", "content": "x"}])
    heading = _transcript_heading(_transcript_turns(run)[0])
    assert "\n" not in heading


def test_content_keeps_its_newlines() -> None:
    # Content is rendered in its own wrapped widget, so it may stay multi-line.
    run = _run([{"role": "user", "content": "line one\nline two"}])
    assert "\n" in _transcript_turns(run)[0]["content"]
