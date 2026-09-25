"""Decide whether a newly trained model may replace the one the QC app uses.

A model is promoted only on evidence: its held-out evaluation (from the
independent evaluator, ``pinny_eval score-corpus``) must be recorded in the
package, measured on the **same** corpus as the current model, and no
tracked metric may drop by more than ``max_drop``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

from .package import ModelPackage

DEFAULT_METRICS = ("ap", "f1", "recall")


@dataclass(frozen=True)
class PromotionDecision:
    ok: bool
    reasons: List[str] = field(default_factory=list)


def _evaluation(model: Union[ModelPackage, dict]) -> dict:
    ev = model.evaluation if isinstance(model, ModelPackage) else model.get("evaluation")
    return ev if isinstance(ev, dict) else {}


def _symbol_class(model: Union[ModelPackage, dict]) -> Optional[str]:
    return model.symbol_class if isinstance(model, ModelPackage) else model.get("symbol_class")


def _metric(ev: dict, name: str) -> Optional[float]:
    v = ev.get(name)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
        return None
    return float(v)


def promotion_check(
    candidate: Union[ModelPackage, dict],
    current: Optional[Union[ModelPackage, dict]] = None,
    metrics: Sequence[str] = DEFAULT_METRICS,
    max_drop: float = 0.01,
) -> PromotionDecision:
    """``candidate`` and ``current`` are packages or their manifests.
    ``current=None`` means nothing is deployed yet."""
    reasons: List[str] = []
    cand_ev = _evaluation(candidate)
    if not cand_ev.get("corpus_sha256"):
        reasons.append("The candidate has no held-out evaluation (evaluation.corpus_sha256 is missing).")
    for m in metrics:
        if _metric(cand_ev, m) is None:
            reasons.append(f"The candidate's evaluation has no valid '{m}' in [0, 1].")
    if current is not None:
        if _symbol_class(candidate) != _symbol_class(current):
            reasons.append(
                f"Symbol class differs ({_symbol_class(candidate)!r} vs {_symbol_class(current)!r}); "
                "a model only replaces one for the same symbol."
            )
        cur_ev = _evaluation(current)
        if cur_ev.get("corpus_sha256") and cur_ev.get("corpus_sha256") != cand_ev.get("corpus_sha256"):
            reasons.append(
                "The two models were evaluated on different corpora, so they can't be compared. "
                "Re-score the current model on the candidate's corpus."
            )
        elif not reasons:
            for m in metrics:
                new, old = _metric(cand_ev, m), _metric(cur_ev, m)
                if old is not None and new is not None and new < old - max_drop:
                    reasons.append(f"{m} dropped from {old:.4f} to {new:.4f} (allowed drop {max_drop}).")
    return PromotionDecision(ok=not reasons, reasons=reasons)
