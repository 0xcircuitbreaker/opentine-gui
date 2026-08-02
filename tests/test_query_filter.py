"""The run filter's field grammar, borrowed from opentine's own query language.

The grammar engages only when the text carries a recognised field prefix, so a
plain multi-word search keeps the substring behaviour users already rely on.
"""

from __future__ import annotations

import time

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import app
from opentine_gui.app import _query_error, _run_matches_filter


def _run(run_id: str, *, status=RunStatus.completed, model="claude-sonnet-4-6",
         cost=0.0, prompt="hi", created=None, tags=()) -> Run:
    graph = Graph()
    graph.add(
        Step(id="s1", parent_ids=[], kind=StepKind.model, inputs={"text": prompt},
             model_info=model, cost=cost)
    )
    run = Run(id=run_id, graph=graph, status=status, model_info=model,
              user_prompt=prompt, created_at=created or time.time())
    for tag in tags:
        run.add_tag(tag)
    return run


@pytest.fixture
def corpus() -> list[Run]:
    old = time.time() - 86_400 * 30
    return [
        _run("alpha", status=RunStatus.failed, model="claude-opus-4-8", cost=0.05,
             prompt="cut the release", tags=("bug",)),
        _run("beta", status=RunStatus.completed, model="claude-sonnet-4-6", cost=0.001,
             prompt="build a timeline"),
        _run("gamma", status=RunStatus.running, model="claude-sonnet-4-6", cost=0.5,
             prompt="cut the release again", created=old, tags=("bug", "urgent")),
    ]


def _ids(corpus: list[Run], query: str) -> list[str]:
    return sorted(r.id for r in corpus if _run_matches_filter(r, query))


# ---- field grammar ----

def test_status_is_an_exact_match(corpus: list[Run]) -> None:
    assert _ids(corpus, "status:failed") == ["alpha"]
    assert _ids(corpus, "status:running") == ["gamma"]


def test_model_is_a_substring_match(corpus: list[Run]) -> None:
    # Mirrors opentine's match_entry: `model` is a case-insensitive substring.
    assert _ids(corpus, "model:opus") == ["alpha"]
    assert _ids(corpus, "model:OPUS") == ["alpha"]
    assert _ids(corpus, "model:claude") == ["alpha", "beta", "gamma"]


def test_tags_must_all_be_present(corpus: list[Run]) -> None:
    assert _ids(corpus, "tag:bug") == ["alpha", "gamma"]
    assert _ids(corpus, "tag:bug tag:urgent") == ["gamma"]
    assert _ids(corpus, "tag:nonexistent") == []


def test_cost_bounds(corpus: list[Run]) -> None:
    assert _ids(corpus, "cost:>0.01") == ["alpha", "gamma"]
    assert _ids(corpus, "cost:<0.01") == ["beta"]
    assert _ids(corpus, "cost:0.01..0.1") == ["alpha"]


def test_date_bounds(corpus: list[Run]) -> None:
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86_400))
    assert _ids(corpus, f"after:{yesterday}") == ["alpha", "beta"]
    assert _ids(corpus, f"before:{yesterday}") == ["gamma"]


def test_fields_combine_as_and(corpus: list[Run]) -> None:
    assert _ids(corpus, "status:running model:sonnet") == ["gamma"]
    assert _ids(corpus, "status:failed model:sonnet") == []


def test_free_terms_alongside_fields_must_all_appear(corpus: list[Run]) -> None:
    assert _ids(corpus, "model:claude release") == ["alpha", "gamma"]
    assert _ids(corpus, "model:claude release timeline") == []


# ---- the substring path is preserved ----

def test_plain_text_stays_a_substring_search(corpus: list[Run]) -> None:
    # The behaviour users already have: a multi-word search is the literal
    # phrase, not an AND of terms.
    assert _ids(corpus, "cut the release") == ["alpha", "gamma"]
    assert _ids(corpus, "release cut") == [], "plain text is a phrase, not AND-of-terms"


def test_an_empty_filter_matches_everything(corpus: list[Run]) -> None:
    assert len(_ids(corpus, "")) == 3


def test_text_without_a_field_prefix_never_engages_the_grammar(corpus: list[Run]) -> None:
    # "status" alone is not "status:", so this stays a substring search.
    assert _ids(corpus, "status") == []


# ---- malformed queries ----

@pytest.mark.parametrize("query", ["cost:abc", "after:nonsense", "before:13-13-13"])
def test_a_malformed_query_falls_back_instead_of_raising(
    corpus: list[Run], query: str
) -> None:
    assert _ids(corpus, query) == []  # substring of a nonsense string
    assert _query_error(query), "the user must be told why it matched nothing"


def test_a_valid_query_reports_no_error(corpus: list[Run]) -> None:
    assert _query_error("status:failed") == ""
    assert _query_error("plain words") == ""
    assert _query_error("") == ""


def test_the_grammar_degrades_without_opentine_support(corpus: list[Run]) -> None:
    # The declared floor is opentine 0.4.0; the import is guarded.
    with pytest.MonkeyPatch.context() as m:
        m.setattr(app, "parse_query", None)
        assert _ids(corpus, "status:failed") == []  # substring, finds nothing
        assert _query_error("cost:abc") == ""


def test_hostile_run_data_cannot_break_the_filter(corpus: list[Run]) -> None:
    # _run_matches_filter runs inside the run-table render: raising would blank
    # the whole list on the refresh loop.
    class Hostile:
        id = "x"

        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert _run_matches_filter(Hostile(), "status:failed") is False


def test_no_index_file_is_written(tmp_path, corpus: list[Run]) -> None:
    # RunIndex.search would write index.json into the user's runs directory;
    # evaluating the parsed Query ourselves keeps the console read-only.
    for run in corpus:
        run.save(tmp_path / f"{run.id}.tine")
    before = {p.name for p in tmp_path.iterdir()}
    for run in corpus:
        _run_matches_filter(run, "status:failed model:opus cost:>0.01")
    assert {p.name for p in tmp_path.iterdir()} == before
