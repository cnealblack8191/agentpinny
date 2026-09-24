"""(a) Block reuse: one Form XObject placed many times."""

from __future__ import annotations

import pikepdf
import pytest

from pinny.vector import VectorMatcher, VectorSettings, classify_page

from .helpers import match_expected
from .pdfgen import (
    Placement,
    box_for,
    build_xobject_page,
    form_xobject,
    standard_placements,
    symbol_ops,
)

MEDIA = (0, 0, 1224, 792)
CROP = (20, 30, 1200, 780)
DISTRACTORS = [
    Placement(700, 600, variant="simplex"),
    Placement(760, 600, angle=90, variant="quad"),
    Placement(820, 600, variant="square"),
    Placement(880, 600, angle=180, variant="simplex"),
]


def _save(pdf, tmp_path, name="a.pdf"):
    path = tmp_path / name
    pdf.save(path)
    return path


@pytest.mark.parametrize("rotate", [90, 0, 180, 270])
def test_all_placements_found_with_rotation_and_mirror(tmp_path, rotate):
    placements = standard_placements(20)
    pdf = pikepdf.new()
    build_xobject_page(pdf, placements + DISTRACTORS, mediabox=MEDIA, cropbox=CROP, rotate=rotate)
    path = _save(pdf, tmp_path)
    assert classify_page(path, 0).kind == "vector"

    exemplar = placements[5]  # rotated 90 ccw in PDF space, not mirrored
    result = VectorMatcher().detect(path, 0, box_for(exemplar, CROP, rotate))
    d = result.to_dict()
    assert d["strategy"] == "xobject"
    assert d["detector"]["name"] == "pinny-vector-xobject"
    assert all(det["source"] == "vector-xobject" and det["score"] == 1.0 for det in d["detections"])
    assert any(det.get("mirrored") for det in d["detections"])
    match_expected(d["detections"], placements, exemplar, CROP, rotate)
    assert d["coordinate_frame"]["width"] == result.frame.width_px
    assert d["warnings"] == []


def test_identical_copy_of_the_form_is_grouped(tmp_path):
    """A second XObject with identical content (different object) is the same symbol."""
    placements = standard_placements(12)
    pdf = pikepdf.new()
    build_xobject_page(pdf, placements + DISTRACTORS, mediabox=MEDIA, rotate=0, duplicate_form_copy=True)
    path = _save(pdf, tmp_path)
    d = VectorMatcher().detect(path, 0, box_for(placements[0], MEDIA, 0)).to_dict()
    match_expected(d["detections"], placements, placements[0], MEDIA, 0)


def test_distractor_exemplar_finds_only_distractors(tmp_path):
    placements = standard_placements(8)
    pdf = pikepdf.new()
    build_xobject_page(pdf, placements + DISTRACTORS, mediabox=MEDIA, rotate=0)
    path = _save(pdf, tmp_path)
    d = VectorMatcher().detect(path, 0, box_for(DISTRACTORS[0], MEDIA, 0, margin=2)).to_dict()
    # Two simplex placements, at 0 and 180 degrees.
    assert d["strategy"] == "xobject"
    assert sorted(det["rotation"] for det in d["detections"]) == [0, 180]


def test_nested_forms_use_full_transform(tmp_path):
    """Symbol form placed inside a 'sheet' form that is itself scaled/translated."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(MEDIA[2], MEDIA[3]))
    sym = pdf.make_indirect(form_xobject(pdf, symbol_ops("duplex")))
    inner = [Placement(50 + 40 * i, 60, angle=90 * i) for i in range(4)]
    body = "".join(f"q {' '.join(f'{v:.10g}' for v in p.matrix())} cm /S Do Q\n" for p in inner)
    sheet = form_xobject(pdf, body, bbox=(0, 0, 400, 200))
    sheet.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(S=sym))
    sheet.Matrix = pikepdf.Array([1, 0, 0, 1, 100, 200])  # form /Matrix
    sheet = pdf.make_indirect(sheet)
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Sheet=sheet, S=sym))
    top = [Placement(700, 500, angle=270, mirror=True)]
    page.obj.Contents = pdf.make_stream(
        b"q 1 0 0 1 10 0 cm /Sheet Do Q\n"
        + "".join(f"q {' '.join(f'{v:.10g}' for v in p.matrix())} cm /S Do Q\n" for p in top).encode()
    )
    path = _save(pdf, tmp_path)
    # Expected user-space placement of the inner ones: symbol -> inner cm -> /Matrix -> cm(10, 0).
    expected = [Placement(p.tx + 110, p.ty + 200, p.angle, p.mirror) for p in inner] + top
    d = VectorMatcher().detect(path, 0, box_for(expected[0], MEDIA, 0)).to_dict()
    assert d["strategy"] == "xobject"
    match_expected(d["detections"], expected, expected[0], MEDIA, 0)


def test_non_quarter_angle_is_snapped_with_warning(tmp_path):
    placements = [Placement(200, 200), Placement(300, 200, angle=90), Placement(400, 200, angle=-30)]
    pdf = pikepdf.new()
    build_xobject_page(pdf, placements, mediabox=MEDIA, rotate=0)
    path = _save(pdf, tmp_path)
    d = VectorMatcher().detect(path, 0, box_for(placements[0], MEDIA, 0)).to_dict()
    assert len(d["detections"]) == 3
    odd = [det for det in d["detections"] if "angle" in det]
    assert len(odd) == 1
    # -30 degrees in PDF (y up) is 30 degrees clockwise on screen.
    assert odd[0]["angle"] == pytest.approx(30.0, abs=1e-6)
    assert odd[0]["rotation"] == 0
    assert any("non-quarter-turn" in w for w in d["warnings"])


def test_rotation_filter_and_mirror_filter(tmp_path):
    placements = standard_placements(12)
    pdf = pikepdf.new()
    build_xobject_page(pdf, placements, mediabox=MEDIA, rotate=0)
    path = _save(pdf, tmp_path)
    box = box_for(placements[0], MEDIA, 0)
    d = VectorMatcher().detect(path, 0, box, VectorSettings(rotations=(0,), allow_mirrored=False)).to_dict()
    assert d["detections"] and all(det["rotation"] == 0 and "mirrored" not in det for det in d["detections"])


def test_single_placement_falls_back_to_paths(tmp_path):
    """A lone XObject is not a 'block family'; auto mode uses path matching,
    which also sees geometry drawn inside forms."""
    pdf = pikepdf.new()
    page = build_xobject_page(pdf, [Placement(200, 200)], mediabox=MEDIA, rotate=0,
                              extra_ops="0.5 w\n" + "q 1 0 0 1 400 300 cm\n" + symbol_ops() + "Q\n")
    path = _save(pdf, tmp_path)
    d = VectorMatcher().detect(path, 0, box_for(Placement(200, 200), MEDIA, 0)).to_dict()
    assert d["strategy"] == "paths"
    assert len(d["detections"]) == 2
