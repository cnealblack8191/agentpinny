"""Whole-set scanning with a learned package per symbol type
(pinny.model.symbols): crossed-out receptacles (demolished devices) match the
legend's duplex, a reviewer rejects them on two sheets, and the learned
package then throws them out on a new sheet while keeping the real ones."""

from __future__ import annotations

import math

import numpy as np
import pytest

from pinny.detection.verifier import crop_with_margin
from pinny.legend import read_legend
from pinny.model.judge import Example
from pinny.model.symbols import train_symbol
from pinny.scan import render_page, scan_set

from tests.legend.pdfgen import Canvas, build, draw_legend, place
from tests.scan.test_scan import LAYOUT, LEGEND_ROWS, PX

REAL = [(150, 150), (300, 150), (450, 150), (150, 400), (300, 400)]
CROSSED = [(600, 150), (750, 150), (600, 400)]


def sheet(real, crossed, shift=0):
    c = Canvas()
    c.rect(80, 80, 1000, 600)
    for x, y in real:
        place(c, "duplex", x + shift, y, 0)
    for x, y in crossed:
        place(c, "duplex", x + shift, y, 0)
        c.line(x + shift - 14, y + 14, x + shift + 14, y - 14, 2.0)  # "remove": a heavy slash
    return c


@pytest.fixture(scope="module")
def crossed_set(tmp_path_factory):
    leg = Canvas()
    draw_legend(leg, LEGEND_ROWS, LAYOUT)
    path = tmp_path_factory.mktemp("learned") / "set.pdf"
    build([(leg, 0, None), (sheet(REAL, CROSSED), 0, None), (sheet(REAL, CROSSED, 20), 0, None),
           (sheet(REAL, CROSSED, 40), 0, None)], str(path))
    legend = read_legend(path)
    return path, legend, scan_set(path, legend)


def is_near(d, pts, shift):
    cx, cy = d.center
    return any(math.hypot(cx - (x + shift) * PX, cy - y * PX) < 25 for x, y in pts)


def test_crossed_out_receptacles_match_the_legend_without_learning(crossed_set):
    _, _, res = crossed_set
    for s, shift in zip(res.sheets, (0, 20, 40)):
        ds = [d for d in s.detections if d.tag == "D"]
        assert sum(is_near(d, CROSSED, shift) for d in ds) == len(CROSSED)
        assert sum(is_near(d, REAL, shift) for d in ds) == len(REAL)
    assert not res.learned and all(not s.learned for s in res.sheets)


def train_from_sheets(path, legend, res, pages):
    """Review sheets ``pages``: approve the real receptacles, reject the crossed ones."""
    entry = next(e for e in legend.entries if e.tag == "D")
    examples = []
    for s in res.sheets:
        if s.page_index not in pages:
            continue
        img = render_page(path, s.page_index)
        shift = (0, 20, 40)[s.page_index - 1]
        for d in s.detections:
            if d.tag == "D":
                examples.append(Example(crop_with_margin(img, d.box), not is_near(d, CROSSED, shift),
                                        f"sheet{s.page_index}", d.rotation))
    return train_symbol("D", res.symbol_templates[entry.id], examples)


def test_a_learned_package_throws_out_the_crossed_ones_on_a_new_sheet(crossed_set):
    path, legend, res = crossed_set
    t = train_from_sheets(path, legend, res, {1, 2})
    assert t.active, t.reason
    out = scan_set(path, legend, packages={"D": t.package})
    assert out.learned == {"D": {"used": True, "reason": "Used the learned examples of this symbol."}}
    new = out.sheets[2]  # never reviewed
    ds = [d for d in new.detections if d.tag == "D"]
    assert sum(is_near(d, CROSSED, 40) for d in ds) == 0
    assert sum(is_near(d, REAL, 40) for d in ds) == len(REAL)
    assert new.learned["rejected"] == {"D": len(CROSSED)}
    assert new.to_dict()["learned"]["rejected"] == {"D": len(CROSSED)}
    # Other symbol types are untouched.
    assert {t: n for t, n in new.counts().items() if t != "D"} == {
        t: n for t, n in res.sheets[2].counts().items() if t != "D"}


def test_a_package_trained_on_another_legend_drawing_is_not_used(crossed_set):
    path, legend, res = crossed_set
    t = train_from_sheets(path, legend, res, {1, 2})
    # Pretend "Q" means this duplex elsewhere: the Q legend drawing differs.
    out = scan_set(path, legend, packages={"Q": t.package})
    assert out.learned["Q"]["used"] is False and "different legend drawing" in out.learned["Q"]["reason"]
    assert out.counts() == res.counts()
