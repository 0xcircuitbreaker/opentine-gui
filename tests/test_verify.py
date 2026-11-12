"""Run > Verify offline: opentine's own `tine verify --offline` report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import verify
from opentine_gui.trust import HMAC_KEY_ENV
from tests.artifacts import dump_doc, load_doc, save_v2

KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.skipif(not verify.available(), reason="no offline verifier")


def _run(run_id: str = "abc") -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="hi")


def test_an_unsigned_run_passes_the_digest_but_establishes_no_authorship(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    report = verify.verify_offline(path)
    assert report.ok and not report.error
    assert report.lines[0].startswith("pass") and "digest (required)" in report.lines[0]
    assert any(line.startswith("unavailable-offline signature") for line in report.lines)
    assert "Authorship: NOT established (internal consistency only)" in report.lines
    assert report.lines[-1] == "VERIFIED (offline)"
    assert report.document["schema"] and report.document["checks"]


def test_strict_fails_when_the_signature_cannot_be_checked(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    report = verify.verify_offline(path, options=verify.VerifyOptions(strict=True))
    assert not report.ok
    assert report.lines[-1] == "FAILED (offline)"
    assert any("required check(s) unavailable offline: signature" in line for line in report.lines)


def test_the_consoles_own_key_verdict_replaces_the_keyless_row(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path, sign_key=KEY)
    good = {"state": "verified", "algorithm": "hmac-sha256", "signer": "ci\nSignature: forged"}
    report = verify.verify_offline(path, signature=good, key_source="HMAC key from $X")
    row = next(line for line in report.lines if line.startswith("pass") and "signature" in line)
    assert "signer=ci Signature: forged" in row  # flattened onto its own row
    assert "Authorship: key configured in the console (HMAC key from $X)" in report.lines
    assert all("\n" not in line for line in report.lines)

    bad = {"state": "mismatch", "reason": "the file changed after signing"}
    report = verify.verify_offline(path, signature=bad, key_source="k")
    assert not report.ok
    assert any(line.startswith("fail") and "signature" in line for line in report.lines)


def test_a_broken_digest_fails_and_keeps_the_signature_unchecked(tmp_path: Path) -> None:
    path = save_v2(_run(), tmp_path / "abc.tine")
    raw = load_doc(path)
    next(iter(raw["graph"]["steps"].values()))["inputs"]["text"] = "tampered"
    dump_doc(path, raw)
    report = verify.verify_offline(path, signature={"state": "verified"}, key_source="k")
    assert not report.ok
    assert report.lines[0].startswith("fail") and "digest" in report.lines[0]
    assert not any(line.startswith("pass") and "signature" in line for line in report.lines)


def test_an_unusable_option_is_an_error_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "abc.tine"
    _run().save(path)
    options = verify.VerifyOptions(action_policy=str(tmp_path / "missing.json"))
    report = verify.verify_offline(path, options=options)
    # Either opentine reports the missing policy as a failed check or refuses it;
    # what must not happen is an exception reaching the UI.
    assert report.error or not report.ok or any("policy" in line for line in report.lines)


def test_the_panel_runs_with_the_configured_key_and_copies_json(
    tmp_path: Path, gui_factory, fake_dpg, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HMAC_KEY_ENV, KEY.decode())
    _run().save(tmp_path / "abc.tine", sign_key=KEY)
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    gui._open_verify()
    body = fake_dpg.value("panel_text")
    assert "VERIFIED (offline)" in body
    assert "Authorship: key configured in the console" in body
    copied: list[str] = []
    gui._copy_to_clipboard = lambda value, label: copied.append(value)
    gui._copy_verify_json()
    assert json.loads(copied[0])["ok"] is True


def test_shortcuts_open_the_read_panels_and_respect_text_fields(
    tmp_path: Path, gui_factory, fake_dpg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run().save(tmp_path / "abc.tine")
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    opened: list[str] = []
    for name in ("_open_transcript", "_open_diff_dialog", "_open_pricing", "_open_verify"):
        monkeypatch.setattr(gui, name, lambda name=name: opened.append(name))
    monkeypatch.setattr(gui, "_command_down", lambda: True)
    monkeypatch.setattr(gui, "_typing", lambda: False)
    gui._on_ctrl_t(), gui._on_ctrl_d(), gui._on_ctrl_p(), gui._on_ctrl_v()
    assert opened == ["_open_transcript", "_open_diff_dialog", "_open_pricing", "_open_verify"]
    monkeypatch.setattr(gui, "_typing", lambda: True)  # Ctrl+V pastes into a field
    gui._on_ctrl_v()
    assert opened[-1] == "_open_verify" and len(opened) == 4


def test_assurance_is_reported_as_verified_not_as_claimed(tmp_path: Path) -> None:
    from opentine_gui.inspectors import _trust_lines

    path = tmp_path / "abc.tine"
    run = _run()
    # A capture can claim any tier; only a proxy signature (and a timestamp)
    # raises the verified one.
    run.metadata["capture"] = {"method": "transcript-import", "claimed_tier": "T3"}
    run.save(path)
    report = verify.verify_offline(path)
    row = next(line for line in report.lines if line.startswith("Assurance:"))
    assert row.startswith("Assurance: T0 verified (claimed T3)")
    assert any(line.startswith("Assurance: T0 verified") for line in _trust_lines(path))
