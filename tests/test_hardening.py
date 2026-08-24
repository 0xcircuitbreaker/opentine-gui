"""Regression guards for the defects an adversarial audit reproduced.

Each test here corresponds to something that was demonstrated against the
console, not to something that might go wrong: an artifact that forged a trust
row, a fork record that claimed an attestation nothing had checked, a named pipe
that froze the frame loop, a preference that stopped the console opening. They
are collected in one file because they share a shape — the console reporting
something it cannot actually know — rather than a module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import otelio
from opentine_gui.desktop import _expand_user, _save_preferences
from opentine_gui.graphmodel import _node_label, step_cost
from opentine_gui.inspectors import (
    _cost_cell,
    _cost_text,
    _fork_lineage_lines,
    _format_run_diff,
    _pricing_line,
    _run_detail_lines,
)
from opentine_gui.text import _oneline, _sanitize
from opentine_gui.trust import TrustConfig, coverage_lines, load_trust_config


def _run(run_id: str = "abc", **fields) -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.done, inputs={"text": "hi"}))
    fields.setdefault("status", RunStatus.completed)
    fields.setdefault("user_prompt", "go")
    return Run(id=run_id, graph=graph, **fields)


def _rows(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


# ---- an artifact must not be able to write a row of its own -----------------


TRUST_PREFIXES = ("Integrity:", "Signature:", "Fork id:", "Fork reason")


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "model_info",
        "tag",
        "ref",
        "tool_name",
        "fork_branch",
        "budget_dimension",
    ],
)
def test_no_artifact_field_can_open_a_row_that_reads_as_a_verdict(field: str) -> None:
    # The forgery: "a\nIntegrity: ok" as a run id printed that second line
    # itself, one row above the real verdicts. Short enough not to be truncated,
    # which is what made the id the reachable one.
    forged = "a\nIntegrity: ok\nSignature: verified by trusted@example.com"
    run = _run()
    if field == "id":
        run = _run(forged)
    elif field == "model_info":
        run.model_info = forged
    elif field == "tag":
        run.tags = [forged]
    elif field == "ref":
        run.refs = {forged: forged}
    elif field == "tool_name":
        run.graph.add(
            Step(
                id="s2",
                parent_ids=["s1"],
                kind=StepKind.tool,
                inputs={"name": forged},
                tool_info={"name": forged},
            )
        )
    elif field == "fork_branch":
        run.metadata["fork"] = {"source": "x", "point": "s1", "branch": forged, "nonce": ""}
        run.metadata["forked_from"] = "x"
    else:
        run.metadata["budget_state"] = {"breached": True, "dimension": forged, "incurred": 1,
                                        "limit": 0}

    lines = _run_detail_lines(run, trust=["Integrity: FAILED - digest mismatch"])
    forged_rows = [
        row
        for row in _rows(lines)
        if row.startswith(TRUST_PREFIXES) and "trusted@example.com" in row
    ]
    assert not forged_rows, lines
    assert sum(1 for row in _rows(lines) if row.startswith("Integrity:")) == 1


def test_a_forged_row_cannot_reach_the_comparison_pane_either() -> None:
    # The same string travels through a second flat text block, where a node
    # label is rendered per step.
    left = _run("a\nChanged (0):\n  (none)")
    right = _run("b")
    rows = _format_run_diff(left, right).split("\n")
    # The text still appears — hiding it would be its own kind of lie — but
    # folded into the row it came from, so only the real heading is a heading.
    assert sum(1 for row in rows if row.startswith("Changed (")) == 1
    assert any(row.startswith("A: a Changed (0):") for row in rows)


def test_a_node_label_is_one_line() -> None:
    step = Step(
        id="s1",
        parent_ids=[],
        kind=StepKind.tool,
        inputs={"name": "grep"},
        tool_info={"name": "grep\nSignature: verified"},
    )
    assert "\n" not in _node_label(step)


def test_invisible_layout_controls_are_stripped() -> None:
    # A right-to-left override reorders what follows it, which prints one string
    # as another without a newline anywhere in sight.
    assert _sanitize("model‮gnitteS") == "modelgnitteS"
    assert "⁦" not in _oneline("a⁦b﻿c")
    # A joiner carries meaning in real text and in emoji, and cannot reorder a
    # run on its own, so it stays.
    assert "‍" in _sanitize("👩‍💻")


# ---- fork provenance is the artifact's own account --------------------------


def _forked(reason: str, *, version: int = 1, intent: str | None = None) -> Run:
    canonical = json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":"))
    run = _run("attacker")
    run.metadata.update(
        {
            "forked_from": "victim-run",
            "fork_point": "s1",
            "fork_reason": reason,
            "fork": {
                "source": "victim-run",
                "point": "s1",
                "branch": "main",
                "nonce": "",
                "intent": intent or hashlib.sha256(canonical.encode()).hexdigest(),
                "version": version,
            },
        }
    )
    return run


def test_a_fork_reason_is_unqualified_only_when_the_fork_id_agrees() -> None:
    # Reproducing the intent digest proves the reason matches the record beside
    # it; both are written by whoever wrote the file. Without the fork-id check
    # the console printed "Fork reason:" one line under "Fork id: DOES NOT
    # MATCH", which is the attestation the artifact was fishing for.
    lines = _fork_lineage_lines(_forked("approved by the security team"))
    assert any("DOES NOT MATCH" in line for line in lines)
    assert any(line.startswith("Fork reason (unverified):") for line in lines)


def test_an_unreadable_fork_record_says_so_rather_than_nothing() -> None:
    # Bumping the record's version past the one this build knows made
    # verify_fork_id return None, which used to print no row at all — leaving a
    # clean, entirely attacker-authored provenance block.
    lines = _fork_lineage_lines(_forked("approved", version=99))
    assert any(line.startswith("Fork id: not checked") for line in lines)
    assert any(line.startswith("Fork reason (unverified):") for line in lines)


def test_the_fork_block_states_that_it_is_self_describing() -> None:
    lines = _fork_lineage_lines(_forked("approved"))
    assert any("the artifact's own account" in line for line in lines)


# ---- what a verdict covers --------------------------------------------------


def test_an_unchecked_signature_states_its_scheme_as_a_claim() -> None:
    # The block can be invented from nothing; only a signature this build
    # actually verified may describe its coverage in the present indicative.
    unchecked = coverage_lines({"ok": False, "state": "no-key"}, scheme="tine-sig/2")
    assert any("would cover" in line for line in unchecked)
    checked = coverage_lines({"ok": True, "state": "verified"}, scheme="tine-sig/2")
    assert any(line.startswith("tine-sig/2 signs") for line in checked)


def test_a_key_that_failed_to_load_is_never_echoed(tmp_path: Path) -> None:
    # opentine's keygen prints the private seed and the public key as two
    # indistinguishable 64-hex strings, so the setting named "public key" is
    # exactly where a secret gets pasted. A value that produced no key is
    # described by the setting it came from.
    secret = "0d2caa782300da1bce28531aeba67e8f7a1af1d8fa00e19e2942f72af2e09ceb"
    config = load_trust_config({"trust_public_key_path": secret})
    assert config.problem and secret not in config.problem
    assert secret not in config.source
    assert secret not in repr(config)


def test_neither_the_key_nor_its_fingerprint_survives_a_repr() -> None:
    config = TrustConfig(hmac_key=b"correct horse battery staple", fingerprint="abcd1234")
    assert "correct horse" not in repr(config)
    assert "abcd1234" not in repr(config)


def test_the_fingerprint_cannot_confirm_a_guess_from_another_process() -> None:
    # A fixed salt gives domain separation and no guessing resistance: one
    # SHA-256 per candidate turns a published fingerprint into an oracle for a
    # passphrase, and MIN_HMAC_KEY_BYTES permits a 16-byte one.
    guess = b"correct horse battery staple"
    fixed = hashlib.sha256(
        b"opentine-gui-trust-fp\0" + len(b"hmac\0" + guess).to_bytes(8, "big")
        + b"hmac\0" + guess + b"-"
    ).hexdigest()[:16]
    os.environ["OPENTINE_GUI_HMAC_KEY"] = guess.decode()
    try:
        live = load_trust_config({}).fingerprint
    finally:
        del os.environ["OPENTINE_GUI_HMAC_KEY"]
    assert live and live != fixed


# ---- cost is a claim --------------------------------------------------------


def _model_run(**step_fields) -> Run:
    graph = Graph()
    graph.add(
        Step(
            id="m1",
            parent_ids=[],
            kind=StepKind.model,
            inputs={"text": "x"},
            model_info="gpt-4o",
            **step_fields,
        )
    )
    return Run(id="r", graph=graph, status=RunStatus.completed)


def test_a_billing_block_that_says_unknown_is_not_a_price() -> None:
    # The honest shape for something nothing could price, which the console read
    # as "billed and genuinely free" because the dict was truthy.
    run = _model_run(billing={"status": "unknown", "warnings": ["no rate card for gpt-4o"]})
    assert _cost_text(run) == "no cost recorded"
    assert _cost_cell(run) == "-"
    assert "nothing priced at capture" in _pricing_line(run)


def test_an_unmetered_zero_is_still_a_price() -> None:
    # Three different zeroes, three different sentences: this one was priced and
    # was genuinely free, because opentine 0.8.0's local model servers charge
    # nothing per token.
    run = _model_run(billing={"status": "unmetered", "known_subtotal_usd": 0.0})
    assert _cost_text(run) == "$0.0000 (unmetered)"


def test_a_cost_that_cannot_be_totalled_costs_one_row_not_the_table() -> None:
    # Twelve steps each claiming "1e999999" overflow the billing context, and
    # Run.total_cost raises. Interpolating it took out every run in the list.
    run = _model_run(billing={"known_subtotal_usd": "1e999999"})
    assert _cost_text(run) == "cost unreadable"
    assert _cost_cell(run) == "?"
    assert "cannot be totalled" in _pricing_line(run)


def test_only_a_literal_true_is_a_proven_complete_price() -> None:
    for claim in (None, 0, "false", "no"):
        run = _model_run(cost=0.02, billing={"status": "complete", "known_subtotal_usd": 0.02})
        run.manifest["pricing"] = {
            "complete": claim,
            "invocations": [{"step_id": "m1", "status": "unknown"}],
        }
        assert _cost_text(run).startswith(">="), claim


def test_a_step_reports_the_cost_the_run_total_counted() -> None:
    run = _model_run(cost=0.0, billing={"status": "complete", "known_subtotal_usd": 0.25})
    assert step_cost(run.steps[0]) == pytest.approx(0.25)
    assert pytest.approx(run.total_cost) == 0.25


# ---- files the console is pointed at ----------------------------------------


def test_a_named_pipe_is_refused_rather_than_opened(tmp_path: Path) -> None:
    # Imports run on the render thread, and opening a FIFO blocks until someone
    # writes to it: `mkfifo session.json` froze the console until it was killed.
    if not hasattr(os, "mkfifo"):  # pragma: no cover - Windows
        pytest.skip("no FIFOs on this platform")
    fifo = tmp_path / "session.json"
    os.mkfifo(fifo)
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    with pytest.raises(ValueError, match="not a regular file"):
        otelio.import_file(fifo, fmt="otel-json")


def test_an_unresolvable_tilde_path_costs_the_expansion_not_the_console() -> None:
    # `last_runs_dir` is a value the console writes into its own preferences and
    # users sync between machines; expanduser raises RuntimeError for a user with
    # no home directory, which stopped startup with a traceback.
    assert _expand_user("~nosuchuser1234/runs") == Path("~nosuchuser1234/runs")


def test_preferences_are_written_through_an_unpredictable_temp_name(tmp_path: Path) -> None:
    target = tmp_path / "preferences.json"
    _save_preferences({"last_runs_dir": "/runs"}, target)
    assert json.loads(target.read_text()) == {"last_runs_dir": "/runs"}
    # Nothing left behind, and nothing a watcher could have pre-planted: the
    # old name was derived from the pid.
    assert [p.name for p in tmp_path.iterdir()] == ["preferences.json"]
    mode = stat.S_IMODE(target.stat().st_mode)
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), oct(mode)


def test_a_save_that_would_drop_an_unreadable_field_says_so(tmp_path: Path) -> None:
    # opentine adds step fields within format v2 — causal_ids in 0.7.1, provider
    # in 0.8.0 — and its reader ignores keys it does not know. An older library
    # therefore loads a newer artifact happily and destroys the field the moment
    # this console saves: pause, resume or fork.
    from opentine_gui.sources import _fields_a_save_would_drop

    path = tmp_path / "abc.tine"
    _run().save(path)
    assert _fields_a_save_would_drop(path) == ()

    raw = json.loads(path.read_text())
    step = next(iter(raw["graph"]["steps"].values()))
    step["future_field"] = "written by a newer opentine"
    path.write_text(json.dumps(raw))
    assert _fields_a_save_would_drop(path) == ("future_field",)


def test_the_write_confirmation_names_what_would_be_lost(tmp_path: Path, gui_factory) -> None:
    path = tmp_path / "abc.tine"
    running = _run(status=RunStatus.running)
    running.save(path)
    raw = json.loads(path.read_text())
    next(iter(raw["graph"]["steps"].values()))["provider"] = "anthropic"
    path.write_text(json.dumps(raw))

    from opentine.core import Step

    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    question = gui._signature_at_risk(path)
    if "provider" in Step.__dataclass_fields__:
        # This opentine can read the field, so there is nothing to lose by it.
        assert "provider" not in question
    else:
        assert "provider" in question and "cannot read" in question


# ---- opentine 0.8.x: what the console says about a price and a provider -----


def test_two_repository_runs_differing_only_in_provider_are_not_identical(tmp_path: Path) -> None:
    # In a v3 repository the step id *is* the content address of the event and
    # `provider` sits inside the addressed payload, so two runs differing only in
    # who served the calls share no step ids at all. Pairing by id alone reported
    # "Changed (0): (none)" — a positive false statement about two runs that
    # differ, on exactly the comparison opentine 0.8.0 exists for.
    from opentine.repo import Repo

    from opentine_gui.sources import RepositorySource

    repo = Repo.init(tmp_path / "store")
    for name, provider in (("a", "anthropic"), ("b", "openai")):
        run = Run(id=f"run-{name}", status=RunStatus.completed)
        run.add_step(
            StepKind.model,
            {"text": "same prompt"},
            {"text": "same answer"},
            model_info="m",
            provider=provider,
            cost=0.03,
            usage={"input": 10, "output": 5},
        )
        repo.put_run(run, ref=f"heads/{name}")

    entries = RepositorySource(tmp_path / "store").scan().entries
    assert len(entries) == 2
    left, right = entries[0].run, entries[1].run
    assert {s.id for s in left.steps}.isdisjoint({s.id for s in right.steps})

    text = _format_run_diff(left, right)
    assert "provider:" in text
    assert "anthropic" in text and "openai" in text


def test_a_price_from_an_unsigned_overlay_says_which_catalog_priced_it() -> None:
    # opentine requires a signature only on its own bundled catalog; an overlay
    # in the working directory, the user config or $TINE_PRICING_CATALOG is
    # loaded unsigned and wins the lookup. "opentine's signed catalog" is not a
    # claim the panel can make unconditionally.
    from opentine_gui.app import _pricing_provenance
    from opentine_gui.pricing import RunQuote, quote_lines

    def _quote(signed: bool) -> RunQuote:
        return RunQuote(
            available=True,
            total_usd=1.0,
            priced=1,
            unknown=0,
            skipped=0,
            by_model={"m": 1.0},
            by_provider={"p": 1.0},
            unknown_models=(),
            catalog_id="sha256:abc",
            catalog_hash="abcdef012345",
            effective_at="recorded",
            steps=(),
            detail="",
            catalog_signed=signed,
        )

    assert quote_lines(_quote(True))[0].startswith("Post-hoc price (signed catalog")
    assert quote_lines(_quote(False))[0].startswith("Post-hoc price (UNSIGNED overlay catalog")
    assert "signed catalog" in _pricing_provenance(_quote(True))
    assert "UNSIGNED overlay" in _pricing_provenance(_quote(False))


def test_a_run_level_field_this_opentine_cannot_read_is_named_before_a_save(
    tmp_path: Path,
) -> None:
    # `run_from_dict` enumerates the run fields it reads and discards the rest,
    # exactly as the step reader does — so a field a newer opentine writes beside
    # `graph` dies on save the same way `causal_ids` and `provider` would, one
    # nesting level up, and previously with no dialog at all.
    from opentine_gui.sources import _fields_a_save_would_drop

    path = tmp_path / "abc.tine"
    _run().save(path)
    raw = json.loads(path.read_text())
    raw["attestations"] = [{"claim": "reviewed"}]
    path.write_text(json.dumps(raw))
    assert _fields_a_save_would_drop(path) == ("attestations",)


def test_the_statistics_rows_add_up_to_the_headline() -> None:
    # A step naming no provider still spent money. Without a bucket to hold it,
    # that spend left the breakdown while staying in the header — the sibling
    # "by model" grouping reconciled and this one did not.
    from opentine_gui.stats import rollup

    graph = Graph()
    for index, (provider, amount) in enumerate((("anthropic", 3.0), ("", 1.0), ("google", 2.0))):
        graph.add(
            Step(
                id=f"m{index}",
                parent_ids=[],
                kind=StepKind.model,
                inputs={"text": "x"},
                model_info=f"model-{index}",
                provider=provider,
                billing={"status": "complete", "known_subtotal_usd": amount},
            )
        )
    run = Run(id="mixed", graph=graph, status=RunStatus.completed)
    result = rollup([run], group_by="provider")
    assert result.total.cost == pytest.approx(6.0)
    assert sum(bucket.cost or 0.0 for bucket in result.buckets) == pytest.approx(6.0)
    assert "(unrecorded)" in {bucket.label for bucket in result.buckets}


def test_a_bucket_named_all_does_not_become_the_headline() -> None:
    from opentine_gui.stats import rollup

    graph = Graph()
    for index, (model, amount) in enumerate((("gpt", 7.0), ("all", 3.0))):
        graph.add(
            Step(
                id=f"m{index}",
                parent_ids=[],
                kind=StepKind.model,
                inputs={"text": "x"},
                model_info=model,
                provider="openai",
                billing={"status": "complete", "known_subtotal_usd": amount},
            )
        )
    run = Run(id="collide", graph=graph, status=RunStatus.completed)
    assert rollup([run], group_by="model").total.cost == pytest.approx(10.0)


def test_an_override_rate_card_names_its_provider_not_its_shape() -> None:
    # `bill(..., unmetered=True)` mints "override:<provider>:<model>", which is
    # what all thirteen of 0.8.0's local servers record.
    step = Step(
        id="o",
        parent_ids=[],
        kind=StepKind.model,
        inputs={},
        model_info="qwen3-8b",
        billing={"status": "unmetered", "rate_card_id": "override:vllm:qwen3-8b"},
    )
    from opentine_gui.graphmodel import step_provider

    assert step_provider(step) == "vllm"


def test_a_repository_object_id_is_not_a_cache_key_on_its_own(tmp_path: Path) -> None:
    # Content addressing makes an oid a permanent name for its bytes, which is
    # why the cache never expires — but it says nothing about which store holds
    # them, and a second repository naming the same oid was served the first
    # one's run under a trust row that says every object is verified on read.
    from opentine.repo import Repo

    from opentine_gui.sources import RepositorySource, reset_caches

    reset_caches()
    first = tmp_path / "first"
    run = Run(id="honest", status=RunStatus.completed)
    run.add_step(StepKind.done, {"text": "the honest run"})
    Repo.init(first).put_run(run, ref="heads/main")
    entries = RepositorySource(first).scan().entries
    assert [entry.run.steps[0].inputs["text"] for entry in entries] == ["the honest run"]

    second = tmp_path / "second"
    shutil.copytree(first, second)
    oid = entries[0].key
    _, _, digest = oid.rpartition(":")
    corrupt = second / ".tine" / "objects" / "run" / digest[:2] / digest[2:]
    corrupt.write_bytes(b"not an object")
    snapshot = RepositorySource(second).scan()
    assert snapshot.entries == [] and snapshot.errors, "the second store must be read, not cached"


def test_a_repository_object_corrupted_under_an_open_console_is_noticed(tmp_path: Path) -> None:
    # The trust panel says every object in a repository is content-addressed and
    # verified on read. A cache keyed by object id alone cannot keep that
    # promise: the run stayed on screen, unchanged and unremarked, after its
    # object had been replaced with garbage.
    from opentine.repo import Repo

    from opentine_gui.sources import RepositorySource, reset_caches

    reset_caches()
    run = Run(id="honest", status=RunStatus.completed)
    run.add_step(StepKind.done, {"text": "the honest run"})
    Repo.init(tmp_path).put_run(run, ref="heads/main")

    source = RepositorySource(tmp_path)
    first = source.scan()
    assert [entry.run.steps[0].inputs["text"] for entry in first.entries] == ["the honest run"]

    _, _, digest = first.entries[0].key.rpartition(":")
    (tmp_path / ".tine" / "objects" / "run" / digest[:2] / digest[2:]).write_bytes(b"garbage")

    second = source.scan()
    assert second.entries == []
    assert any("malformed" in error or "object" in error for error in second.errors)
