"""Small statistics helpers shared by page and corpus reports."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


def ratio(num: int, den: int, undefined_reason: str) -> Dict[str, Any]:
    """Zero-denominator rule: value is null (never 0 or 1) and a reason is given."""
    if den == 0:
        return {"value": None, "numerator": num, "denominator": den, "undefined_reason": undefined_reason}
    return {"value": num / den, "numerator": num, "denominator": den, "undefined_reason": None}


def prf(tp: int, fp: int, fn: int) -> Dict[str, Dict[str, Any]]:
    """Precision, recall and F1 from counts. F1 = 2TP / (2TP + FP + FN)."""
    return {
        "precision": ratio(tp, tp + fp, "no predictions (TP + FP = 0)"),
        "recall": ratio(tp, tp + fn, "no reference receptacles (TP + FN = 0)"),
        "f1": ratio(2 * tp, 2 * tp + fp + fn, "no predictions and no references (2TP + FP + FN = 0)"),
    }


def f1_value(tp: int, fp: int, fn: int) -> Optional[float]:
    den = 2 * tp + fp + fn
    return None if den == 0 else 2 * tp / den


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy's default method); q in [0, 100]."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def localization(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Offset statistics over matched pairs. dx, dy = prediction - reference (y down)."""
    ds = [r["distance_px"] for r in rows]
    if not ds:
        return {"matched_pairs": 0, "note": "no matched pairs"}
    n = len(ds)
    return {
        "matched_pairs": n,
        "mean_distance_px": sum(ds) / n,
        "median_distance_px": percentile(ds, 50),
        "p95_distance_px": percentile(ds, 95),
        "max_distance_px": max(ds),
        "mean_dx_px": sum(r["dx_px"] for r in rows) / n,
        "mean_dy_px": sum(r["dy_px"] for r in rows) / n,
        "percentile_method": "linear interpolation",
    }


def runtime_summary(values: List[float], missing: int) -> Dict[str, Any]:
    if not values:
        return {"pages_with_runtime": 0, "pages_without_runtime": missing, "note": "no runtime recorded"}
    return {
        "pages_with_runtime": len(values),
        "pages_without_runtime": missing,
        "p50_seconds": percentile(values, 50),
        "p95_seconds": percentile(values, 95),
        "mean_seconds": sum(values) / len(values),
        "total_seconds": sum(values),
    }
