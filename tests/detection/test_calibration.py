"""Calibration math on fixed data; background normalization on a synthetic page."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from pinny.detection import DetectionError, OpenCVTemplateDetector, ScanSettings
from pinny.detection.calibration import (
    BackgroundStats,
    IsotonicCalibrator,
    PlattCalibrator,
    background_from_candidates,
    background_from_score_map,
    background_stats,
    normalize_scores,
    pav,
    suggest_threshold,
)


def test_background_stats_and_normalize():
    st = background_stats([0.1, 0.2, 0.3, 0.4, 10.0, np.nan])
    assert st.median == pytest.approx(0.3) and st.n == 5
    assert st.mad == pytest.approx(0.1)
    assert st.scale == pytest.approx(0.14826)
    assert normalize_scores([0.3, 0.3 + 0.14826], st) == pytest.approx([0.0, 1.0])
    assert BackgroundStats(0.5, 0.0, 3).scale == pytest.approx(1e-3)
    with pytest.raises(DetectionError):
        background_stats([np.nan])


def test_background_from_score_map_excludes_peaks_and_is_deterministic():
    rng = np.random.default_rng(0)
    m = rng.normal(0.2, 0.05, (200, 200)).astype(np.float32)
    m[50, 50] = m[100, 120] = 1.0
    m[0, 0] = np.nan
    a = background_from_score_map(m, n_samples=2000, exclude_above=0.8)
    b = background_from_score_map(m, n_samples=2000, exclude_above=0.8)
    assert a == b and a.n == 2000
    assert a.median == pytest.approx(0.2, abs=0.01)
    assert a.scale == pytest.approx(0.05, rel=0.15)


def test_background_from_candidates_on_cluttered_page():
    rng = np.random.default_rng(3)
    page = np.full((300, 400), 255, np.uint8)
    for _ in range(80):  # line-work clutter
        p1 = tuple(int(v) for v in rng.integers(0, [400, 300]))
        p2 = tuple(int(v) for v in rng.integers(0, [400, 300]))
        cv2.line(page, p1, p2, 0, 2)
    glyph = np.full((36, 24), 255, np.uint8)
    cv2.circle(glyph, (12, 20), 9, 0, 2)
    cv2.line(glyph, (8, 16), (8, 24), 0, 2)
    cv2.line(glyph, (16, 16), (16, 24), 0, 2)
    page[10:46, 10:34] = glyph
    det = OpenCVTemplateDetector()
    settings = ScanSettings(rotations=(0,), threshold=0.8)
    stats = background_from_candidates(det, page, glyph, settings, low_threshold=0.2)
    assert stats is not None and stats.n >= 20
    assert stats.median < 0.8
    z_symbol = normalize_scores(1.0, stats)
    assert z_symbol > 3.0
    # A clean page has too few background peaks.
    clean = np.full((200, 200), 255, np.uint8)
    clean[10:46, 10:34] = glyph
    assert background_from_candidates(det, clean, glyph, settings) is None


def test_pav_known_answer():
    assert pav([1, 3, 2, 4]) == pytest.approx([1, 2.5, 2.5, 4])
    assert pav([3, 2, 1]) == pytest.approx([2, 2, 2])
    assert pav([0, 1], w=[1, 1]) == pytest.approx([0, 1])
    assert pav([1, 0], w=[3, 1]) == pytest.approx([0.75, 0.75])


def test_isotonic_calibrator_is_monotone_and_pools_ties():
    scores = [0.5, 0.6, 0.6, 0.7, 0.8, 0.9]
    labels = [0, 1, 0, 0, 1, 1]
    cal = IsotonicCalibrator().fit(scores, labels)
    # 0.6 pooled to 0.5, then {0.6: 0.5, 0.7: 0} violates -> pooled to 1/3.
    assert list(cal.x_) == [0.5, 0.6, 0.7, 0.8, 0.9]
    assert cal.y_ == pytest.approx([0, 1 / 3, 1 / 3, 1, 1])
    p = cal.predict([0.0, 0.55, 0.75, 1.0])
    assert p == pytest.approx([0, 1 / 6, 2 / 3, 1])
    assert np.all(np.diff(cal.predict(np.linspace(0, 1, 50))) >= 0)
    with pytest.raises(DetectionError):
        IsotonicCalibrator().predict([0.5])


def test_platt_calibrator_fits_separable_trend():
    rng = np.random.default_rng(0)
    s = rng.uniform(0.5, 1.0, 400)
    y = rng.uniform(size=400) < 1 / (1 + np.exp(-(s - 0.75) * 20))
    cal = PlattCalibrator().fit(s, y)
    assert cal.a < 0  # P increases with score
    assert cal.predict(0.75) == pytest.approx(0.5, abs=0.08)
    assert cal.predict(0.95) > 0.9 and cal.predict(0.55) < 0.1
    with pytest.raises(DetectionError):
        PlattCalibrator().fit([0.9, 0.8], [1, 1])


def _fixed_pairs():
    # 40 labelled reviews: all >= 0.90 approved; 0.85-0.89 mostly approved; below mostly rejected.
    pos = [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.91, 0.90] * 2
    mid = [(0.89, 1), (0.88, 1), (0.87, 0), (0.86, 1), (0.85, 1)]
    low = [(0.84, 0), (0.83, 0), (0.82, 1), (0.81, 0), (0.80, 0)] * 3
    return [(s, True) for s in pos] + mid + low


def test_suggest_threshold_known_answer():
    pairs = _fixed_pairs()
    assert len(pairs) == 40
    sug = suggest_threshold(pairs, target_precision=0.95, min_labels=30)
    # At 0.85: 25 kept, 24 approved -> 0.96. At 0.84: 28 kept, 24 -> 0.857.
    assert sug.reason == "ok" and sug.value == pytest.approx(0.85)
    assert sug.precision == pytest.approx(24 / 25)
    assert sug.recall_proxy == pytest.approx(24 / 27)
    assert (sug.n, sug.n_positive, sug.n_negative) == (40, 27, 13)
    strict = suggest_threshold(pairs, target_precision=1.0)
    assert strict.value == pytest.approx(0.88) and strict.precision == 1.0


def test_suggest_threshold_includes_all_ties():
    pairs = [(0.9, True)] * 30 + [(0.8, True), (0.8, False)]
    sug = suggest_threshold(pairs, target_precision=0.95)
    # 0.8 would give 31/32 = 0.969 including both tied candidates.
    assert sug.value == pytest.approx(0.8) and sug.precision == pytest.approx(31 / 32)


def test_suggest_threshold_refusals():
    few = suggest_threshold([(0.9, True)] * 10, min_labels=30)
    assert few.value is None and few.reason == "insufficient_labels" and few.n == 10
    none_pos = suggest_threshold([(0.9, False)] * 40)
    assert none_pos.value is None and none_pos.reason == "no_positives"
    bad = suggest_threshold([(0.9, False), (0.8, True)] * 20, target_precision=0.95)
    assert bad.value is None and bad.reason == "target_unreachable"
    assert bad.precision == pytest.approx(0.5)
    with pytest.raises(DetectionError):
        suggest_threshold([], target_precision=0)
