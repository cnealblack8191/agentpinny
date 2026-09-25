"""Score calibration and threshold suggestion. Raw ``Candidate.score`` is
never modified; everything here is a derived view.

Unsupervised (no labels)
    :func:`background_stats` estimates, for one template on one page, the
    typical score of *non-symbol* positions as a robust location/scale
    ``(median, MAD)``. :func:`normalize_scores` turns raw scores into
    ``z = (s - median_bg) / (1.4826 * MAD_bg)`` so scores of different
    templates or pages become comparable ("how many noise-sigmas above the
    clutter").

    The detector does not expose its score map, so two sources are
    supported: :func:`background_from_score_map` (any NCC map, e.g. from
    ``cv2.matchTemplate``, sampled with peaks excluded) and
    :func:`background_from_candidates`, which reruns the detector at a low
    threshold and treats candidate peaks below the operating threshold as
    background. The latter samples *local maxima* of clutter, so its median
    is higher than a whole-map median; it is still a consistent per
    template/page reference, which is what normalization needs.

Supervised (review labels)
    :class:`IsotonicCalibrator` (pool-adjacent-violators, pure numpy) and
    :class:`PlattCalibrator` (logistic fit by Newton's method) map raw score
    to P(approved) from ``(score, label)`` pairs. :func:`suggest_threshold`
    picks the lowest raw threshold meeting a target precision. It is a
    suggestion only: callers must show it to the user, never apply it
    silently.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple, Union

import numpy as np

from .interface import Detector
from .types import DetectionError, ScanSettings, Template

__all__ = [
    "BackgroundStats",
    "IsotonicCalibrator",
    "PlattCalibrator",
    "ThresholdSuggestion",
    "background_from_candidates",
    "background_from_score_map",
    "background_stats",
    "normalize_scores",
    "pav",
    "suggest_threshold",
]

_MAD_TO_SIGMA = 1.4826
_MIN_SCALE = 1e-3


@dataclass(frozen=True)
class BackgroundStats:
    median: float
    #: Raw median absolute deviation (unscaled).
    mad: float
    n: int

    @property
    def scale(self) -> float:
        """Robust sigma: ``1.4826 * MAD``, floored at 1e-3."""
        return max(_MAD_TO_SIGMA * self.mad, _MIN_SCALE)


def background_stats(scores: Sequence[float]) -> BackgroundStats:
    s = np.asarray(scores, dtype=np.float64).ravel()
    s = s[np.isfinite(s)]
    if s.size == 0:
        raise DetectionError("no_background", "No finite background scores to estimate from.")
    med = float(np.median(s))
    return BackgroundStats(median=med, mad=float(np.median(np.abs(s - med))), n=int(s.size))


def normalize_scores(scores: Union[float, Sequence[float], np.ndarray], stats: BackgroundStats) -> np.ndarray:
    """``(s - median) / scale`` as a float64 array."""
    return (np.asarray(scores, dtype=np.float64) - stats.median) / stats.scale


def background_from_score_map(
    score_map: np.ndarray,
    n_samples: int = 4096,
    exclude_above: Optional[float] = None,
    exclude_top_quantile: float = 0.01,
    seed: int = 0,
) -> BackgroundStats:
    """Sample a score map, excluding peaks: values above ``exclude_above``
    (e.g. the operating threshold) or in the top ``exclude_top_quantile``,
    and non-finite values. Deterministic for a given ``seed``."""
    v = np.asarray(score_map, dtype=np.float64).ravel()
    v = v[np.isfinite(v) & (v >= -1.0)]
    if v.size == 0:
        raise DetectionError("no_background", "Score map has no finite values.")
    cut = np.quantile(v, 1.0 - exclude_top_quantile) if exclude_top_quantile > 0 else np.inf
    if exclude_above is not None:
        cut = min(cut, exclude_above)
    bg = v[v < cut] if np.any(v < cut) else v
    if bg.size > n_samples:
        bg = np.random.default_rng(seed).choice(bg, size=n_samples, replace=False)
    return background_stats(bg)


def background_from_candidates(
    detector: Detector,
    page: np.ndarray,
    template: Union[Template, np.ndarray],
    settings: ScanSettings = ScanSettings(),
    low_threshold: float = 0.3,
    min_samples: int = 20,
) -> Optional[BackgroundStats]:
    """Rerun ``detector`` at ``low_threshold`` and use candidate scores below
    ``settings.threshold`` as background. Returns ``None`` when fewer than
    ``min_samples`` such peaks exist (a very clean page); callers should then
    fall back to raw scores."""
    low = dataclasses.replace(
        settings, threshold=float(low_threshold), max_candidates=max(settings.max_candidates, 2000)
    )
    res = detector.detect(page, template, low)
    bg = [c.score for c in res.candidates if c.score < settings.threshold]
    if len(bg) < min_samples:
        return None
    return background_stats(bg)


# --------------------------------------------------------------------------
# Supervised calibration


def _pairs_arrays(pairs: Iterable[Tuple[float, object]]) -> Tuple[np.ndarray, np.ndarray]:
    ps = list(pairs)
    if not ps:
        return np.zeros(0), np.zeros(0)
    s = np.asarray([float(p[0]) for p in ps], dtype=np.float64)
    y = np.asarray([1.0 if bool(p[1]) else 0.0 for p in ps], dtype=np.float64)
    ok = np.isfinite(s)
    return s[ok], y[ok]


def pav(y: Sequence[float], w: Optional[Sequence[float]] = None) -> np.ndarray:
    """Pool-adjacent-violators: the non-decreasing sequence closest to ``y``
    in weighted least squares."""
    y = np.asarray(y, dtype=np.float64)
    w = np.ones_like(y) if w is None else np.asarray(w, dtype=np.float64)
    vals, wts, lens = [], [], []
    for yi, wi in zip(y, w):
        vals.append(yi)
        wts.append(wi)
        lens.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            wsum = wts[-2] + wts[-1]
            v = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / wsum
            n = lens[-2] + lens[-1]
            vals[-2:], wts[-2:], lens[-2:] = [v], [wsum], [n]
    return np.repeat(vals, lens)


class IsotonicCalibrator:
    """Monotone map raw score -> P(positive), fitted by PAV.

    Tied scores are pooled first. Prediction interpolates linearly between
    fitted points and is constant beyond the observed range.
    """

    def __init__(self) -> None:
        self.x_: Optional[np.ndarray] = None
        self.y_: Optional[np.ndarray] = None

    def fit(self, scores: Sequence[float], labels: Sequence[object]) -> "IsotonicCalibrator":
        s, y = _pairs_arrays(zip(scores, labels))
        if s.size == 0:
            raise DetectionError("insufficient_labels", "Isotonic calibration needs labelled scores.")
        order = np.argsort(s, kind="stable")
        s, y = s[order], y[order]
        ux, inv = np.unique(s, return_inverse=True)
        w = np.bincount(inv).astype(np.float64)
        ym = np.bincount(inv, weights=y) / w
        self.x_, self.y_ = ux, pav(ym, w)
        return self

    def predict(self, scores: Union[float, Sequence[float]]) -> np.ndarray:
        if self.x_ is None:
            raise DetectionError("calibrator_not_fitted", "Call fit() before predict().")
        return np.interp(np.asarray(scores, dtype=np.float64), self.x_, self.y_)


class PlattCalibrator:
    """``P = 1 / (1 + exp(a*s + b))`` fitted with Platt's smoothed targets."""

    def __init__(self, max_iter: int = 100) -> None:
        self.max_iter = max_iter
        self.a: Optional[float] = None
        self.b: Optional[float] = None

    def fit(self, scores: Sequence[float], labels: Sequence[object]) -> "PlattCalibrator":
        s, y = _pairs_arrays(zip(scores, labels))
        n_pos, n_neg = y.sum(), (1 - y).sum()
        if n_pos == 0 or n_neg == 0:
            raise DetectionError(
                "insufficient_labels", "Platt scaling needs both positive and negative labels."
            )
        t = np.where(y > 0, (n_pos + 1) / (n_pos + 2), 1 / (n_neg + 2))
        a, b = 0.0, float(np.log((n_neg + 1) / (n_pos + 1)))
        for _ in range(self.max_iter):
            f = a * s + b
            p = 1.0 / (1.0 + np.exp(f))
            # Gradient/Hessian of cross-entropy wrt (a, b); dP/df = -p(1-p).
            g = np.array([((t - p) * s).sum(), (t - p).sum()])
            d = p * (1 - p)
            h = np.array([[(d * s * s).sum(), (d * s).sum()], [(d * s).sum(), d.sum()]]) + 1e-12 * np.eye(2)
            step = np.linalg.solve(h, g)
            a, b = a - step[0], b - step[1]
            if np.abs(step).max() < 1e-10:
                break
        self.a, self.b = float(a), float(b)
        return self

    def predict(self, scores: Union[float, Sequence[float]]) -> np.ndarray:
        if self.a is None:
            raise DetectionError("calibrator_not_fitted", "Call fit() before predict().")
        f = self.a * np.asarray(scores, dtype=np.float64) + self.b
        return 1.0 / (1.0 + np.exp(np.clip(f, -500, 500)))


# --------------------------------------------------------------------------
# Threshold suggestion


@dataclass(frozen=True)
class ThresholdSuggestion:
    #: Suggested raw-score threshold (keep candidates with score >= value),
    #: or ``None`` when no suggestion can be made (see ``reason``).
    value: Optional[float]
    #: Precision among labelled candidates at or above ``value`` (for
    #: ``target_unreachable``: the best precision found).
    precision: Optional[float]
    #: Fraction of labelled positives at or above ``value``. A proxy: symbols
    #: the detector never proposed are not in the pairs.
    recall_proxy: Optional[float]
    #: Number of labelled pairs used.
    n: int
    #: ``ok`` | ``insufficient_labels`` | ``no_positives`` | ``target_unreachable``.
    reason: str
    n_positive: int = 0
    n_negative: int = 0


def suggest_threshold(
    pairs: Iterable[Tuple[float, object]],
    target_precision: float = 0.95,
    min_labels: int = 30,
) -> ThresholdSuggestion:
    """Lowest raw threshold whose labelled precision is >= ``target_precision``.

    ``pairs`` are ``(raw score, label)`` for *reviewed machine detections*
    (label truthy = approved, falsy = rejected). Choosing the lowest
    qualifying threshold maximizes the recall proxy. Candidate thresholds
    are the observed scores, so ``value`` is always a score that was seen.
    """
    if not (0.0 < target_precision <= 1.0):
        raise DetectionError("invalid_settings", "target_precision must be in (0, 1].")
    s, y = _pairs_arrays(pairs)
    n = int(s.size)
    n_pos, n_neg = int(y.sum()), int(n - y.sum())
    if n < min_labels:
        return ThresholdSuggestion(None, None, None, n, "insufficient_labels", n_pos, n_neg)
    if n_pos == 0:
        return ThresholdSuggestion(None, 0.0, None, n, "no_positives", n_pos, n_neg)
    order = np.argsort(-s, kind="stable")
    s, y = s[order], y[order]
    # Evaluate only at the last index of each run of equal scores so a
    # threshold keeps every tied candidate.
    last = np.r_[s[1:] != s[:-1], True]
    tp = np.cumsum(y)[last]
    kept = (np.arange(n) + 1)[last]
    thresholds = s[last]
    precision = tp / kept
    recall = tp / n_pos
    ok = np.flatnonzero(precision >= target_precision - 1e-12)
    if ok.size == 0:
        best = int(np.argmax(precision))
        return ThresholdSuggestion(
            None, float(precision[best]), float(recall[best]), n, "target_unreachable", n_pos, n_neg
        )
    i = int(ok[-1])  # lowest threshold that still meets the target
    return ThresholdSuggestion(
        float(thresholds[i]), float(precision[i]), float(recall[i]), n, "ok", n_pos, n_neg
    )
