"""Flattened-path matcher.

Used when the exporter "exploded" blocks into plain path operators, so there
is no XObject to group by. All geometry is in canonical px.

1. **Primitives.** Every painted path segment (line or cubic), including
   those drawn inside Form XObjects, with its full CTM applied.
2. **Exemplar.** The primitives lying entirely inside the exemplar box (a
   wire that only crosses the box is excluded because it extends past it).
3. **Connected components** of primitives whose bounding boxes lie within
   the tolerance of each other.
4. **Fast path: signature hash.** For the exemplar and every component with
   the same primitive count, the endpoints/control points are normalised by
   centroid and RMS radius, transformed by each of the 8 dihedral maps
   (4 quarter turns x optional mirror), quantised and sorted. Equal
   descriptors give the pose directly.
5. **General path: anchor hypotheses.** Components that touch other line
   work (a wire drawn through the symbol) don't hash equal. For them, a
   distinctive exemplar primitive (the anchor) is paired with every page
   primitive of the same type and length; each of the 8 dihedral maps that
   carries the anchor's control points onto the candidate's gives a pose.
   Poses are pre-filtered with a coarse "ink nearby" bitmap.
6. **Scoring.** For a pose, ``coverage`` is the fraction of exemplar sample
   points that lie within ``tol`` of page line work (a directed, thresholded
   Chamfer distance; extra line work such as a crossing wire does not lower
   it). ``precision`` penalises extra primitives lying *entirely inside* the
   placed symbol that the exemplar doesn't explain (a look-alike symbol with
   an extra mark): ``L_exemplar / (L_exemplar + L_extra)``.
   ``score = coverage * precision`` in [0, 1].
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .content import LINE
from .errors import VectorMatchError
from .geometry import (
    DIHEDRAL,
    control_length,
    flatten,
    point_segment_distance,
    sample,
)
from ._types import RawDetection

#: Primitives whose bounding box spans more grid cells than this are kept in
#: a side list and returned by every region query.
_MAX_CELLS_PER_PRIM = 4096
#: Grid cells for the coarse ink bitmap are never more than this many.
_MAX_BITMAP_CELLS = 60_000_000


@dataclass
class PathParams:
    threshold: float
    tol_px: float
    rotations: Sequence[int]
    allow_mirrored: bool
    scale_tolerance: float
    max_anchors: int
    max_hypotheses: int
    extra_match_fraction: float
    descriptor_quantum: float
    check_deadline: Callable[[], None]


# --------------------------------------------------------------------------
# Page index
# --------------------------------------------------------------------------


class PageIndex:
    """Spatial index, flattened segments and a coarse ink bitmap for a page."""

    def __init__(self, ctrl: np.ndarray, kind: np.ndarray, boxes: np.ndarray,
                 tol: float, cell: float, page_w: float, page_h: float) -> None:
        self.ctrl = ctrl
        self.kind = kind
        self.boxes = boxes
        self.tol = tol
        self.n = ctrl.shape[0]
        self.chord = np.linalg.norm(ctrl[:, 3] - ctrl[:, 0], axis=1) if self.n else np.zeros(0)
        self.segs, seg_owner = flatten(ctrl, kind)
        self.seg_start = np.searchsorted(seg_owner, np.arange(self.n), side="left")
        self.seg_end = np.searchsorted(seg_owner, np.arange(self.n), side="right")
        seg_len = np.linalg.norm(self.segs[:, 1] - self.segs[:, 0], axis=1) if self.n else np.zeros(0)
        self.length = np.bincount(seg_owner, weights=seg_len, minlength=self.n) if self.n else np.zeros(0)
        self._build_grid(cell, page_w, page_h)
        self._build_bitmap(page_w, page_h)

    # -- region grid -------------------------------------------------------

    def _build_grid(self, cell: float, page_w: float, page_h: float) -> None:
        self.cell = cell
        b = self.boxes
        lo = np.floor(np.minimum(b[:, :2], b[:, 2:]) / cell).astype(np.int64) if self.n else np.zeros((0, 2), np.int64)
        hi = np.floor(b[:, 2:] / cell).astype(np.int64) if self.n else np.zeros((0, 2), np.int64)
        self.gx0 = int(lo[:, 0].min()) if self.n else 0
        self.gy0 = int(lo[:, 1].min()) if self.n else 0
        lo = lo - [self.gx0, self.gy0]
        hi = hi - [self.gx0, self.gy0]
        self.ncols = int(hi[:, 0].max()) + 1 if self.n else 1
        w = hi[:, 0] - lo[:, 0] + 1
        h = hi[:, 1] - lo[:, 1] + 1
        counts = w * h
        big = counts > _MAX_CELLS_PER_PRIM
        self.big = np.nonzero(big)[0]
        idx = np.nonzero(~big)[0]
        counts = counts[idx]
        rep = np.repeat(idx, counts)
        starts = np.cumsum(counts) - counts
        local = np.arange(rep.size) - np.repeat(starts, counts)
        wr = w[rep]
        cx = lo[rep, 0] + local % wr
        cy = lo[rep, 1] + local // wr
        keys = cy * self.ncols + cx
        order = np.argsort(keys, kind="stable")
        self.keys = keys[order]
        self.key_prims = rep[order]

    def query(self, rect: Tuple[float, float, float, float]) -> np.ndarray:
        """Indices of primitives whose bbox may intersect ``rect`` (x0, y0, x1, y1)."""
        cx0 = int(math.floor(rect[0] / self.cell)) - self.gx0
        cx1 = int(math.floor(rect[2] / self.cell)) - self.gx0
        cy0 = int(math.floor(rect[1] / self.cell)) - self.gy0
        cy1 = int(math.floor(rect[3] / self.cell)) - self.gy0
        cx0, cx1 = max(cx0, 0), min(cx1, self.ncols - 1)
        parts = [self.big]
        if cx1 >= cx0:
            rows = np.arange(max(cy0, 0), max(cy1 + 1, 0))
            if rows.size:
                lo = np.searchsorted(self.keys, rows * self.ncols + cx0, side="left")
                hi = np.searchsorted(self.keys, rows * self.ncols + cx1, side="right")
                for a, z in zip(lo, hi):
                    if z > a:
                        parts.append(self.key_prims[a:z])
        ids = np.unique(np.concatenate(parts)) if len(parts) > 1 else self.big.copy()
        if ids.size:
            b = self.boxes[ids]
            keep = (b[:, 0] <= rect[2]) & (b[:, 2] >= rect[0]) & (b[:, 1] <= rect[3]) & (b[:, 3] >= rect[1])
            ids = ids[keep]
        return ids

    def segs_of(self, ids: np.ndarray) -> np.ndarray:
        if ids.size == 0:
            return np.zeros((0, 2, 2))
        parts = [self.segs[self.seg_start[i] : self.seg_end[i]] for i in ids]
        return np.concatenate(parts, axis=0)

    # -- coarse ink bitmap -------------------------------------------------

    def _build_bitmap(self, page_w: float, page_h: float) -> None:
        tol = self.tol
        # With cell == tol and step == tol, the dilation radius below is 2
        # cells, the same as with finer sampling, at half the samples.
        step = tol
        margin = 8.0 * tol
        lo = np.array([-margin, -margin])
        extent = np.array([page_w + 2 * margin, page_h + 2 * margin])
        cell = tol
        while (extent[0] / cell) * (extent[1] / cell) > _MAX_BITMAP_CELLS:
            cell *= 1.5
        shape = (int(extent[1] // cell) + 2, int(extent[0] // cell) + 2)
        ink = np.zeros(shape, dtype=bool)
        if self.n:
            pts, _ = sample(self.ctrl, step)
            ij = np.floor((pts - lo) / cell).astype(np.int64)
            ok = (ij[:, 0] >= 0) & (ij[:, 1] >= 0) & (ij[:, 0] < shape[1]) & (ij[:, 1] < shape[0])
            ink[ij[ok, 1], ij[ok, 0]] = True
        # Any point within tol of ink has a sample within tol + step/2, i.e.
        # at most floor(R / cell) + 1 cells away on each axis.
        r = int(math.floor((tol + step / 2.0) / cell)) + 1
        near = ink.copy()
        for axis in (0, 1):
            src = near.copy()
            for k in range(1, r + 1):
                if axis == 0:
                    near[k:] |= src[:-k]
                    near[:-k] |= src[k:]
                else:
                    near[:, k:] |= src[:, :-k]
                    near[:, :-k] |= src[:, k:]
        self.near = near
        self.bm_lo = lo
        self.bm_cell = cell

    def coarse_coverage(self, pts: np.ndarray) -> np.ndarray:
        """``pts`` (H, M, 2) -> (H,) fraction of points on a near-ink cell.
        Never below the exact coverage at ``tol``."""
        ij = np.floor((pts - self.bm_lo) / self.bm_cell).astype(np.int64)
        h, w = self.near.shape
        ok = (ij[..., 0] >= 0) & (ij[..., 1] >= 0) & (ij[..., 0] < w) & (ij[..., 1] < h)
        ix = np.clip(ij[..., 0], 0, w - 1)
        iy = np.clip(ij[..., 1], 0, h - 1)
        return (self.near[iy, ix] & ok).mean(axis=1)

    # -- connected components ---------------------------------------------

    def components(self) -> np.ndarray:
        """Component label per primitive (bboxes within ``tol`` are connected)."""
        n = self.n
        parent = np.arange(n)
        if n == 0:
            return parent
        keys, prims = self.keys, self.key_prims
        pairs_a: List[np.ndarray] = []
        pairs_b: List[np.ndarray] = []
        if keys.size > 1:
            # Longest run of equal keys bounds the offsets we need.
            change = np.flatnonzero(np.diff(keys)) + 1
            bounds = np.concatenate([[0], change, [keys.size]])
            max_run = int(np.diff(bounds).max())
            for k in range(1, max_run):
                same = keys[k:] == keys[:-k]
                if not same.any():
                    break
                pairs_a.append(prims[:-k][same])
                pairs_b.append(prims[k:][same])
        for i in self.big:
            pairs_a.append(np.full(n, i))
            pairs_b.append(np.arange(n))
        if not pairs_a:
            return parent
        a = np.concatenate(pairs_a)
        b = np.concatenate(pairs_b)
        t = self.tol
        ba, bb = self.boxes[a], self.boxes[b]
        touch = (
            (ba[:, 0] <= bb[:, 2] + t) & (bb[:, 0] <= ba[:, 2] + t)
            & (ba[:, 1] <= bb[:, 3] + t) & (bb[:, 1] <= ba[:, 3] + t) & (a != b)
        )
        a, b = a[touch], b[touch]
        if a.size == 0:
            return parent
        pk = np.unique(np.minimum(a, b) * n + np.maximum(a, b))
        a, b = pk // n, pk % n
        # Vectorised union-find by repeated min-label propagation with pointer jumping.
        label = np.arange(n)
        while True:
            la, lb = label[a], label[b]
            m = np.minimum(la, lb)
            new = label.copy()
            np.minimum.at(new, la, m)
            np.minimum.at(new, lb, m)
            # pointer jumping
            while True:
                nxt = new[new]
                if np.array_equal(nxt, new):
                    break
                new = nxt
            if np.array_equal(new, label):
                break
            label = new
        return label


# --------------------------------------------------------------------------
# Exemplar
# --------------------------------------------------------------------------


@dataclass
class Exemplar:
    ids: np.ndarray
    ctrl: np.ndarray
    kind: np.ndarray
    pts: np.ndarray  # sample points
    segs: np.ndarray  # flattened segments
    length: float
    bbox: Tuple[float, float, float, float]
    centroid: np.ndarray
    scale: float
    canon_hash: bytes
    canon_dihedral: int


def _centroid_scale(ctrl: np.ndarray, step: float) -> Tuple[np.ndarray, float]:
    pts, _ = sample(ctrl, step)
    c = pts.mean(axis=0)
    s = float(np.sqrt(((pts - c) ** 2).sum(axis=1).mean()))
    return c, max(s, 1e-9)


def descriptor_hashes(ctrl: np.ndarray, centroid: np.ndarray, scale: float,
                      quantum: float) -> List[bytes]:
    """Sorted, quantised control-point descriptor under each of the 8 dihedral maps."""
    pts = (ctrl.reshape(-1, 2) - centroid) / scale
    out = []
    for _r, _m, d in DIHEDRAL:
        q = np.round((pts @ d.T) / quantum).astype(np.int64)
        q = q[np.lexsort((q[:, 1], q[:, 0]))]
        out.append(hashlib.sha1(q.tobytes()).digest())
    return out


def build_exemplar(index: PageIndex, box: Tuple[float, float, float, float],
                   params: PathParams, max_prims: int) -> Exemplar:
    t = params.tol_px
    b = index.boxes
    cand = index.query(box)
    inside = cand[
        (b[cand, 0] >= box[0] - t) & (b[cand, 1] >= box[1] - t)
        & (b[cand, 2] <= box[2] + t) & (b[cand, 3] <= box[3] + t)
    ]
    # Ignore zero-length specks (dots from dash patterns, hairline caps).
    inside = inside[index.length[inside] > 1e-6]
    if inside.size == 0:
        raise VectorMatchError(
            "no_vector_geometry",
            "No vector line work lies entirely inside the exemplar box. Draw the box "
            "around the whole symbol, or use the raster matcher if the symbol is text "
            "or part of an image.",
        )
    if inside.size > max_prims:
        raise VectorMatchError(
            "invalid_exemplar_box",
            f"The exemplar box contains {inside.size} primitives (limit {max_prims}). "
            "Draw a tighter box around a single symbol.",
        )
    ctrl = index.ctrl[inside]
    kind = index.kind[inside]
    step = t / 2.0
    pts, _ = sample(ctrl, t)  # coverage sample points, <= tol apart
    segs, _ = flatten(ctrl, kind)
    lo = index.boxes[inside, :2].min(axis=0)
    hi = index.boxes[inside, 2:].max(axis=0)
    c, s = _centroid_scale(ctrl, step)
    hashes = descriptor_hashes(ctrl, c, s, params.descriptor_quantum)
    canon = min(hashes)
    return Exemplar(
        ids=inside, ctrl=ctrl, kind=kind, pts=pts, segs=segs,
        length=float(index.length[inside].sum()),
        bbox=(float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])),
        centroid=c, scale=s, canon_hash=canon, canon_dihedral=hashes.index(canon),
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _transform_box(bbox, A: np.ndarray, b: np.ndarray) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    corners = np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]]) @ A.T + b
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))


def score_pose(index: PageIndex, ex: Exemplar, A: np.ndarray, b: np.ndarray,
               params: PathParams) -> Tuple[float, float, Tuple[float, float, float, float]]:
    """Exact ``(score, coverage, box)`` for the exemplar placed by ``x -> A x + b``."""
    t = params.tol_px
    box = _transform_box(ex.bbox, A, b)
    region = (box[0] - 2 * t, box[1] - 2 * t, box[2] + 2 * t, box[3] + 2 * t)
    local = index.query(region)
    pts = ex.pts @ A.T + b
    d = point_segment_distance(pts, index.segs_of(local))
    coverage = float((d <= t).mean())
    if coverage < params.threshold:
        return coverage, coverage, box
    bb = index.boxes[local]
    contained = local[
        (bb[:, 0] >= box[0] - t) & (bb[:, 1] >= box[1] - t)
        & (bb[:, 2] <= box[2] + t) & (bb[:, 3] <= box[3] + t)
    ]
    extra = 0.0
    if contained.size:
        placed_segs = ex.segs @ A.T + b  # (K, 2, 2) @ (2, 2) works row-wise
        cpts, owner = sample(index.ctrl[contained], t / 2.0)
        cd = point_segment_distance(cpts, placed_segs)
        near = np.bincount(owner, weights=(cd <= t).astype(float), minlength=contained.size)
        total = np.bincount(owner, minlength=contained.size)
        unexplained = near / np.maximum(total, 1) < params.extra_match_fraction
        extra = float(index.length[contained[unexplained]].sum())
    precision = ex.length / (ex.length + extra) if ex.length > 0 else 0.0
    return coverage * precision, coverage, box


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _hausdorff(a: np.ndarray, b: np.ndarray) -> float:
    d = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2))
    return float(max(d.min(axis=1).max(), d.min(axis=0).max()))


def _allowed_dihedral(params: PathParams) -> List[int]:
    return [
        i for i, (r, m, _d) in enumerate(DIHEDRAL)
        if r in params.rotations and (params.allow_mirrored or not m)
    ]


def _dihedral_index(mat: np.ndarray) -> int:
    for i, (_r, _m, d) in enumerate(DIHEDRAL):
        if np.allclose(mat, d, atol=1e-6):
            return i
    raise AssertionError("not a dihedral matrix")


@dataclass
class PathMatchStats:
    primitives: int = 0
    exemplar_primitives: int = 0
    components: int = 0
    signature_matches: int = 0
    hypotheses: int = 0
    hypotheses_verified: int = 0


def match_paths(index: PageIndex, box: Tuple[float, float, float, float],
                params: PathParams, max_exemplar_primitives: int
                ) -> Tuple[List[RawDetection], PathMatchStats, Exemplar]:
    stats = PathMatchStats(primitives=index.n)
    ex = build_exemplar(index, box, params, max_exemplar_primitives)
    stats.exemplar_primitives = int(ex.ids.size)
    allowed = _allowed_dihedral(params)
    allowed_set = set(allowed)
    step = params.tol_px / 2.0
    dets: List[RawDetection] = []
    consumed = np.zeros(index.n, dtype=bool)

    def add(score: float, coverage: float, bx, d_idx: int, scale: float = 1.0) -> None:
        r, m, _ = DIHEDRAL[d_idx]
        dets.append(RawDetection(box=bx, score=score, rotation=r, mirrored=m,
                                 source="vector-path", scale=scale, coverage=coverage))

    # ---- fast path: components with an equal signature -------------------
    labels = index.components()
    params.check_deadline()
    uniq, inv, counts = np.unique(labels, return_inverse=True, return_counts=True)
    stats.components = int(uniq.size)
    n_ex = ex.ids.size
    order = np.argsort(inv, kind="stable")
    starts = np.cumsum(counts) - counts
    d_e = DIHEDRAL[ex.canon_dihedral][2]
    ex_norm = (ex.ctrl.reshape(-1, 2) - ex.centroid) / ex.scale
    for ci in np.nonzero(counts == n_ex)[0]:
        ids = order[starts[ci] : starts[ci] + counts[ci]]
        c, s = _centroid_scale(index.ctrl[ids], step)
        ratio = s / ex.scale
        if abs(ratio - 1.0) > params.scale_tolerance:
            continue
        hashes = descriptor_hashes(index.ctrl[ids], c, s, params.descriptor_quantum)
        best: Optional[int] = None
        for j, h in enumerate(hashes):
            if h != ex.canon_hash:
                continue
            rel = _dihedral_index(DIHEDRAL[j][2].T @ d_e)
            if rel in allowed_set and (best is None or rel < best):
                best = rel
        if best is None:
            # Quantisation can split equal shapes across a rounding boundary:
            # fall back to a tolerant symmetric Hausdorff test per dihedral map.
            comp_norm = (index.ctrl[ids].reshape(-1, 2) - c) / s
            lim = 2.0 * params.tol_px / ex.scale
            for di in allowed:
                if _hausdorff(ex_norm @ DIHEDRAL[di][2].T, comp_norm) <= lim:
                    best = di
                    break
        if best is None:
            continue
        stats.signature_matches += 1
        A = ratio * DIHEDRAL[best][2]
        b = c - A @ ex.centroid
        score, cov, bx = score_pose(index, ex, A, b, params)
        if score >= params.threshold:
            add(score, cov, bx, best, ratio)
            consumed[ids] = True
    params.check_deadline()

    # ---- general path: anchor hypotheses ---------------------------------
    avail = np.nonzero(~consumed)[0]
    t = params.tol_px
    by_kind: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    for k in (0, 1):
        ids = avail[index.kind[avail] == k]
        o = np.argsort(index.chord[ids])
        by_kind[k] = (ids[o], index.chord[ids][o])

    def candidates(k: int, chord: float) -> np.ndarray:
        ids, ch = by_kind[k]
        lo = np.searchsorted(ch, chord - 2 * t, side="left")
        hi = np.searchsorted(ch, chord + 2 * t, side="right")
        return ids[lo:hi]

    ex_chord = np.linalg.norm(ex.ctrl[:, 3] - ex.ctrl[:, 0], axis=1)
    options = []
    for j in range(n_ex):
        if ex_chord[j] < 3 * t:
            continue
        cnt = candidates(int(ex.kind[j]), float(ex_chord[j])).size
        options.append((cnt, int(ex.kind[j]), round(float(ex_chord[j]) / t), j))
    options.sort()
    anchors: List[int] = []
    seen_buckets = set()
    for cnt, k, bucket, j in options:
        if (k, bucket) in seen_buckets:
            continue
        seen_buckets.add((k, bucket))
        anchors.append(j)
        if len(anchors) >= params.max_anchors:
            break

    hyp_d: List[np.ndarray] = []
    hyp_t: List[np.ndarray] = []
    for j in anchors:
        P = ex.ctrl[j]
        cand = candidates(int(ex.kind[j]), float(ex_chord[j]))
        if cand.size == 0:
            continue
        Q = index.ctrl[cand]
        for di in allowed:
            DP = P @ DIHEDRAL[di][2].T
            for Qo in (Q, Q[:, ::-1]):
                diff = Qo - DP[None]
                tr = diff.mean(axis=1)
                resid = np.abs(diff - tr[:, None]).max(axis=(1, 2))
                ok = resid <= 2 * t
                if ok.any():
                    hyp_t.append(tr[ok])
                    hyp_d.append(np.full(int(ok.sum()), di))
    params.check_deadline()
    if hyp_t:
        H_t = np.concatenate(hyp_t)
        H_d = np.concatenate(hyp_d)
        key = np.stack([H_d, np.round(H_t[:, 0] / t), np.round(H_t[:, 1] / t)], axis=1).astype(np.int64)
        _, first = np.unique(key, axis=0, return_index=True)
        H_t, H_d = H_t[first], H_d[first]
        stats.hypotheses = int(H_t.shape[0])
        if H_t.shape[0] > params.max_hypotheses:
            raise VectorMatchError(
                "timeout",
                f"The exemplar produced {H_t.shape[0]} pose hypotheses (limit "
                f"{params.max_hypotheses}); it is too generic (e.g. a single line). "
                "Choose a more distinctive symbol or raise max_hypotheses.",
            )
        # Coarse filter, one dihedral map at a time, in chunks.
        coarse = np.zeros(H_t.shape[0])
        m = ex.pts.shape[0]
        chunk = max(1, 4_000_000 // max(1, m))
        for di in np.unique(H_d):
            sel = np.nonzero(H_d == di)[0]
            base = ex.pts @ DIHEDRAL[di][2].T
            for s0 in range(0, sel.size, chunk):
                ss = sel[s0 : s0 + chunk]
                coarse[ss] = index.coarse_coverage(base[None] + H_t[ss, None, :])
        params.check_deadline()
        # Verify the most promising first; ties in D order (unmirrored, then
        # smallest rotation), the same preference as suppress(). Once a
        # location has a perfect match, other poses there can only lose in
        # suppress(), so they are skipped. Several anchors (and rounding) also
        # propose the same pose twice.
        ex_c = np.array([(ex.bbox[0] + ex.bbox[2]) / 2.0, (ex.bbox[1] + ex.bbox[3]) / 2.0])
        dup_r = 0.5 * max(1.0, min(ex.bbox[2] - ex.bbox[0], ex.bbox[3] - ex.bbox[1]))
        g = max(dup_r, 4.0 * t)
        perfect: Dict[Tuple[int, int], List[np.ndarray]] = {}
        same: Dict[Tuple[int, int, int], List[np.ndarray]] = {}
        cand = np.nonzero(coarse >= params.threshold)[0]
        cand = cand[np.lexsort((H_d[cand], -coarse[cand]))]

        def near(table, key_prefix, gx, gy, pt, r) -> bool:
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    for other in table.get(key_prefix + (gx + ox, gy + oy), ()):
                        if abs(other[0] - pt[0]) <= r and abs(other[1] - pt[1]) <= r:
                            return True
            return False

        for hi in cand:
            di = int(H_d[hi])
            tr = H_t[hi]
            A = DIHEDRAL[di][2]
            center = A @ ex_c + tr
            gx, gy = int(center[0] // g), int(center[1] // g)
            if near(perfect, (), gx, gy, center, dup_r) or near(same, (di,), gx, gy, center, 2 * t):
                continue
            score, cov, bx = score_pose(index, ex, A, tr, params)
            stats.hypotheses_verified += 1
            if score >= params.threshold:
                add(score, cov, bx, di)
                same.setdefault((di, gx, gy), []).append(center)
                if score >= 0.999:
                    perfect.setdefault((gx, gy), []).append(center)
            if stats.hypotheses_verified % 256 == 0:
                params.check_deadline()
    return dets, stats, ex


def suppress(dets: List[RawDetection], min_center_dist: float) -> List[RawDetection]:
    """Keep the best detection per location. Ties prefer unmirrored, then the
    smallest rotation, so a symmetric symbol gets a stable label."""
    ranked = sorted(dets, key=lambda d: (-round(d.score, 3), d.mirrored, d.rotation, d.box[1], d.box[0]))
    kept: List[RawDetection] = []
    centers = np.zeros((0, 2))
    for d in ranked:
        c = np.array(d.center)
        if centers.shape[0] and (np.hypot(*(centers - c).T) < min_center_dist).any():
            continue
        kept.append(d)
        centers = np.vstack([centers, c])
    return kept
