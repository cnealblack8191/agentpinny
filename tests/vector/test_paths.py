"""(b) Flattened symbols with crossing wires, and (c) look-alike distractors."""

from __future__ import annotations

import pikepdf
import pytest

from pinny.vector import VectorMatcher, VectorSettings
from pinny.vector.frame import PT_TO_PX
from pinny.vector.geometry import DIHEDRAL, decompose, dihedral

from .helpers import match_expected
from .pdfgen import (
    Placement,
    box_for,
    build_flat_page,
    build_xobject_page,
    line_ops,
    standard_placements,
)

MEDIA = (0, 0, 1224, 792)
CROP = (20, 30, 1200, 780)
DISTRACTORS = [
    Placement(700, 600, variant="simplex"),
    Placement(760, 600, angle=90, variant="quad"),
    Placement(820, 600, variant="square"),
    Placement(880, 600, angle=180, mirror=True, variant="simplex"),
    Placement(940, 600, angle=270, variant="quad"),
    Placement(1000, 600, angle=90, mirror=True, variant="square"),
]
#: Wires: one horizontal through the first row of symbols' circles, one vertical
#: through the second column, one diagonal.
WIRES = (
    line_ops(60, 120, 520, 120)
    + line_ops(191, 60, 191, 460)
    + line_ops(250, 170, 480, 400)
)


def _page(tmp_path, placements, rotate, *, extra="", name="b.pdf"):
    pdf = pikepdf.new()
    build_flat_page(pdf, placements, mediabox=MEDIA, cropbox=CROP, rotate=rotate, extra_ops=extra)
    path = tmp_path / name
    pdf.save(path)
    return path


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_flattened_symbols_with_crossing_wires(tmp_path, rotate):
    placements = standard_placements(20)
    path = _page(tmp_path, placements + DISTRACTORS, rotate, extra=WIRES)
    exemplar = placements[7]
    result = VectorMatcher().detect(path, 0, box_for(exemplar, CROP, rotate))
    d = result.to_dict()
    assert d["strategy"] == "paths"
    assert d["detector"]["name"] == "pinny-vector-paths"
    assert d["threshold"] == 0.9 and d["detector"]["settings"]["threshold"] == 0.9
    match_expected(d["detections"], placements, exemplar, CROP, rotate)
    for det in d["detections"]:
        assert det["source"] == "vector-path"
        assert 0.9 <= det["score"] <= 1.0
    # Some symbols really were crossed by a wire (not signature matches).
    assert d["stats"]["signature_matches"] < 20


def test_exemplar_crossed_by_a_wire(tmp_path):
    placements = standard_placements(20)
    path = _page(tmp_path, placements + DISTRACTORS, 0, extra=WIRES)
    exemplar = placements[0]  # the horizontal wire runs through it
    d = VectorMatcher().detect(path, 0, box_for(exemplar, CROP, 0)).to_dict()
    match_expected(d["detections"], placements, exemplar, CROP, 0)


def test_generous_exemplar_box(tmp_path):
    placements = standard_placements(12)
    path = _page(tmp_path, placements + DISTRACTORS, 0)
    d = VectorMatcher().detect(path, 0, box_for(placements[3], CROP, 0, margin=12)).to_dict()
    match_expected(d["detections"], placements, placements[3], CROP, 0)


@pytest.mark.parametrize("variant", ["simplex", "quad", "square"])
def test_distractors_alone_do_not_match(tmp_path, variant):
    """Only look-alikes (and wires) on the page besides the exemplar."""
    target = Placement(150, 150)
    looks = [Placement(250 + 60 * i, 150 + 50 * (i % 2), angle=90 * i, mirror=i % 2 == 1, variant=variant)
             for i in range(8)]
    path = _page(tmp_path, [target] + looks, 0, extra=line_ops(230, 150, 800, 150))
    d = VectorMatcher().detect(path, 0, box_for(target, CROP, 0)).to_dict()
    assert len(d["detections"]) == 1, d["detections"]


def test_distractor_scores_are_below_threshold(tmp_path):
    """With threshold lowered, look-alikes appear with the scores we expect to reject."""
    target = Placement(150, 150)
    looks = [Placement(300, 150, variant="simplex"), Placement(400, 150, variant="quad"),
             Placement(500, 150, variant="square")]
    path = _page(tmp_path, [target] + looks, 0)
    d = VectorMatcher().detect(path, 0, box_for(target, CROP, 0), VectorSettings(threshold=0.5)).to_dict()
    scores = {round(det["x"] / PT_TO_PX): det["score"] for det in d["detections"]}
    assert scores[round((150 - 20))] == pytest.approx(1.0)
    # simplex (two lines missing) and quad (extra line inside) are scored but rejected
    assert 0.5 <= scores[300 - 20] < 0.9, scores
    assert 0.5 <= scores[400 - 20] < 0.9, scores
    # the square outline covers too little of the circle to score even 0.5
    assert scores.get(500 - 20, 0.0) < 0.9, scores


def test_scaled_symbol_is_not_matched(tmp_path):
    target = Placement(150, 150)
    pdf = pikepdf.new()
    from .pdfgen import symbol_ops

    extra = "q 1.3 0 0 1.3 400 150 cm " + symbol_ops() + "Q\n"
    build_flat_page(pdf, [target], mediabox=MEDIA, extra_ops=extra)
    path = tmp_path / "scaled.pdf"
    pdf.save(path)
    d = VectorMatcher().detect(path, 0, box_for(target, MEDIA, 0)).to_dict()
    assert len(d["detections"]) == 1
    loose = VectorMatcher().detect(path, 0, box_for(target, MEDIA, 0),
                                   VectorSettings(scale_tolerance=0.35)).to_dict()
    assert len(loose["detections"]) == 2  # isolated scaled copy found by the signature path


def test_paths_strategy_on_xobject_page(tmp_path):
    placements = standard_placements(10)
    pdf = pikepdf.new()
    build_xobject_page(pdf, placements + DISTRACTORS, mediabox=MEDIA, cropbox=CROP, rotate=90)
    path = tmp_path / "x.pdf"
    pdf.save(path)
    d = VectorMatcher().detect(path, 0, box_for(placements[1], CROP, 90),
                               VectorSettings(strategy="paths")).to_dict()
    assert d["strategy"] == "paths"
    match_expected(d["detections"], placements, placements[1], CROP, 90)


def test_rotation_restriction(tmp_path):
    placements = standard_placements(8)
    path = _page(tmp_path, placements, 0)
    d = VectorMatcher().detect(path, 0, box_for(placements[0], CROP, 0),
                               VectorSettings(rotations=(0, 180), allow_mirrored=False)).to_dict()
    assert d["detections"]
    assert all(det["rotation"] in (0, 180) and "mirrored" not in det for det in d["detections"])


def test_dihedral_conventions():
    # Clockwise on screen (y down): right -> down.
    assert tuple(dihedral(90, False) @ [1, 0]) == (0, 1)
    for r, m, mat in DIHEDRAL:
        angle, mirrored, scale = decompose(mat)
        assert (round(angle) % 360, mirrored, round(scale, 9)) == (r, m, 1.0)


def test_symmetric_symbol_gets_stable_labels(tmp_path):
    """A symbol symmetric under 180 degrees and mirroring: every instance is
    labelled with the smallest equivalent rotation and never 'mirrored'."""
    from .pdfgen import circle_ops

    sym = circle_ops(6) + line_ops(-2, -4, -2, 4) + line_ops(2, -4, 2, 4)
    ops = "".join(
        f"q {' '.join(f'{v:.10g}' for v in Placement(100 + 50 * i, 200, angle=90 * i, mirror=i % 3 == 1).matrix())} cm {sym}Q\n"
        for i in range(8)
    )
    pdf = pikepdf.new()
    build_flat_page(pdf, [], mediabox=MEDIA, extra_ops=ops)
    path = tmp_path / "sym.pdf"
    pdf.save(path)
    b = PT_TO_PX
    box = (int((100 - 8) * b), int((792 - 208) * b), int(16 * b), int(16 * b))
    d = VectorMatcher().detect(path, 0, box).to_dict()
    assert len(d["detections"]) == 8
    assert all(det["rotation"] in (0, 90) and "mirrored" not in det for det in d["detections"])
