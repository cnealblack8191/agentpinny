"""Shared geometry helpers (canonical px, y down, column-vector 2x2 maps)."""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np

from .content import LINE

_R90 = np.array([[0.0, -1.0], [1.0, 0.0]])  # clockwise quarter-turn in y-down
_FLIP = np.array([[-1.0, 0.0], [0.0, 1.0]])  # mirror left-right


def dihedral(rotation: int, mirrored: bool) -> np.ndarray:
    """2x2 map (column vectors) for "mirror left-right, then rotate clockwise
    by ``rotation`` degrees" in the y-down raster."""
    m = np.linalg.matrix_power(_R90, (rotation // 90) % 4)
    return m @ _FLIP if mirrored else m.copy()


#: The 8 dihedral poses as ``(rotation, mirrored, matrix)``, identity first.
DIHEDRAL: List[Tuple[int, bool, np.ndarray]] = [
    (r, m, dihedral(r, m)) for m in (False, True) for r in (0, 90, 180, 270)
]


def decompose(linear: np.ndarray) -> Tuple[float, bool, float]:
    """Split a 2x2 map into ``(clockwise angle in degrees, mirrored, scale)``,
    using the same "mirror first, then rotate" convention as :func:`dihedral`."""
    det = float(np.linalg.det(linear))
    mirrored = det < 0
    scale = math.sqrt(abs(det))
    rot = linear @ _FLIP if mirrored else linear
    angle = math.degrees(math.atan2(rot[1, 0], rot[0, 0])) % 360.0
    return angle, mirrored, scale


def snap_quarter(angle: float) -> Tuple[int, float]:
    """Nearest quarter-turn and the absolute deviation from it, in degrees."""
    snapped = int(round(angle / 90.0)) * 90 % 360
    dev = abs((angle - snapped + 180.0) % 360.0 - 180.0)
    return snapped, dev


def bezier_points(ctrl: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Evaluate cubics ``ctrl`` (N, 4, 2) at per-row parameters ``t`` (N,)."""
    t = t[:, None]
    u = 1.0 - t
    return (
        (u**3) * ctrl[:, 0]
        + (3 * u * u * t) * ctrl[:, 1]
        + (3 * u * t * t) * ctrl[:, 2]
        + (t**3) * ctrl[:, 3]
    )


def control_length(ctrl: np.ndarray) -> np.ndarray:
    """Control-polygon length per cubic (an upper bound on arc length)."""
    return np.linalg.norm(np.diff(ctrl, axis=1), axis=2).sum(axis=1)


def sample(ctrl: np.ndarray, step: float, min_samples: int = 2) -> Tuple[np.ndarray, np.ndarray]:
    """Points spaced at most ~``step`` along each cubic, and their owner index."""
    n = ctrl.shape[0]
    if n == 0:
        return np.zeros((0, 2)), np.zeros((0,), dtype=np.int64)
    counts = np.maximum(min_samples, np.ceil(control_length(ctrl) / step).astype(np.int64) + 1)
    owner = np.repeat(np.arange(n), counts)
    starts = np.cumsum(counts) - counts
    local = np.arange(owner.size) - np.repeat(starts, counts)
    t = local / np.repeat(counts - 1, counts)
    # Straight segments (interior control points on the chord at 1/3, 2/3)
    # are sampled by linear interpolation, which is much cheaper.
    p0, p3 = ctrl[:, 0], ctrl[:, 3]
    straight = np.abs(ctrl[:, 1] - (p0 + (p3 - p0) / 3.0)).max(axis=1) < 1e-9
    straight &= np.abs(ctrl[:, 2] - (p0 + (p3 - p0) * (2.0 / 3.0))).max(axis=1) < 1e-9
    out = np.empty((owner.size, 2))
    s_mask = straight[owner]
    if s_mask.any():
        o = owner[s_mask]
        ts = t[s_mask, None]
        out[s_mask] = p0[o] + ts * (p3[o] - p0[o])
    if not s_mask.all():
        c_mask = ~s_mask
        out[c_mask] = bezier_points(ctrl[owner[c_mask]], t[c_mask])
    return out, owner


def flatten(ctrl: np.ndarray, kind: np.ndarray, curve_steps: int = 12) -> Tuple[np.ndarray, np.ndarray]:
    """Polyline segments ``(K, 2, 2)`` approximating the cubics, and owners."""
    n = ctrl.shape[0]
    if n == 0:
        return np.zeros((0, 2, 2)), np.zeros((0,), dtype=np.int64)
    steps = np.where(kind == LINE, 1, curve_steps)
    owner = np.repeat(np.arange(n), steps)
    starts = np.cumsum(steps) - steps
    local = np.arange(owner.size) - starts[owner]
    t0 = local / steps[owner]
    t1 = (local + 1) / steps[owner]
    c = ctrl[owner]
    return np.stack([bezier_points(c, t0), bezier_points(c, t1)], axis=1), owner


def point_segment_distance(pts: np.ndarray, segs: np.ndarray, chunk: int = 4_000_000) -> np.ndarray:
    """Minimum Euclidean distance from each point (M, 2) to any segment (K, 2, 2)."""
    m = pts.shape[0]
    if m == 0:
        return np.zeros((0,))
    if segs.shape[0] == 0:
        return np.full((m,), np.inf)
    ax, ay = segs[:, 0, 0], segs[:, 0, 1]
    abx, aby = segs[:, 1, 0] - ax, segs[:, 1, 1] - ay
    inv = 1.0 / np.maximum(abx * abx + aby * aby, 1e-12)
    out = np.empty((m,))
    rows = max(1, chunk // max(1, segs.shape[0]))
    for s in range(0, m, rows):
        px = pts[s : s + rows, 0:1] - ax
        py = pts[s : s + rows, 1:2] - ay
        t = np.clip((px * abx + py * aby) * inv, 0.0, 1.0)
        dx = px - t * abx
        dy = py - t * aby
        out[s : s + rows] = np.sqrt((dx * dx + dy * dy).min(axis=1))
    return out


def bbox_of(points: np.ndarray) -> Tuple[float, float, float, float]:
    lo = points.min(axis=0)
    hi = points.max(axis=0)
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))


def iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def box_to_int(b: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
    """Float ``(x0, y0, x1, y1)`` to an integer ``(x, y, width, height)``
    covering it, with width/height >= 1."""
    x0 = int(math.floor(b[0] + 1e-6))
    y0 = int(math.floor(b[1] + 1e-6))
    x1 = int(math.ceil(b[2] - 1e-6))
    y1 = int(math.ceil(b[3] - 1e-6))
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)
