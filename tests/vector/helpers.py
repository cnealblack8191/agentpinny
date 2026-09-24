"""Assertion helpers shared by the vector tests."""

from __future__ import annotations

from typing import Sequence

from .pdfgen import Placement, expected_box_px, expected_pose


def match_expected(detections: Sequence[dict], expected: Sequence[Placement], exemplar: Placement,
                   crop, rotate, *, box_tol: float = 2.0) -> None:
    """Every expected placement has exactly one detection whose box is within
    ``box_tol`` px on every edge and whose rotation/mirror label is right;
    nothing else was detected."""
    assert len(detections) == len(expected), (len(detections), len(expected))
    unused = list(detections)
    for p in expected:
        x0, y0, x1, y1 = expected_box_px(p, crop, rotate)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        det = min(unused, key=lambda d: (d["x"] - cx) ** 2 + (d["y"] - cy) ** 2)
        unused.remove(det)
        b = det["box"]
        edges = (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
        for got, want in zip(edges, (x0, y0, x1, y1)):
            assert abs(got - want) <= box_tol, (p, b, (x0, y0, x1, y1))
        assert abs(det["x"] - cx) <= box_tol and abs(det["y"] - cy) <= box_tol
        rotation, mirrored = expected_pose(exemplar, p, crop, rotate)
        assert det["rotation"] == rotation, (p, det)
        assert bool(det.get("mirrored", False)) == mirrored, (p, det)
