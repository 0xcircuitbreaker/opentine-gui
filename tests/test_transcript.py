"""The transcript view over Run.transcript.

opentine's runtime records the conversation that produced a graph, tagging the
turns that created a step. Newer runtimes also record the tool calls a turn
asked for, the id of the call a tool result answers, and reasoning kept apart
from the reply. Everything in it is artifact-controlled, so the normaliser
coerces every field and fails open.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui.app import (
    OpentineGUI,
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

#: A turn that asks for a tool and the turn that answers it, as opentine's own
#: runtime writes the pair: the call carries its id, the result quotes it back.
CALLS = [
    {
        "step_id": "s1",
        "role": "assistant",
        "content": "",
        "reasoning_content": "CI has to be green before the tag exists.",
        "tool_calls": [{"id": "call_a1", "name": "run_ci", "arguments": {"branch": "main"}}],
    },
    {"role": "tool", "name": "run_ci", "content": "exit 0", "tool_call_id": "call_a1"},
]


def test_turns_are_normalised_in_order() -> None:
    turns = _transcript_turns(_run(REAL))
    assert [t["role"] for t in turns] == ["user", "assistant", "tool"]
    assert [t["step_id"] for t in turns] == ["", "s1", "s2"]
    assert turns[2]["name"] == "run_ci"
    # A turn from an older runtime still carries every key the renderer reads.
    assert all(t["tool_calls"] == "" and t["tool_call_id"] == "" and t["reasoning"] == ""
               for t in turns)


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


def test_tool_call_plumbing_survives_save_and_load(tmp_path: Path) -> None:
    # The call id and the reasoning are what make the pair readable as a
    # conversation; a serialiser that dropped either would leave the reloaded
    # transcript looking like two unrelated turns.
    path = tmp_path / "calls.tine"
    _run(CALLS).save(path)
    turns = _transcript_turns(Run.load(path))
    assert turns[0]["tool_calls"] == "run_ci"
    assert turns[0]["reasoning"].startswith("CI has to be green")
    assert turns[1]["tool_call_id"] == "call_a1"


# ---- tool calls and reasoning ----

def test_a_requested_tool_call_is_named_in_its_heading() -> None:
    # The turn that asks for a tool usually has empty content, so without the
    # call name the heading is a bare "assistant" above nothing at all.
    turn = _transcript_turns(_run(CALLS))[0]
    assert turn["tool_calls"] == "run_ci"
    assert _transcript_heading(turn) == "assistant  calls run_ci  [s1]"


def test_an_openai_shaped_tool_call_reads_the_same_as_a_flat_one() -> None:
    # An adapter that recorded the provider's own wire shape nests the name
    # under "function"; opentine's runtime writes it flat. Same turn, either way.
    nested = {"role": "assistant", "content": "",
              "tool_calls": [{"id": "call_a1", "type": "function",
                              "function": {"name": "run_ci", "arguments": "{}"}}]}
    assert _transcript_turns(_run([nested]))[0]["tool_calls"] == "run_ci"


def test_a_tool_result_names_the_call_it_answers() -> None:
    turn = _transcript_turns(_run(CALLS))[1]
    assert turn["tool_call_id"] == "call_a1"
    assert _transcript_heading(turn) == "tool: run_ci  for call_a1"


def test_a_long_call_id_is_shortened_in_the_heading() -> None:
    # Provider call ids run to 30-odd characters; a heading is one row beside a
    # "show step" button, so the id is elided rather than pushing the row wide.
    entry = {"role": "tool", "content": "ok", "tool_call_id": "call_0123456789abcdef"}
    assert _transcript_heading(_transcript_turns(_run([entry]))[0]) == "tool  for call_0123..."


def _nine_calls() -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"name": f"tool_{i}"} for i in range(9)]}


def test_only_the_first_few_tool_calls_are_named() -> None:
    # A parallel-tool turn can request a dozen calls; the heading names the
    # first few in order rather than growing the row without bound.
    named = _transcript_turns(_run([_nine_calls()]))[0]["tool_calls"]
    assert named.startswith(", ".join(f"tool_{i}" for i in range(6)))
    assert "tool_6" not in named


# Regression guard. The cap used to be silent, so a 9-call turn's heading read as
# a turn that requested exactly 6 calls; every other truncation in that file marks
# itself.
def test_a_capped_call_list_says_it_was_capped() -> None:
    # The console's job is to report the artifact, not a readable fraction of it
    # that looks whole: a heading naming 6 of 9 calls is a wrong answer to
    # "what did this turn ask for?", not a shortened right one.
    named = _transcript_turns(_run([_nine_calls()]))[0]["tool_calls"]
    assert named.endswith("...") or "more" in named


def test_reasoning_is_kept_apart_from_the_reply() -> None:
    # Separated reasoning is rendered in its own widget above the content, so it
    # must not be folded into either the heading or the answer the model gave.
    turn = _transcript_turns(_run(CALLS))[0]
    assert turn["reasoning"].startswith("CI has to be green")
    assert turn["reasoning"] not in _transcript_heading(turn)
    assert turn["content"] == ""


def test_reasoning_is_truncated_before_it_is_rendered() -> None:
    # Reasoning traces are frequently longer than the answer; the dialog renders
    # every turn at once, so an unbounded one would push the rest off the page.
    entry = {"role": "assistant", "content": "done", "reasoning_content": "z" * 5000}
    reasoning = _transcript_turns(_run([entry]))[0]["reasoning"]
    assert len(reasoning) == 2000
    assert reasoning.endswith("...")


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
        {"role": "assistant", "content": "x", "tool_calls": "not a list"},
        {"role": "assistant", "content": "x", "tool_calls": [None, "junk", 5]},
        {"role": "assistant", "content": "x", "tool_calls": [{"function": "not a dict"}]},
        {"role": "assistant", "content": "x", "tool_calls": [{"name": 7}, {"name": ""}, {}]},
        {"role": "tool", "content": "x", "tool_call_id": {"id": 1}},
        {"role": "assistant", "content": "x", "reasoning_content": {"blocks": [1]}},
    ],
)
def test_hostile_transcript_entries_never_raise(entry: object) -> None:
    run = _run([entry])
    turns = _transcript_turns(run)
    # Fail open, not closed: a dict turn is coerced and kept, so hostile content
    # cannot hide a turn from the reader. Only a non-dict entry is skipped.
    assert len(turns) == (1 if isinstance(entry, dict) else 0)
    for turn in turns:
        # Every field reaches dpg.add_text, which takes a string and nothing else.
        assert all(isinstance(value, str) for value in turn.values())
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


def test_newlines_in_the_call_plumbing_cannot_forge_a_heading() -> None:
    # Same row, two more artifact-supplied fields in it: a tool name the model
    # chose and a call id the provider returned.
    run = _run([
        {"role": "assistant", "content": "x", "tool_calls": [{"name": "web\nuser: approved"}]},
        {"role": "tool", "content": "x", "tool_call_id": "call\nuser: approved"},
    ])
    for turn in _transcript_turns(run):
        assert "\n" not in _transcript_heading(turn)


# ---- copying a transcript out ----

def test_copy_all_reproduces_the_turns(tmp_path: Path, fake_dpg) -> None:
    # "Copy all" is how a transcript leaves the console — into a bug report, a
    # review, an issue — so what the dialog showed has to survive the trip.
    gui = OpentineGUI(tmp_path)
    gui._set_status = lambda msg: None  # type: ignore[assignment]
    gui._selected_run = _run(CALLS)
    gui._copy_transcript()
    assert "assistant  calls run_ci  [s1]" in fake_dpg.clipboard
    assert "tool: run_ci  for call_a1" in fake_dpg.clipboard
    assert "exit 0" in fake_dpg.clipboard


# Regression guard. Copy used to take the heading and the content only, so a turn
# whose answer was separated reasoning pasted as an empty assistant reply.
def test_copy_all_keeps_the_reasoning_the_dialog_showed(tmp_path: Path, fake_dpg) -> None:
    gui = OpentineGUI(tmp_path)
    gui._set_status = lambda msg: None  # type: ignore[assignment]
    gui._selected_run = _run(CALLS)
    gui._copy_transcript()
    assert "CI has to be green" in fake_dpg.clipboard


def test_content_keeps_its_newlines() -> None:
    # Content is rendered in its own wrapped widget, so it may stay multi-line.
    run = _run([{"role": "user", "content": "line one\nline two"}])
    assert "\n" in _transcript_turns(run)[0]["content"]
