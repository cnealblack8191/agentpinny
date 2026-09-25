"""Score-based precision/recall curves.

For every distinct confidence value ``t`` (highest first), the detections with
``confidence >= t`` are matched against **all** reference receptacles with the
same maximum-cardinality one-to-one rule as the main report, and one PR point
is recorded. Nothing is sampled: every distinct threshold gets a point.

Efficiency. Re-running the Hungarian matcher at every threshold would be
O(thresholds x matching). Only the *number* of matches matters for P/R, so the
curve instead grows one maximum matching incrementally (Kuhn's augmenting-path
algorithm): detections are added in descending confidence order, and each new
detection gets one augmenting-path search limited to its own eligibility
component. After each batch of equal-confidence detections the matching is a
maximum matching of exactly the detections added so far (the standard
prefix property of Kuhn's algorithm), so each point's TP equals what a full
re-match at that threshold would give. Tests cross-check this against
re-running ``matching.match`` at every threshold. Cost is roughly
O(detections x component size): well under a second for ~5k detections on a
page-like layout.

AP is the **all-point interpolated** average precision of PASCAL VOC
2010+: precision is replaced by its running maximum from the right
(p_interp(r) = max precision at any recall >= r), and AP is the area under that
step function over recall, starting from recall 0. It is *not* COCO's
101-point sampled AP and it is not averaged over several IoU/distance
thresholds.

Confidences are the detector's raw scores (contracts.md: ``score`` is not a
probability); thresholds are reported in the same units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

TARGET = 0.95


@dataclass
class CurveInput:
    """One page: detection IDs with confidences, the reference count, and eligible pairs."""

    confidences: Mapping[str, Optional[float]]
    n_references: int
    edges: Mapping[Tuple[str, str], float]  # (prediction_id, reference_id) -> distance


def _augment(start: Any, adj: Dict[Any, List[Any]], match_ref: Dict[Any, Any]) -> bool:
    """One iterative DFS for an augmenting path from ``start`` (Kuhn)."""
    visited = set()
    stack = [(start, iter(adj.get(start, ())))]
    path: List[Any] = []  # path[i] = reference chosen by stack[i]
    while stack:
        u, it = stack[-1]
        advanced = False
        for r in it:
            if r in visited:
                continue
            visited.add(r)
            path.append(r)
            owner = match_ref.get(r)
            if owner is None:
                for (pu, _), pr in zip(stack, path):
                    match_ref[pr] = pu
                return True
            stack.append((owner, iter(adj.get(owner, ()))))
            advanced = True
            break
        if not advanced:
            stack.pop()
            if path:
                path.pop()
    return False


def _unavailable(note: str, **extra: Any) -> Dict[str, Any]:
    return {"available": False, "note": note, "points": [], "ap": None, "best_f1": None,
            "precision_at_recall_0_95": None, "recall_at_precision_0_95": None, **extra}


def pr_curve(pages: List[CurveInput]) -> Dict[str, Any]:
    """PR curve pooled over ``pages`` (one page is just a list of one)."""
    total = sum(len(p.confidences) for p in pages)
    missing = sum(1 for p in pages for c in p.confidences.values() if c is None)
    n_refs = sum(p.n_references for p in pages)
    if missing:
        return _unavailable(
            f"curve omitted: {missing} of {total} detections have no confidence/score, "
            "so detections cannot be ranked", detections_without_confidence=missing)
    if n_refs == 0:
        return _unavailable("curve omitted: no reference receptacles, so recall is undefined")

    adj: Dict[Any, List[Any]] = {}
    order: List[Tuple[float, int, str]] = []
    for pi, page in enumerate(pages):
        for (pid, rid), d in sorted(page.edges.items(), key=lambda kv: (kv[1], kv[0])):
            adj.setdefault((pi, pid), []).append((pi, rid))
        for pid, conf in page.confidences.items():
            order.append((conf, pi, pid))
    order.sort(key=lambda t: (-t[0], t[1], t[2]))

    match_ref: Dict[Any, Any] = {}
    points: List[Dict[str, Any]] = []
    tp = 0
    i = 0
    while i < len(order):
        t = order[i][0]
        while i < len(order) and order[i][0] == t:
            key = (order[i][1], order[i][2])
            if key in adj and _augment(key, adj, match_ref):
                tp += 1
            i += 1
        fp = i - tp
        fn = n_refs - tp
        prec = tp / i
        rec = tp / n_refs
        f1 = 2 * tp / (2 * tp + fp + fn)
        points.append({"threshold": t, "detections": i, "tp": tp, "fp": fp, "fn": fn,
                       "precision": prec, "recall": rec, "f1": f1})

    # All-point interpolated AP (VOC 2010+).
    ap = 0.0
    interp = 0.0
    envelope = [0.0] * len(points)
    for k in range(len(points) - 1, -1, -1):
        interp = max(interp, points[k]["precision"])
        envelope[k] = interp
    prev_r = 0.0
    for k, pt in enumerate(points):
        ap += (pt["recall"] - prev_r) * envelope[k]
        prev_r = pt["recall"]

    def best(key: str, cond) -> Optional[Dict[str, Any]]:
        # Ties go to the higher threshold (fewer detections), i.e. the first seen.
        chosen = None
        for pt in points:
            if cond(pt) and (chosen is None or pt[key] > chosen[key]):
                chosen = pt
        return None if chosen is None else dict(chosen)

    return {
        "available": True,
        "note": None,
        "method": "incremental maximum-cardinality matching at every distinct confidence",
        "ap_method": "all-point interpolated (PASCAL VOC 2010+), recall from 0; not COCO 101-point",
        "references": n_refs,
        "detections": total,
        "thresholds": len(points),
        "ap": ap,
        "best_f1": best("f1", lambda p: True),
        "precision_at_recall_0_95": best("precision", lambda p: p["recall"] >= TARGET),
        "recall_at_precision_0_95": best("recall", lambda p: p["precision"] >= TARGET),
        "points": points,
    }


def sample_points(points: List[Dict[str, Any]], n: int = 10) -> List[Dict[str, Any]]:
    """Up to ``n`` evenly spaced points (always including the last) for Markdown tables."""
    if len(points) <= n:
        return list(points)
    step = (len(points) - 1) / (n - 1)
    idx = sorted({round(k * step) for k in range(n)})
    return [points[k] for k in idx]
