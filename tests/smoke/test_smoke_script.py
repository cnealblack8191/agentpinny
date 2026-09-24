"""End-to-end check of scripts/smoke_test.py on a generated PDF."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

REPO_ROOT = Path(__file__).resolve().parents[2]
PT_PER_PX = 72 / 200  # canonical 200 DPI


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _draw_symbol(page, ox, oy, rotated):
    """An asymmetric receptacle-like symbol in a 36x36 pt cell at (ox, oy)."""
    def pt(x, y):
        # Rotate 90 degrees clockwise (y-down) about the cell centre.
        if rotated:
            x, y = 36 - y, x
        return pymupdf.Point(ox + x, oy + y)

    shape = page.new_shape()
    shape.draw_circle(pt(18, 18), 10)
    shape.draw_line(pt(14, 12), pt(14, 24))
    shape.draw_line(pt(22, 12), pt(22, 24))
    shape.draw_line(pt(28, 18), pt(36, 18))
    shape.finish(color=(0, 0, 0), width=1.5)
    shape.commit()


def _make_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page(width=720, height=540)
    # Cells on multiples of 36 pt land on whole canonical pixels (100 px).
    cells = [(72, 72, False), (288, 72, False), (72, 324, False), (504, 360, False), (432, 180, True)]
    for ox, oy, rotated in cells:
        _draw_symbol(page, ox, oy, rotated)
    page.draw_rect(pymupdf.Rect(20, 20, 700, 520), color=(0, 0, 0), width=1)
    doc.save(str(path))
    return cells


def test_smoke_script_end_to_end(tmp_path):
    smoke = _load("smoke_test", REPO_ROOT / "scripts" / "smoke_test.py")
    pdf = tmp_path / "drawing.pdf"
    cells = _make_pdf(pdf)
    out = tmp_path / "out"

    # Render-only pass.
    assert smoke.main([str(pdf), "--out-dir", str(out)]) == 0
    assert (out / "page.png").is_file() and (out / "grid.png").is_file()
    assert not (out / "detections.json").exists()

    # The first cell, in canonical px (72 pt -> 200 px, 36 pt -> 100 px).
    assert smoke.main([
        str(pdf), "--out-dir", str(out), "--document-id", "doc-1",
        "--template-box", "200,200,100,100",
    ]) == 0
    export = json.loads((out / "detections.json").read_text())
    assert export["coordinate_frame"]["width"] == 2000
    assert export["coordinate_frame"]["height"] == 1500
    assert export["document"]["document_id"] == "doc-1"

    expected = {(round((ox + 18) / PT_PER_PX), round((oy + 18) / PT_PER_PX)) for ox, oy, _ in cells}
    found = {(round(d["x"]), round(d["y"])) for d in export["detections"]}
    assert found == expected
    rotations = sorted(d["rotation"] for d in export["detections"])
    assert rotations == [0, 0, 0, 0, 90]

    # The export is accepted by the independent evaluator.
    sys.path.insert(0, str(REPO_ROOT / "evaluation"))
    try:
        from pinny_eval.inputs import load_detections
    finally:
        sys.path.pop(0)
    loaded = load_detections(str(out / "detections.json"))
    assert len(loaded.points) == 5


def test_smoke_script_rejects_bad_page(tmp_path):
    smoke = _load("smoke_test", REPO_ROOT / "scripts" / "smoke_test.py")
    pdf = tmp_path / "drawing.pdf"
    _make_pdf(pdf)
    with pytest.raises(SystemExit):
        smoke.main([str(pdf), "--page", "3", "--out-dir", str(tmp_path / "out")])
