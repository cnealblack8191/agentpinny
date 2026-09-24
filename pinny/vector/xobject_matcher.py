"""Block reuse matcher: find every placement of the Form XObject under the exemplar.

CAD exporters (AutoCAD, Revit, MicroStation, Bluebeam) often write each block
definition once as a Form XObject and place it with ``cm ... /Name Do``. When
the user's exemplar box matches one such placement, every other placement of
the same XObject is the same symbol, with an exact transform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .content import PageContent, Placement
from .frame import apply
from .geometry import bbox_of, decompose, iou, snap_quarter
from ._types import RawDetection

_CURVE_T = np.linspace(0.0, 1.0, 17)


def primitive_boxes_px(content: PageContent) -> np.ndarray:
    """``(N, 4)`` px bounding boxes (x0, y0, x1, y1) of every primitive."""
    n = content.ctrl.shape[0]
    if n == 0:
        return np.zeros((0, 4))
    t = _CURVE_T[None, :, None]
    u = 1.0 - t
    c = content.ctrl[:, None]  # (N, 1, 4, 2)
    pts = (u**3) * c[:, :, 0] + 3 * u * u * t * c[:, :, 1] + 3 * u * t * t * c[:, :, 2] + (t**3) * c[:, :, 3]
    px = apply(content.frame.user_to_px, pts)  # (N, 17, 2)
    return np.concatenate([px.min(axis=1), px.max(axis=1)], axis=1)


@dataclass
class PlacedForm:
    placement: Placement
    box: Tuple[float, float, float, float]  # canonical px, x0 y0 x1 y1
    linear: np.ndarray  # 2x2, column vectors: form space -> canonical px


def place_forms(content: PageContent, prim_boxes: np.ndarray) -> List[PlacedForm]:
    to_px = content.frame.user_to_px
    out: List[PlacedForm] = []
    for p in content.placements:
        full = p.matrix @ to_px
        if p.prim_end > p.prim_start:
            b = prim_boxes[p.prim_start : p.prim_end]
            box = (float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 2].max()), float(b[:, 3].max()))
        else:
            x0, y0, x1, y1 = p.bbox_form
            corners = apply(full, np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], dtype=float))
            box = bbox_of(corners)
        out.append(PlacedForm(placement=p, box=box, linear=full[:2, :2].T.copy()))
    return out


@dataclass
class XObjectMatch:
    exemplar: PlacedForm
    exemplar_iou: float
    detections: List[RawDetection]
    warnings: List[str]


def match_xobjects(
    content: PageContent,
    exemplar_box: Tuple[float, float, float, float],
    *,
    min_iou: float,
    rotations: Sequence[int],
    allow_mirrored: bool,
    angle_tolerance_deg: float,
    prim_boxes: Optional[np.ndarray] = None,
) -> Optional[XObjectMatch]:
    """Return every placement of the XObject whose placement best fits the box,
    or ``None`` if no placement reaches ``min_iou``."""
    if not content.placements:
        return None
    if prim_boxes is None:
        prim_boxes = primitive_boxes_px(content)
    placed = place_forms(content, prim_boxes)
    best: Optional[PlacedForm] = None
    best_key = (-1.0, -1)
    for pf in placed:
        score = iou(pf.box, exemplar_box)
        key = (round(score, 6), pf.placement.depth)
        if key > best_key:
            best, best_key = pf, key
    if best is None or best_key[0] < min_iou:
        return None

    ref_inv = np.linalg.inv(best.linear) if abs(np.linalg.det(best.linear)) > 1e-12 else None
    if ref_inv is None:
        return None
    warnings: List[str] = []
    dets: List[RawDetection] = []
    n_off_angle = 0
    for pf in placed:
        if pf.placement.group_key != best.placement.group_key:
            continue
        rel = pf.linear @ ref_inv
        angle, mirrored, scale = decompose(rel)
        rotation, dev = snap_quarter(angle)
        if dev > angle_tolerance_deg:
            n_off_angle += 1
        if rotation not in rotations or (mirrored and not allow_mirrored):
            continue
        dets.append(
            RawDetection(
                box=pf.box,
                score=1.0,
                rotation=rotation,
                mirrored=mirrored,
                angle=angle if dev > angle_tolerance_deg else None,
                scale=scale,
                source="vector-xobject",
            )
        )
    if n_off_angle:
        warnings.append(
            f"{n_off_angle} placement(s) are rotated by a non-quarter-turn angle; their "
            "rotation is snapped to the nearest quarter turn and the exact angle is "
            "reported as 'angle'."
        )
    return XObjectMatch(exemplar=best, exemplar_iou=best_key[0], detections=dets, warnings=warnings)
