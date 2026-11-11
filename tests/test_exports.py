"""Run > Export as HTML page / ATIF JSON, rendered by opentine itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind

from opentine_gui import exports


def _run(run_id: str = "abc") -> Run:
    graph = Graph()
    graph.add(
        Step(id="s1", parent_ids=[], kind=StepKind.model, model_info="m",
             inputs={"text": "<script>alert(1)</script>"},
             outputs={"text": "<img src=x onerror=alert(2)>"})
    )
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="<svg onload=1>")


@pytest.mark.skipif(not exports.available(exports.HTML), reason="no HTML view")
def test_the_html_page_is_inert_whatever_the_run_says(tmp_path: Path) -> None:
    page = exports.html_page(_run(), cost="$0.00")
    assert exports.CSP in page and "default-src 'none'" in page
    assert page.index("Content-Security-Policy") < page.index("<body")
    assert "<script" not in page.lower() and "<img" not in page and "<svg" not in page


@pytest.mark.skipif(not exports.available(exports.ATIF), reason="no ATIF exporter")
def test_atif_is_written_atomically_and_never_over_a_file(tmp_path: Path) -> None:
    target = tmp_path / "abc.atif.json"
    written = exports.write(exports.ATIF, _run(), target, cost="$0")
    assert written == target.stat().st_size
    assert json.loads(target.read_text())["schema_version"].startswith("ATIF")
    with pytest.raises(FileExistsError):
        exports.write(exports.ATIF, _run(), target, cost="$0")
    assert [p.name for p in tmp_path.iterdir()] == ["abc.atif.json"]


def test_the_menu_exports_beside_the_runs_and_asks_before_overwriting(
    tmp_path: Path, gui_factory, fake_dpg
) -> None:
    _run().save(tmp_path / "abc.tine")
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    gui._export_html()
    page = tmp_path / "abc.html"
    assert page.is_file() and exports.CSP in page.read_text()
    before = page.read_bytes()
    gui._export_html()
    assert gui._modal_open() == "confirm_dialog"
    assert page.read_bytes() == before
    gui._confirm_accept()
    gui._export_atif()
    assert (tmp_path / "abc.atif.json").is_file()


def test_every_export_is_scrubbed_like_tine_export(tmp_path: Path) -> None:
    from opentine_gui import otelio

    graph = Graph()
    graph.add(
        Step(id="s1", parent_ids=[], kind=StepKind.model, model_info="m",
             inputs={"text": "my key is sk-ant-api03-" + "A" * 90, "api_key": "hunter2hunter2"})
    )
    run = Run(id="r", graph=graph, status=RunStatus.completed, user_prompt="hi")
    documents = [json.dumps(otelio.export_document(run))]
    documents += [exports.render(fmt, run, cost="$0").decode() for fmt in exports.FORMATS]
    for document in documents:
        assert "sk-ant-api03-AAAA" not in document and "hunter2" not in document
    assert run.steps[0].inputs["api_key"] == "hunter2hunter2", "the run itself is untouched"


@pytest.mark.parametrize("fmt", exports.VENDORS, ids=lambda f: f.key)
def test_vendor_exports_land_beside_the_runs(fmt, tmp_path: Path, gui_factory) -> None:
    _run().save(tmp_path / "abc.tine")
    gui = gui_factory(tmp_path)
    gui._select_run("abc")
    gui._export_as(fmt)
    out = tmp_path / f"abc{fmt.suffix}"
    assert out.is_file() and json.loads(out.read_text())
