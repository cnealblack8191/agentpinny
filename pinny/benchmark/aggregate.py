"""Summed, not averaged, aggregation of per-page counts (P9).

TP/FP/FN are summed over pages and divided once. Ratios are kept as exact
fractions so the promotion gate's thresholds are applied without float
rounding.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, Iterable, Mapping, Optional


def aggregate_counts(pages: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    tp = fp = fn = 0
    n = 0
    for page in pages:
        tp += int(page["true_positives"])
        fp += int(page["false_positives"])
        fn += int(page["false_negatives"])
        n += 1
    return {"pages": n, "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "predictions": tp + fp, "references": tp + fn}


def exact_ratio(num: int, den: int) -> Optional[Fraction]:
    return None if den == 0 else Fraction(num, den)


def ratio(num: int, den: int, undefined_reason: str) -> Dict[str, Any]:
    """Same shape and zero-denominator rule as the evaluator's metrics."""
    if den == 0:
        return {"value": None, "numerator": num, "denominator": den,
                "undefined_reason": undefined_reason}
    return {"value": num / den, "numerator": num, "denominator": den, "undefined_reason": None}


def metrics(counts: Mapping[str, int]) -> Dict[str, Any]:
    tp, fp, fn = counts["true_positives"], counts["false_positives"], counts["false_negatives"]
    return {
        "precision": ratio(tp, tp + fp, "no predictions (TP + FP = 0)"),
        "recall": ratio(tp, tp + fn, "no reference receptacles (TP + FN = 0)"),
    }
