"""The console's widgets: what it builds, and what it does to them.

Every test here drives the recording stand-in from tests/fakedpg.py, which the
autouse `fake_dpg` fixture installs in place of Dear PyGui, and builds the whole
UI through `gui_factory`. Nothing may reach the real library: without a graphics
context it segfaults the interpreter, so a stray `dpg.` call would take the
suite down rather than fail one test. Nothing here starts the loader thread or a
frame loop either — `_scan_now` and the `_apply_*` methods are what a frame
would have called, so a test can step the console one beat at a time.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import app
from opentine_gui.sources import RunEntry, Snapshot

# ------------------------------------------------------------------ artifacts


def _run(run_id: str = "alpha", *, status: RunStatus = RunStatus.completed) -> Run:
    """think -> tool -> done, with a causal edge and nothing priced.

    s1 -> s3 is causal, not lineage: opentine's fork keeps those edges, so the
    graph has to draw them, and a parent-only picture is a strict subgraph of
    what a fork would take. Nothing is priced, which is what an imported trace
    looks like and what the cost column has to stay honest about.
    """
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan the work"}))
    graph.add(
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.tool,
            inputs={"name": "grep"},
            tool_info={"name": "grep"},
            duration=1.5,
        )
    )
    graph.add(
        Step(
            id="s3",
            parent_ids=["s2"],
            causal_ids=["s1"],
            kind=StepKind.done,
            inputs={},
            outputs={"answer": "shipped"},
        )
    )
    run = Run(
        id=run_id,
        graph=graph,
        status=status,
        model_info="claude-opus-4",
        user_prompt="ship the release",
    )
    run.transcript.extend(
        [
            {"role": "user", "content": "ship the release"},
            {"step_id": "s2", "role": "assistant", "content": "grepping the changelog"},
        ]
    )
    return run


def _priced_run(run_id: str = "beta", *, cost: float = 0.25) -> Run:
    """A model invocation opentine priced, and recorded a provider for."""
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "decide"}))
    graph.add(
        Step(
            id="s2",
            parent_ids=["s1"],
            kind=StepKind.model,
            inputs={"text": "answer this"},
            outputs={"text": "42"},
            cost=cost,
            # 0.7.2 has no Step.provider, so a real artifact carries the identity
            # only in the rate card opentine's adapter chose.
            billing={"rate_card_id": "anthropic:claude-sonnet-4"},
        )
    )
    return Run(
        id=run_id,
        graph=graph,
        status=RunStatus.running,
        model_info="claude-sonnet-4",
        user_prompt="answer this",
    )


def _write(runs_dir: Path, run: Run) -> Path:
    path = runs_dir / f"{run.id}.tine"
    run.save(path)
    return path


def _chain(run_id: str, steps: int) -> Run:
    """A straight-line run of `steps` steps, for the caps."""
    graph = Graph()
    parents: list[str] = []
    for index in range(steps):
        step_id = f"s{index}"
        graph.add(
            Step(id=step_id, parent_ids=parents, kind=StepKind.think, inputs={"text": step_id})
        )
        parents = [step_id]
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="go")


def _install(gui, entries: list[RunEntry], **snapshot_kwargs) -> None:
    """Hand the console a snapshot without going through a directory scan.

    Used where the point is a widget behaviour at a scale (500 rows, 400 nodes)
    that writing that many artifacts to disk would only slow down.
    """
    gui._snapshot = Snapshot(root=gui._runs_dir, entries=entries, **snapshot_kwargs)
    gui._entries = entries
    gui._errors = list(gui._snapshot.errors)


# -------------------------------------------------------------- fake readers


def _rows(fake) -> list:
    return fake.children_of("run_table", slot=1)


def _cells(fake, row) -> list[str]:
    """One row as text. The id is a selectable's label; the rest are values."""
    cells = []
    for cell in fake.children_of(row):
        config = fake.config(cell)
        cells.append(str(config["label"] if "label" in config else fake.value(cell)))
    return cells


def _row_ids(fake) -> list[str]:
    return [_cells(fake, row)[0] for row in _rows(fake)]


def _row_for(fake, run_id: str):
    row = next((r for r in _rows(fake) if _cells(fake, r)[0] == run_id), None)
    assert row is not None, f"{run_id} is not in the table: {_row_ids(fake)}"
    return row


def _nodes(fake) -> dict[str, int]:
    """step id -> node item, for whatever is in the editor right now."""
    return {str(fake.config(node).get("user_data")): node for node in fake.find("node")}


def _links(fake) -> list[tuple[str, str, int]]:
    """(cause, effect, theme) per link, resolved back through the attributes."""
    owner: dict[int, str] = {}
    for node in fake.find("node"):
        for attribute in fake.children_of(node):
            owner[attribute] = str(fake.config(node).get("user_data"))
    drawn = []
    for link in fake.find("node_link"):
        config = fake.config(link)
        drawn.append(
            (owner.get(config["first"], "?"), owner.get(config["second"], "?"), config.get("theme"))
        )
    return drawn


def _transcript_turns_drawn(fake) -> int:
    """How many turns the dialog actually built: one spacer closes each turn."""
    spacers = set(fake.find("spacer"))
    return sum(1 for child in fake.children_of("transcript_body") if child in spacers)


def _fire(fake, tag: str, app_data=None):
    """Call a widget's own callback with the arity Dear PyGui would give it.

    In manual callback management mode the console drains the queue with
    `dpg.run_callbacks`, which inspects each callback and passes only as many of
    (sender, app_data, user_data) as it declares. `fake_dpg.invoke` always
    passes three, so the console's two-parameter callbacks need this instead.
    """
    config = fake.config(tag)
    args = [tag, app_data, config.get("user_data")]
    callback = config["callback"]
    return callback(*args[: len(inspect.signature(callback).parameters)])


def _press(fake, key: int) -> None:
    """Fire the console's own handler for a key, the way the loop would.

    Key handlers take no arguments, so `fake_dpg.invoke` — which always passes
    sender, app_data and user_data — cannot be used on them.
    """
    handlers = [h for h in fake.find("key_press_handler") if fake.config(h)["key"] == key]
    assert len(handlers) == 1, f"expected one handler bound to key {key}, found {len(handlers)}"
    fake.config(handlers[0])["callback"]()


def _inspect_button(fake, step_id: str):
    found = [
        item
        for item in fake.find("button")
        if fake.config(item).get("label") == "inspect"
        and fake.config(item).get("user_data") == step_id
    ]
    assert len(found) == 1, f"no inspect button for {step_id}"
    return found[0]


# ------------------------------------------------------------------- build


def test_building_the_console_creates_every_panel_its_callbacks_address(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # Every one of these tags is written to by a callback somewhere; a rename
    # in _build_ui would otherwise only show up as a SystemError at runtime.
    gui_factory(tmp_path)
    for tag in (
        "run_table",
        "detail_text",
        "step_text",
        "dag_editor",
        "status_bar",
        "message_log",
        "run_filter",
        "step_filter",
        "run_summary",
        "dag_summary",
        "source_badge",
    ):
        assert fake_dpg.exists(tag), f"{tag} was not built"


def test_the_console_starts_with_both_inspectors_asking_for_a_selection(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run())
    gui_factory(tmp_path)
    assert fake_dpg.value("detail_text") == "Select a run"
    assert fake_dpg.value("step_text") == "Select a step in the DAG"
    assert fake_dpg.find("node") == []


# --------------------------------------------------------------- run table


def test_the_table_holds_one_row_per_visible_run(tmp_path: Path, gui_factory, fake_dpg) -> None:
    for run_id in ("alpha", "beta", "gamma"):
        _write(tmp_path, _run(run_id))
    gui_factory(tmp_path)
    assert sorted(_row_ids(fake_dpg)) == ["alpha", "beta", "gamma"]


def test_a_row_carries_the_id_status_steps_cost_and_age(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # Five columns, not six. A sixth elided every header to "Stat…"/"A…" in a
    # sidebar this wide; the model is in the run inspector and in the row's own
    # tooltip, which is where it earns its space.
    _write(tmp_path, _priced_run("beta", cost=0.25))
    gui = gui_factory(tmp_path)
    run_id, status, steps, cost, age = _cells(fake_dpg, _row_for(fake_dpg, "beta"))
    assert run_id == "beta"
    assert status == "running"
    assert steps == "2"
    assert cost == "$0.2500"
    assert age  # a formatted age, whatever the clock says
    entry = gui._snapshot.entry("beta")
    assert "claude-sonnet-4" in gui._row_tooltip(entry)


def test_a_run_nothing_priced_shows_a_dash_rather_than_a_zero_cost(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # $0.0000 in this column would state a spend the artifact never claimed;
    # an imported trace records no cost at all.
    _write(tmp_path, _run("alpha"))
    gui_factory(tmp_path)
    assert _cells(fake_dpg, _row_for(fake_dpg, "alpha"))[3] == "-"


def test_only_the_selected_row_is_marked_selected(tmp_path: Path, gui_factory, fake_dpg) -> None:
    for run_id in ("alpha", "beta", "gamma"):
        _write(tmp_path, _run(run_id))
    gui = gui_factory(tmp_path)
    gui._select_run("beta")
    marked = {
        _cells(fake_dpg, row)[0]: fake_dpg.value(fake_dpg.children_of(row)[0])
        for row in _rows(fake_dpg)
    }
    assert marked == {"alpha": False, "beta": True, "gamma": False}


def test_a_run_list_past_the_cap_says_how_many_it_left_out(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # The table is rebuilt from scratch every render, so it is bounded; the row
    # that says so is the only thing standing between a big directory and a
    # console that silently shows part of it.
    gui = gui_factory(tmp_path)
    entries = [
        RunEntry(key=f"r{index:04d}", run=_run(f"r{index:04d}"), mtime=float(index))
        for index in range(app.MAX_TABLE_ROWS + 1)
    ]
    _install(gui, entries)
    gui._render_run_table()
    rows = _rows(fake_dpg)
    assert len(rows) == app.MAX_TABLE_ROWS + 1  # the drawn rows, plus the notice
    assert _cells(fake_dpg, rows[-1])[0] == "...1 more not shown"


def test_an_empty_directory_says_so_instead_of_showing_an_empty_table(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    gui_factory(tmp_path)
    assert _row_ids(fake_dpg) == ["No .tine runs here yet"]
    assert fake_dpg.config("err_header")["show"] is False


def test_a_filter_that_matches_nothing_says_so(tmp_path: Path, gui_factory, fake_dpg) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._run_filter = "status:failed"
    gui._render_run_table()
    assert _row_ids(fake_dpg) == ["No runs match this filter"]


def test_a_missing_runs_directory_is_a_problem_not_an_empty_list(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # A typo'd path and an empty directory used to look identical ("0 runs").
    gui_factory(tmp_path / "not-here")
    assert _row_ids(fake_dpg) == ["Nothing loaded - see the problems below"]
    assert fake_dpg.config("err_header")["show"] is True
    assert "no such directory" in fake_dpg.value("err_text")


def test_a_file_that_will_not_parse_is_listed_without_hiding_the_runs_that_did(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    (tmp_path / "junk.tine").write_text("this is not a .tine file")
    gui_factory(tmp_path)
    assert _row_ids(fake_dpg) == ["alpha"]
    assert "junk.tine" in fake_dpg.value("err_text")
    assert fake_dpg.value("err_header") == "1 load error(s)"


def test_the_load_error_panel_clears_once_the_bad_file_is_gone(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    junk = tmp_path / "junk.tine"
    junk.write_text("this is not a .tine file")
    gui = gui_factory(tmp_path)
    junk.unlink()
    gui._scan_now()
    assert fake_dpg.config("err_header")["show"] is False
    assert fake_dpg.value("err_text") == ""


# ---------------------------------------------------------------- selection


def test_clicking_a_row_fills_the_run_inspector(tmp_path: Path, gui_factory, fake_dpg) -> None:
    _write(tmp_path, _run("alpha"))
    _write(tmp_path, _run("beta"))
    gui = gui_factory(tmp_path)
    fake_dpg.invoke(fake_dpg.children_of(_row_for(fake_dpg, "beta"))[0])
    assert gui._selected_key == "beta"
    assert "Run: beta" in fake_dpg.value("detail_text")
    assert fake_dpg.value("step_text") == "Select a step in the DAG"


def test_a_nodes_inspect_button_fills_the_step_inspector(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    fake_dpg.invoke(_inspect_button(fake_dpg, "s2"))
    assert gui._selected_step is not None and gui._selected_step.id == "s2"
    detail = fake_dpg.value("step_text")
    assert "ID: s2" in detail
    assert "Kind: tool" in detail


def test_selecting_a_run_enables_the_actions_its_state_allows(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha", status=RunStatus.running))
    _write(tmp_path, _run("halted", status=RunStatus.paused))
    _write(tmp_path, _run("done", status=RunStatus.completed))
    gui = gui_factory(tmp_path)

    gui._select_run("alpha")
    assert fake_dpg.config("btn_pause")["enabled"] is True
    assert fake_dpg.config("btn_resume")["enabled"] is False  # it is not paused
    assert fake_dpg.config("btn_fork")["enabled"] is False  # no step selected yet
    fake_dpg.invoke(_inspect_button(fake_dpg, "s2"))
    assert fake_dpg.config("btn_fork")["enabled"] is True

    # The mirror: each action is offered for exactly the state that action means
    # something in. Pausing a paused run, or a finished one, is not a slower
    # no-op -- it rewrites the artifact's status to something it already left.
    gui._select_run("halted")
    assert fake_dpg.config("btn_resume")["enabled"] is True
    assert fake_dpg.config("btn_pause")["enabled"] is False
    gui._select_run("done")
    assert fake_dpg.config("btn_pause")["enabled"] is False
    assert fake_dpg.config("btn_resume")["enabled"] is False


def test_a_read_only_source_disables_every_write_action(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # A v3 repository opens rather than being refused, so the console has to
    # disable the writes instead: a Pause click there would move a branch.
    gui = gui_factory(tmp_path)
    entry = RunEntry(
        key="run:sha256:abcdef",
        run=_run("alpha", status=RunStatus.running),
        path=None,  # a repository run has no file to write back to
        location="run:abcdef",
    )
    gui._loader.results.put(
        Snapshot(kind="repository", root=tmp_path, entries=[entry], writable=False)
    )
    gui._apply_snapshot()
    gui._select_entry("run:sha256:abcdef")
    fake_dpg.invoke(_inspect_button(fake_dpg, "s2"))
    assert fake_dpg.value("source_badge") == "v3 repository (read-only)"
    for tag in ("btn_pause", "btn_resume", "btn_fork", "menu_pause", "menu_resume", "menu_fork"):
        assert fake_dpg.config(tag)["enabled"] is False, f"{tag} is still live"
    assert fake_dpg.config("menu_refs")["enabled"] is True


# --------------------------------------------------------------------- DAG


def test_the_graph_draws_one_node_per_step_with_its_provider(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _priced_run("beta"))
    gui = gui_factory(tmp_path)
    gui._select_run("beta")
    nodes = _nodes(fake_dpg)
    assert sorted(nodes) == ["s1", "s2"]
    assert fake_dpg.config(nodes["s2"])["label"] == "model: 42"
    # The subtitle answers "who served this call?" — recovered here from the
    # rate card, since 0.7.2 has no Step.provider.
    assert any("anthropic" in text for text in fake_dpg.texts(nodes["s2"]))


def test_lineage_and_causal_edges_are_both_drawn_and_told_apart(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    parent, causal = app._link_theme("parent"), app._link_theme("causal")
    assert parent != causal
    drawn = _links(fake_dpg)
    lineage = sorted((cause, effect) for cause, effect, theme in drawn if theme == parent)
    assert lineage == [("s1", "s2"), ("s2", "s3")]
    # "s3 needed s1" is not "s1 ran before s3": a fork keeps that edge, so it is
    # drawn, and in its own colour rather than passed off as lineage.
    assert [(c, e) for c, e, theme in drawn if theme == causal] == [("s1", "s3")]
    assert "1 causal" in fake_dpg.value("dag_summary")


def test_the_graph_stops_at_the_node_cap_and_says_it_did(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # A legal artifact holds thousands of steps; five items per node is a frame
    # the console would never finish drawing.
    gui = gui_factory(tmp_path)
    entry = RunEntry(key="big", run=_chain("big", app.MAX_DAG_NODES + 1))
    _install(gui, [entry])
    gui._select_entry("big")
    assert len(fake_dpg.find("node")) == app.MAX_DAG_NODES
    assert f"drawing the first {app.MAX_DAG_NODES} step(s)" in fake_dpg.value("dag_summary")


def test_clearing_the_graph_deletes_every_link_before_any_node(
    tmp_path: Path, gui_factory, fake_dpg, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deleting a node a live link still references segfaults Dear PyGui's native
    # layer, so the order is a crash guard, not tidiness. The end state cannot
    # show it: only the sequence can.
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    order: list[str] = []
    delete_item = fake_dpg.delete_item

    def recording(tag, **kwargs):
        record = fake_dpg.items.get(tag)
        if record is not None and record["type"] in ("node", "node_link"):
            order.append(record["type"])
        delete_item(tag, **kwargs)

    monkeypatch.setattr(fake_dpg, "delete_item", recording)
    gui._clear_dag()
    assert order == ["node_link"] * 3 + ["node"] * 3  # 2 lineage + 1 causal, then the nodes
    assert fake_dpg.find("node") == [] and fake_dpg.find("node_link") == []
    assert fake_dpg.value("dag_summary").startswith("Select a run")


def test_selecting_another_run_redraws_the_graph_and_drops_the_old_step(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    _write(tmp_path, _priced_run("beta"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    # Inspect a step that beta does not have, so a stale inspector is visible as
    # a wrong answer rather than as the untouched startup prompt.
    fake_dpg.invoke(_inspect_button(fake_dpg, "s3"))
    assert "ID: s3" in fake_dpg.value("step_text")

    gui._select_run("beta")

    assert sorted(_nodes(fake_dpg)) == ["s1", "s2"]
    assert len(fake_dpg.find("node_link")) == 1
    # s3 is not in beta: leaving it in the inspector would describe a step the
    # graph beside it no longer draws.
    assert gui._selected_step is None
    assert fake_dpg.value("step_text") == "Select a step in the DAG"


# ---------------------------------------------------------------- keyboard


def test_down_and_up_walk_the_visible_runs_and_stop_at_the_ends(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    for run_id in ("alpha", "beta", "gamma"):
        _write(tmp_path, _run(run_id))
    gui = gui_factory(tmp_path)
    order = [entry.key for entry in gui._visible_entries()]

    def press(key):
        _press(fake_dpg, key)
        gui._apply_pending_input()  # a keystroke is applied between frames

    press(fake_dpg.mvKey_Down)
    assert gui._selected_key == order[0]
    press(fake_dpg.mvKey_Down)
    assert gui._selected_key == order[1]
    for _ in range(5):
        press(fake_dpg.mvKey_Down)
    assert gui._selected_key == order[-1]  # clamped, not wrapped
    for _ in range(9):
        press(fake_dpg.mvKey_Up)
    assert gui._selected_key == order[0]


def test_the_arrows_are_inert_while_a_field_has_focus(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    fake_dpg.focus_item("run_filter")
    _press(fake_dpg, fake_dpg.mvKey_Down)
    gui._apply_pending_input()
    assert gui._selected_key is None


def test_escape_closes_a_modal_before_touching_either_filter(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    _fire(fake_dpg, "step_filter", "grep")
    _fire(fake_dpg, "run_filter", "alpha")
    gui._open_help()
    _press(fake_dpg, fake_dpg.mvKey_Escape)
    assert fake_dpg.config("help_dialog")["show"] is False
    assert gui._step_filter == "grep"
    assert gui._run_filter == "alpha"


def test_escape_clears_the_dag_filter_before_the_run_filter(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    _fire(fake_dpg, "step_filter", "grep")
    _fire(fake_dpg, "run_filter", "alpha")
    _press(fake_dpg, fake_dpg.mvKey_Escape)
    assert gui._step_filter == ""
    assert gui._run_filter == "alpha"
    _press(fake_dpg, fake_dpg.mvKey_Escape)
    assert gui._run_filter == ""
    assert fake_dpg.value("run_filter") == ""


def test_the_transcript_dialog_counts_as_a_modal_for_escape(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # It did not, once: Esc over an open transcript cleared the DAG filter
    # behind it and left the dialog on screen.
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    _fire(fake_dpg, "step_filter", "grep")
    gui._open_transcript()
    _press(fake_dpg, fake_dpg.mvKey_Escape)
    assert fake_dpg.config("transcript_dialog")["show"] is False
    assert gui._step_filter == "grep"


def test_command_shortcuts_do_nothing_without_the_modifier(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    fake_dpg.set_value("status_bar", "")
    _press(fake_dpg, fake_dpg.mvKey_C)
    _press(fake_dpg, fake_dpg.mvKey_R)
    _press(fake_dpg, fake_dpg.mvKey_F)
    assert fake_dpg.clipboard == ""
    assert fake_dpg.value("status_bar") == ""
    assert fake_dpg.focused == set()


def test_command_shortcuts_fire_with_the_modifier_held(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    fake_dpg.keys.add(fake_dpg.mvKey_ModCtrl)
    _press(fake_dpg, fake_dpg.mvKey_C)
    assert fake_dpg.clipboard == "alpha"
    _press(fake_dpg, fake_dpg.mvKey_R)
    assert fake_dpg.value("status_bar") == "Refreshing..."
    _press(fake_dpg, fake_dpg.mvKey_F)
    assert fake_dpg.is_item_focused("run_filter")


def test_command_shortcuts_are_inert_while_the_user_is_typing(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # Ctrl+C in a text field is a copy, not a "copy the selected run id".
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    fake_dpg.keys.add(fake_dpg.mvKey_ModCtrl)
    fake_dpg.focus_item("run_filter")
    fake_dpg.set_value("status_bar", "")
    _press(fake_dpg, fake_dpg.mvKey_C)
    _press(fake_dpg, fake_dpg.mvKey_R)
    assert fake_dpg.clipboard == ""
    assert fake_dpg.value("status_bar") == ""


def test_the_arrows_stop_at_the_last_row_the_table_actually_drew(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # The keyboard cursor and the drawn window share one bound. Walking off the
    # end of the cap would select a run with no row: the inspector would fill
    # with it while nothing was marked, and there is no way to scroll to a row
    # the table never built.
    gui = gui_factory(tmp_path)
    entries = [
        RunEntry(key=f"r{index:04d}", run=_run(f"r{index:04d}"), mtime=float(index))
        for index in range(app.MAX_TABLE_ROWS + 1)
    ]
    _install(gui, entries)
    gui._render_run_table()
    visible = [entry.key for entry in gui._visible_entries()]
    assert len(visible) > app.MAX_TABLE_ROWS  # there is an undrawn run to walk onto
    last_drawn = visible[app.MAX_TABLE_ROWS - 1]
    gui._select_entry(last_drawn)

    _press(fake_dpg, fake_dpg.mvKey_Down)
    gui._apply_pending_input()

    assert gui._selected_key == last_drawn
    assert gui._selected_key != visible[-1]  # the run past the cap stays unreachable
    marked = [
        _cells(fake_dpg, row)[0]
        for row in _rows(fake_dpg)
        if fake_dpg.value(fake_dpg.children_of(row)[0]) is True
    ]
    assert marked == [last_drawn], "the selected run must be a row the reader can see"


def test_f1_opens_help_unless_the_user_is_typing(tmp_path: Path, gui_factory, fake_dpg) -> None:
    gui_factory(tmp_path)
    fake_dpg.focus_item("run_filter")
    _press(fake_dpg, fake_dpg.mvKey_F1)
    assert fake_dpg.config("help_dialog")["show"] is False
    fake_dpg.focused.clear()
    _press(fake_dpg, fake_dpg.mvKey_F1)
    assert fake_dpg.config("help_dialog")["show"] is True


# ------------------------------------------------------------- message log


def test_the_message_log_keeps_an_error_a_later_refresh_would_overwrite(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # The status line is one widget that every refresh tick rewrites; anything
    # the user may have to act on has to survive the next tick.
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._open_import_dialog()
    fake_dpg.set_value("import_path", str(tmp_path / "no-such-trace.json"))
    gui._do_import()
    assert "Cannot import" in fake_dpg.value("status_bar")
    gui._scan_now()
    assert "Cannot import" not in fake_dpg.value("status_bar")
    assert any("Cannot import" in line for line in fake_dpg.texts("message_log"))


def test_the_message_log_is_bounded(tmp_path: Path, gui_factory, fake_dpg) -> None:
    gui = gui_factory(tmp_path)
    for index in range(app.MAX_MESSAGES + 5):
        gui._note("info", f"message {index}")
    lines = fake_dpg.texts("message_log")
    assert len(lines) == app.MAX_MESSAGES
    assert any(f"message {app.MAX_MESSAGES + 4}" in line for line in lines)
    # The widgets are only half of it: _note also keeps the backing list every
    # other reader of the log goes through, and that list is what grows for the
    # lifetime of the process if it is not trimmed too.
    assert len(gui._messages) == app.MAX_MESSAGES
    assert gui._messages[-1].text.endswith(f"message {app.MAX_MESSAGES + 4}")


# --------------------------------------------------------------- debouncing


def test_typing_in_the_filter_does_not_rebuild_the_table_until_the_debounce_expires(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # One keystroke must not re-read every payload of every run.
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    _fire(fake_dpg, "run_filter", "zzz")
    assert _row_ids(fake_dpg) == ["alpha"]
    gui._apply_deferred_writes()
    assert _row_ids(fake_dpg) == ["alpha"]
    # Age the pending edit rather than sleeping through the debounce.
    gui._filter_dirty_at -= app.FILTER_DEBOUNCE_SECONDS
    gui._apply_deferred_writes()
    assert _row_ids(fake_dpg) == ["No runs match this filter"]
    assert "0/1 run(s) shown" in fake_dpg.value("status_bar")


def test_preferences_are_not_written_on_every_keystroke(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    preferences = Path(os.environ["OPENTINE_GUI_PREFS"])
    _fire(fake_dpg, "run_filter", "alp")
    gui._apply_deferred_writes()
    assert not preferences.exists()  # no disk write per character
    gui._preferences_dirty_at -= app.PREFERENCES_FLUSH_SECONDS
    gui._filter_dirty_at -= app.FILTER_DEBOUNCE_SECONDS
    gui._apply_deferred_writes()
    assert '"last_filter": "alp"' in preferences.read_text()


def test_a_filter_opentine_cannot_parse_falls_back_to_a_text_search(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    _fire(fake_dpg, "run_filter", "cost:>abc")
    gui._filter_dirty_at -= app.FILTER_DEBOUNCE_SECONDS
    gui._apply_deferred_writes()
    assert "falling back to a plain text search" in fake_dpg.value("status_bar")


# ------------------------------------------------------------- next match


def test_next_match_cycles_through_the_matches_and_scrolls_onto_them(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    _fire(fake_dpg, "step_filter", "s")  # every step id contains it
    matches = app._matching_steps(gui._selected_run, "s")
    assert len(matches) == 3
    seen, scrolled = [], []
    for _ in range(len(matches) + 1):
        gui._focus_next_match()
        seen.append(gui._selected_step.id)
        scrolled.append(fake_dpg.scrolls["dag_editor"])
    assert seen == [matches[0], matches[1], matches[2], matches[0]]  # cycles, does not stop
    assert "ID: s1" in fake_dpg.value("step_text")
    # Selecting a node the user cannot see is only half the feature: the editor
    # scrolls onto each match, right and down, as the layout demands.
    assert scrolled[0] == (0.0, 0.0)  # the first match is the top-left node
    assert any(x > 0 for x, _ in scrolled)
    assert any(y > 0 for _, y in scrolled)


def test_next_match_without_a_run_says_what_to_do_instead(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    gui = gui_factory(tmp_path)
    gui._focus_next_match()
    assert fake_dpg.value("status_bar") == "Select a run first"


def test_next_match_with_no_matches_says_so(tmp_path: Path, gui_factory, fake_dpg) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    _fire(fake_dpg, "step_filter", "no-such-step")
    gui._focus_next_match()
    assert fake_dpg.value("status_bar") == "No matching steps to jump to"


def test_the_dag_filter_marks_the_matching_nodes(tmp_path: Path, gui_factory, fake_dpg) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    _fire(fake_dpg, "step_filter", "grep")
    # Deferred like the run filter: matching walks every step's payload and then
    # rebuilds the graph, which is a second of work on a large run, so the
    # keystroke only records the intent and the frame loop does it.
    gui._apply_step_filter()
    labels = {step: fake_dpg.config(node)["label"] for step, node in _nodes(fake_dpg).items()}
    assert labels["s2"].startswith("* ")
    assert not labels["s1"].startswith("* ")
    assert "1/3 match query 'grep'" in fake_dpg.value("dag_summary")


# ----------------------------------------------------------------- dialogs


def test_the_transcript_opens_with_a_turn_per_entry_and_a_jump_to_its_step(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    gui._open_transcript()
    assert fake_dpg.config("transcript_dialog")["show"] is True
    body = fake_dpg.texts("transcript_body")
    assert "user" in body and "ship the release" in body
    assert "assistant  [s2]" in body
    fake_dpg.invoke("transcript_step_1")  # the assistant turn: the one that made a step
    assert fake_dpg.config("transcript_dialog")["show"] is False
    assert gui._selected_step.id == "s2"


def test_reopening_the_transcript_does_not_stack_two_copies(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    gui._open_transcript()
    first = list(fake_dpg.texts("transcript_body"))
    gui._open_transcript()
    assert fake_dpg.texts("transcript_body") == first


def test_a_huge_transcript_stops_at_the_cap_and_says_it_did(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # The transcript is artifact-supplied, and every turn costs a heading, a
    # content block and a spacer in the single frame that opens the dialog. A
    # 20,000-turn conversation fits inside MAX_TINE_BYTES, so the bound is what
    # stands between a legal .tine and ~60,000 items built at once -- the same
    # reason MAX_DAG_NODES and MAX_TABLE_ROWS exist.
    gui = gui_factory(tmp_path)
    run = _run("chatty")
    run.transcript.extend(
        {"role": "user", "content": "x"} for _ in range(app.MAX_TRANSCRIPT_TURNS)
    )
    _install(gui, [RunEntry(key="chatty", run=run)])
    gui._select_entry("chatty")

    gui._open_transcript()

    assert _transcript_turns_drawn(fake_dpg) == app.MAX_TRANSCRIPT_TURNS
    summary = fake_dpg.value("transcript_summary")
    assert f"showing the first {app.MAX_TRANSCRIPT_TURNS}" in summary
    # Capping without saying so would answer "how long was this conversation?"
    # with the length of the excerpt.
    assert f"{len(run.transcript)} turn(s)" in summary


def test_a_transcript_inside_the_cap_is_drawn_whole(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    gui._open_transcript()
    assert _transcript_turns_drawn(fake_dpg) == 2
    assert "showing the first" not in fake_dpg.value("transcript_summary")


def test_the_diff_dialog_opens_with_candidates_and_renders_a_comparison(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    _write(tmp_path, _priced_run("beta"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    gui._open_diff_dialog()
    assert fake_dpg.config("diff_dialog")["show"] is True
    assert any("beta" in item for item in fake_dpg.config("diff_candidates")["items"])
    gui._compare_runs()
    body = fake_dpg.value("diff_text")
    assert "A: alpha" in body and "B: beta" in body


def test_the_fork_dialog_opens_with_the_slice_it_would_keep(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    fake_dpg.invoke(_inspect_button(fake_dpg, "s3"))
    gui._open_fork_dialog()
    assert fake_dpg.config("fork_dialog")["show"] is True
    assert fake_dpg.value("fork_subject") == "Fork alpha at step s3"
    # s3 keeps s2 through lineage and s1 through the causal edge: the preview
    # has to agree with what the fork will actually take.
    assert fake_dpg.value("fork_slice") == "Keeps 3 of 3 step(s)"
    assert fake_dpg.value("fork_branch") == "main"


def test_expanding_a_run_opens_the_text_viewer_with_the_whole_prompt(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    gui._expand_run_detail()
    assert fake_dpg.config("text_dialog")["show"] is True
    assert fake_dpg.value("text_subject") == "Run alpha"
    body = fake_dpg.value("text_body")
    assert "Prompt (full):" in body and "ship the release" in body


def test_expanding_a_step_opens_the_text_viewer_with_both_payloads(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    fake_dpg.invoke(_inspect_button(fake_dpg, "s3"))
    gui._expand_step_detail()
    body = fake_dpg.value("text_body")
    assert fake_dpg.value("text_subject") == "Step s3"
    assert "Inputs (full):" in body and "Outputs (full):" in body and "shipped" in body


def test_the_help_dialog_opens_with_the_bindings_it_documents(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    gui = gui_factory(tmp_path)
    gui._open_help()
    assert fake_dpg.config("help_dialog")["show"] is True
    text = fake_dpg.value("help_text")
    assert "F1" in text and "Esc" in text
    assert "causal" in text  # the purple links need explaining somewhere


def test_the_about_panel_names_both_versions_and_the_source(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    gui = gui_factory(tmp_path)
    gui._open_about()
    assert fake_dpg.config("panel_dialog")["show"] is True
    assert fake_dpg.config("panel_dialog")["label"] == "About"
    text = fake_dpg.value("panel_text")
    assert f"opentine-gui {app._GUI_VERSION}" in text
    assert "Dear PyGui" in text
    assert str(tmp_path) in text


def test_the_statistics_panel_opens_over_the_visible_runs(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _write(tmp_path, _run("alpha"))
    _write(tmp_path, _priced_run("beta"))
    gui = gui_factory(tmp_path)
    gui._open_stats()
    assert fake_dpg.config("panel_dialog")["label"] == "Statistics"
    assert "2 all run(s)" in fake_dpg.value("panel_subject")
    assert "5 step(s)" in fake_dpg.value("panel_text")


def test_opening_a_second_panel_replaces_the_first_panels_controls(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    # The panel dialog is reused by stats, pricing, refs and import; leaving the
    # previous panel's controls behind would put a stats combo on the importer.
    _write(tmp_path, _run("alpha"))
    gui = gui_factory(tmp_path)
    gui._select_run("alpha")
    gui._open_stats()
    assert fake_dpg.exists("stats_group")
    gui._open_import_dialog()
    assert not fake_dpg.exists("stats_group")
    assert fake_dpg.exists("import_path")


def test_the_directory_picker_opens_on_the_current_directory(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    gui = gui_factory(tmp_path)
    gui._open_dir_picker()
    assert fake_dpg.config("dir_picker")["show"] is True
    assert fake_dpg.value("dir_picker_input") == str(tmp_path)
