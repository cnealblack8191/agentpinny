"""Runtime on a dense Arch D sheet: 500 symbols plus 20,000 extra line segments.

The measured times are recorded in docs/vector-matching.md. The assertion
bound is deliberately loose so slow CI machines don't flake.
"""

from __future__ import annotations

import time

import pikepdf
import pytest

from pinny.vector import VectorMatcher

from .helpers import match_expected
from .pdfgen import Placement, box_for, build_flat_page, build_xobject_page, random_lines_ops

W, H = 2592, 1728  # Arch D, 36 x 24 in
MEDIA = (0, 0, W, H)
PLACEMENTS = [
    Placement(60 + 100 * (i % 25), 60 + 80 * (i // 25), angle=90.0 * (i % 4), mirror=(i % 7 == 3))
    for i in range(500)
]


@pytest.mark.parametrize("builder,strategy", [(build_flat_page, "paths"), (build_xobject_page, "xobject")])
def test_dense_sheet_runtime(tmp_path, builder, strategy):
    pdf = pikepdf.new()
    builder(pdf, PLACEMENTS, mediabox=MEDIA, extra_ops=random_lines_ops(20_000, W, H, seed=3))
    path = tmp_path / "dense.pdf"
    pdf.save(path)
    exemplar = PLACEMENTS[26]
    t0 = time.perf_counter()
    result = VectorMatcher().detect(path, 0, box_for(exemplar, MEDIA, 0))
    elapsed = time.perf_counter() - t0
    d = result.to_dict()
    print(f"\n[vector benchmark] {strategy}: {len(d['detections'])} detections, "
          f"{d['stats'].get('primitives')} primitives, {elapsed:.2f} s, stats={d['stats']}")
    assert d["strategy"] == strategy
    match_expected(d["detections"], PLACEMENTS, exemplar, MEDIA, 0)
    assert elapsed < 30.0
