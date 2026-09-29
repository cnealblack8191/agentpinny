"""Scanning a drawing set with its legend: vector sheets, rotated sheets,
scanned sheets, block (XObject) symbols, legend drawn at another size,
look-alike symbols, and the errors and outputs."""

from __future__ import annotations

import dataclasses
import json
import math

import pikepdf
import pytest

from pinny.detection import BoundingBox
from pinny.legend import read_legend
from pinny.scan import ScanError, SetScanSettings, render_page, scan_set
from pinny.scan.__main__ import main as cli
from pinny.vector import detect_exemplar, make_exemplar
from pinny.vector.content import get_page, open_pdf, page_frame

from tests.legend.pdfgen import Canvas, Layout, Row, build, draw_legend, place, raster_copy, use_form

PX = 200 / 72

LEGEND_ROWS = [
    Row(["duplex"], ["DUPLEX RECEPTACLE"], tag="D"),
    Row(["gfci"], ["GFCI RECEPTACLE"], tag="G", label="GFI"),
    Row(["dedicated"], ["DEDICATED RECEPTACLE"], tag="DD"),
    Row(["quad"], ["QUAD RECEPTACLE"], tag="Q"),
    Row(["tstat"], ["THERMOSTAT (BY MECH)"], tag="T"),
]
LAYOUT = Layout(sym_x=70, tag_x=112, desc_x=150)
TAG = {"duplex": "D", "gfci": "G", "dedicated": "DD", "quad": "Q"}

SHEET1 = [("duplex", 150, 100, 0), ("duplex", 300, 100, 0), ("duplex", 98, 300, 270), ("duplex", 1062, 300, 90),
          ("gfci", 500, 100, 0), ("gfci", 650, 100, 0), ("dedicated", 800, 100, 0), ("quad", 950, 100, 0),
          ("duplex", 400, 662, 180), ("quad", 700, 662, 180), ("tstat", 600, 400, 0)]
SHEET2 = [("duplex", 200, 100, 0), ("gfci", 400, 100, 0), ("dedicated", 98, 400, 270)]


def expected(sheet):
    out = {}
    for k, *_ in sheet:
        if k in TAG:
            out[TAG[k]] = out.get(TAG[k], 0) + 1
    return out


def plan(sheet, wire=False, canvas=None):
    c = canvas or Canvas()
    c.rect(80, 80, 1000, 600)
    for k, x, y, r in sheet:
        place(c, k, x, y, r, label="GFI" if k == "gfci" else None)
    if wire:
        c.line(280, 90, 320, 110)  # a wire drawn through the duplex at (300, 100)
    return c


@pytest.fixture(scope="module")
def vector_set(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scan")
    leg = Canvas()
    draw_legend(leg, LEGEND_ROWS, LAYOUT)
    path = tmp / "set.pdf"
    # Sheet 2 is a rotated page drawn to display upright.
    build([(leg, 0, None), (plan(SHEET1, wire=True), 0, None), (plan(SHEET2, canvas=Canvas(1224, 792)), 90, None)], str(path))
    legend = read_legend(path)
    return path, legend, scan_set(path, legend)


def test_counts_every_legend_symbol_per_sheet(vector_set):
    _, legend, res = vector_set
    assert res.counts_by_sheet() == {1: expected(SHEET1), 2: expected(SHEET2)}
    assert res.counts() == {"D": 6, "G": 3, "DD": 2, "Q": 2}  # the thermostat isn't counted
    assert [s.method for s in res.sheets] == ["vector", "vector"]
    assert res.legend_page == 0 and all(s.page_index != 0 for s in res.sheets)
    assert not res.warnings


def test_detections_sit_on_the_symbols(vector_set):
    _, _, res = vector_set
    sheet1 = next(s for s in res.sheets if s.page_index == 1)
    for d in sheet1.detections:
        cx, cy = d.center
        near = [(k, x, y) for k, x, y, _ in SHEET1 if TAG.get(k) == d.tag and math.hypot(x * PX - cx, y * PX - cy) < 25]
        assert near, (d.tag, cx, cy)
    assert all(d.label == "GFI" for d in sheet1.detections if d.tag == "G")


def test_labels_and_detail_separate_lookalikes(vector_set):
    """A GFCI is a duplex plus "GFI" text; a dedicated is a duplex plus a
    triangle. Each spot is counted once, as the right symbol."""
    _, _, res = vector_set
    sheet1 = next(s for s in res.sheets if s.page_index == 1)
    assert sheet1.dropped.get("G", 0) >= 5  # GFCI matched plain duplexes but had no label there
    assert sheet1.dropped.get("D", 0) >= 3  # duplex also matched the GFCI and dedicated spots


def test_scanned_sheet(tmp_path, vector_set):
    src, _, _ = vector_set
    raster_copy(str(src), 1, str(tmp_path / "scan.pdf"))
    with pikepdf.open(src) as a, pikepdf.open(tmp_path / "scan.pdf") as b:
        del a.pages[1:]
        a.pages.append(b.pages[0])
        a.save(tmp_path / "mixed_set.pdf")
    path = tmp_path / "mixed_set.pdf"
    legend = read_legend(path, page_index=0)
    res = scan_set(path, legend)
    assert res.sheets[0].method == "raster"
    assert res.counts_by_sheet() == {1: expected(SHEET1)}


def test_block_symbols_match_across_pages(tmp_path):
    forms = {"SymD": "duplex", "SymQ": "quad", "SymJ": "jbox"}
    leg = Canvas()
    leg.text(50, 58, "ELECTRICAL LEGEND", size=12)
    leg.text(50, 74, "SYMBOL", size=7); leg.text(112, 74, "TAG", size=7); leg.text(150, 74, "DESCRIPTION", size=7)
    for i, (name, tag, desc) in enumerate([("SymD", "D", "DUPLEX RECEPTACLE"), ("SymQ", "Q", "QUAD RECEPTACLE"),
                                           ("SymJ", "J", "JUNCTION BOX")]):
        y = 100 + 30 * i
        use_form(leg, name, 70, y)
        leg.text(112, y + 3, tag); leg.text(150, y + 3, desc)
    p = Canvas()
    p.rect(80, 80, 1000, 600)
    spots = [("SymD", 150, 100, 0), ("SymD", 300, 100, 90), ("SymD", 450, 100, 180), ("SymQ", 600, 100, 0),
             ("SymJ", 700, 300, 0), ("SymJ", 800, 300, 270)]
    for name, x, y, r in spots:
        use_form(p, name, x, y, r)
    path = tmp_path / "blocks.pdf"
    build([(leg, 0, None), (p, 0, None)], str(path), forms=forms)
    legend = read_legend(path)
    assert [e.tag for e in legend.entries] == ["D", "Q", "J"]
    res = scan_set(path, legend)
    assert res.counts() == {"D": 3, "Q": 1, "J": 2}
    assert {d.source for d in res.detections()} == {"vector-xobject"}


def test_legend_drawn_at_another_size(tmp_path):
    leg = Canvas()
    leg.text(50, 58, "LEGEND", size=12)
    leg.text(55, 74, "SYMBOL", size=7); leg.text(112, 74, "TAG", size=7); leg.text(150, 74, "DESCRIPTION", size=7)
    for i, (k, t, desc) in enumerate([("duplex", "D", "DUPLEX RECEPTACLE"), ("quad", "Q", "QUAD RECEPTACLE")]):
        y = 100 + 34 * i
        place(leg, k, 75, y, 0, scale=1.5)
        leg.text(112, y + 3, t); leg.text(150, y + 3, desc)
    p = Canvas()
    p.rect(80, 80, 1000, 600)
    for k, x, y, r in [("duplex", 150, 100, 0), ("duplex", 300, 100, 0), ("quad", 500, 100, 0), ("duplex", 98, 300, 270)]:
        place(p, k, x, y, r)
    path = tmp_path / "scale.pdf"
    build([(leg, 0, None), (p, 0, None)], str(path))
    legend = read_legend(path)
    res = scan_set(path, legend)
    assert res.counts() == {"D": 3, "Q": 1}
    assert all(abs(f - 2 / 3) < 1e-9 for f in res.scales.values())
    assert any("0.67×" in w for w in res.warnings)
    off = scan_set(path, legend, SetScanSettings(scale_search=False))
    assert off.counts() == {"D": 0, "Q": 0} and any("not found" in w for w in off.warnings)


def test_identical_legend_symbols_are_reported(tmp_path):
    rows = [Row(["duplex"], ["DUPLEX RECEPTACLE"], tag="D"), Row(["usb"], ["USB RECEPTACLE"], tag="U")]
    leg = Canvas()
    draw_legend(leg, rows, LAYOUT)
    path = tmp_path / "same.pdf"
    build([(leg, 0, None), (plan([("duplex", 150, 100, 0), ("duplex", 300, 100, 0)]), 0, None)], str(path))
    res = scan_set(path, read_legend(path))
    assert res.counts() == {"D": 2, "U": 0}
    assert any("drawn the same" in w for w in res.warnings)


def test_scan_refusals(vector_set, tmp_path):
    path, legend, _ = vector_set
    other = tmp_path / "other.pdf"
    leg = Canvas()
    draw_legend(leg, LEGEND_ROWS, LAYOUT)
    build([(leg, 0, None)], str(other))
    with pytest.raises(ScanError) as e:
        scan_set(other, legend)
    assert e.value.code == "legend_mismatch"
    with pytest.raises(ScanError) as e:
        scan_set(path, legend, SetScanSettings(pages=(9,)))
    assert e.value.code == "page_index_out_of_range"
    from pinny.legend import Legend
    lg = Legend.from_dict(legend.to_dict())
    for x in lg.entries:
        lg.set_count(x.id, False)
    with pytest.raises(ScanError) as e:
        scan_set(path, lg)
    assert e.value.code == "nothing_to_count"
    lg = Legend.from_dict(legend.to_dict())
    lg.set_tag(lg.entries[1].id, lg.entries[0].tag)  # duplicate tag needs a look
    with pytest.raises(ScanError) as e:
        scan_set(path, lg)
    assert e.value.code == "legend_needs_review"


def test_pages_option_and_legend_page(vector_set):
    path, legend, _ = vector_set
    only2 = scan_set(path, legend, SetScanSettings(pages=(2,)))
    assert [s.page_index for s in only2.sheets] == [2]
    with_legend = scan_set(path, legend, SetScanSettings(pages=(0,), skip_legend_page=False))
    assert with_legend.sheets[0].page_index == 0 and sum(with_legend.counts().values()) >= 4


def test_result_json_and_progress(vector_set):
    path, legend, _ = vector_set
    seen = []
    res = scan_set(path, legend, progress=lambda p, done, total: seen.append((p, done, total)))
    assert seen == [(1, 1, 2), (2, 2, 2)]
    d = json.loads(json.dumps(res.to_dict()))
    assert d["format"] == "pinny.set_scan" and d["counts"]["D"] == 6
    det = d["sheets"][0]["detections"][0]
    assert {"entry_id", "tag", "box", "x", "y", "score", "rotation", "source"} <= set(det)
    assert d["sheets"][0]["coordinate_frame"]["dpi"] == 200


def test_cross_page_exemplar_and_render(vector_set):
    path, legend, _ = vector_set
    e = legend.entries[0]
    b = e.symbol_boxes[0]
    ex = make_exemplar(path, 0, BoundingBox(int(b[0]) - 2, int(b[1]) - 2, int(b[2] - b[0]) + 4, int(b[3] - b[1]) + 4))
    res = detect_exemplar(path, 1, ex)
    assert len(res.detections) >= 5  # duplex geometry also sits inside GFCI and dedicated symbols
    big = ex.scaled(2.0)
    assert big.scale == 2.0 and big.box.width >= 2 * ex.box.width - 2 and big.xobject_key is None
    for page in (1, 2):
        img = render_page(path, page)
        with open_pdf(path) as pdf:
            fr = page_frame(get_page(pdf, page))
        assert img.shape == (fr.height_px, fr.width_px, 3)


def test_cli(vector_set, tmp_path, capsys):
    path, legend, _ = vector_set
    legend.save(tmp_path / "legend.json")
    out = tmp_path / "result.json"
    assert cli([str(path), "--legend", str(tmp_path / "legend.json"), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "GFCI receptacle" in text and json.loads(out.read_text())["counts"]["G"] == 3
    assert cli([str(path), "--pages", "x"]) == 64
    assert cli([str(tmp_path / "missing.pdf")]) == 2
