"""Tests for the search strategy of the OpenCV detector: coarse-to-fine
equivalence, strip tiling, threading and the runtime deadline.

Synthetic fixtures: they prove implementation behaviour, not real-world
accuracy.
"""

from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

import pinny.detection.opencv_matcher as matcher
from pinny.detection import DetectionTimeout, OpenCVTemplateDetector, ScanSettings

GLYPH_W, GLYPH_H = 24, 36


def render_glyph(dx: float = 0.0, dy: float = 0.0, scale: float = 1.0, size: float = 1.0) -> np.ndarray:
    """Asymmetric receptacle-like glyph drawn at 8x and area-downsampled.
    ``size`` sets the nominal symbol size; ``scale`` perturbs it."""
    s, k = 8, scale * size
    w, h = int(round(GLYPH_W * k)) + 4, int(round(GLYPH_H * k)) + 4
    g = np.full((h * s, w * s), 255, np.uint8)

    def p(x, y):
        return (int(round((x * k + 2 + dx) * s)), int(round((y * k + 2 + dy) * s)))

    lw = max(1, int(round(2 * k * s)))
    cv2.circle(g, p(12, 20), int(round(9 * k * s)), 0, lw)
    cv2.line(g, p(8, 16), p(8, 24), 0, lw)
    cv2.line(g, p(16, 16), p(16, 24), 0, lw)
    cv2.line(g, p(12, 0), p(12, 10), 0, lw)
    cv2.rectangle(g, p(0, 31), p(5, 35), 0, -1)
    return cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA)


def orient(img: np.ndarray, rotation: int, mirrored: bool = False) -> np.ndarray:
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if mirrored:
        img = cv2.flip(img, 1)
    return img if rotation == 0 else cv2.rotate(img, codes[rotation])


def place(page: np.ndarray, img: np.ndarray, x: int, y: int) -> None:
    h, w = img.shape[:2]
    page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], img)


def synthetic_page(seed: int, size: float = 1.0, mirrored: bool = False):
    """Clutter plus a grid of symbols at random quarter turns, sub-pixel
    offsets and scales in [0.95, 1.05] (some touching clutter lines)."""
    rng = np.random.default_rng(seed)
    w, h = int(900 * size), int(700 * size)
    page = np.full((h, w), 255, np.uint8)
    for _ in range(20):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        length, width = int(rng.integers(40, 500)), int(rng.integers(1, 3))
        end = (x0 + length, y0) if rng.random() < 0.5 else (x0, y0 + length)
        cv2.line(page, (x0, y0), end, 0, width)
    for _ in range(10):
        cv2.putText(page, "RM %d" % rng.integers(100, 999),
                    (int(rng.integers(0, w - 100)), int(rng.integers(20, h))),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, 0, 2)
    cell = int(80 * size)
    for gx, gy in itertools.product(range(1, w // cell - 1), range(1, h // cell - 1)):
        if rng.random() < 0.3:
            continue
        img = render_glyph(float(rng.uniform(0, 1)), float(rng.uniform(0, 1)),
                           float(rng.choice([0.95, 1.0, 1.05])), size)
        img = orient(img, int(rng.choice([0, 90, 180, 270])), mirrored and rng.random() < 0.4)
        place(page, img, gx * cell + int(rng.integers(0, 10)), gy * cell + int(rng.integers(0, 10)))
    return page


def assert_equivalent(a, b, box_tol=1, score_tol=0.01):
    """Same detections: one-to-one, same orientation, boxes within
    ``box_tol`` px and scores within ``score_tol``."""
    assert len(a.candidates) == len(b.candidates)
    unmatched = list(b.candidates)
    for c in a.candidates:
        match = next(
            (
                d for d in unmatched
                if (d.rotation, d.mirrored, d.box.width, d.box.height)
                == (c.rotation, c.mirrored, c.box.width, c.box.height)
                and abs(d.box.x - c.box.x) <= box_tol and abs(d.box.y - c.box.y) <= box_tol
                and abs(d.score - c.score) <= score_tol
            ),
            None,
        )
        assert match is not None, f"no counterpart for {c}"
        unmatched.remove(match)


@pytest.fixture
def detector() -> OpenCVTemplateDetector:
    return OpenCVTemplateDetector()


@pytest.fixture
def coarse_calls(monkeypatch):
    """Records whether each coarse-to-fine attempt was used (True) or fell
    back to the full-resolution search (False)."""
    calls = []
    original = matcher._Search._coarse_to_fine

    def spy(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        calls.append(out is not None)
        return out

    monkeypatch.setattr(matcher._Search, "_coarse_to_fine", spy)
    return calls


# --- coarse-to-fine ---------------------------------------------------------


@pytest.mark.parametrize(
    "seed,size,threshold,mirrored",
    [
        (0, 1.0, 0.8, False),
        (1, 1.0, 0.8, False),
        (2, 1.0, 0.6, False),
        (3, 1.5, 0.8, False),
        (4, 1.5, 0.7, True),
        (5, 2.0, 0.8, True),
    ],
)
def test_coarse_to_fine_matches_full_resolution(detector, coarse_calls, seed, size, threshold, mirrored):
    page = synthetic_page(seed, size, mirrored)
    template = render_glyph(size=size)
    common = dict(threshold=threshold, include_mirrored=mirrored)
    full = detector.detect(page, template, ScanSettings(coarse_to_fine=False, **common))
    coarse = detector.detect(page, template, ScanSettings(coarse_to_fine=True, **common))
    assert coarse_calls and all(coarse_calls), "coarse-to-fine path was not exercised"
    assert len(full.candidates) >= 15  # the page really has symbols to find
    assert_equivalent(coarse, full)


def test_coarse_to_fine_with_search_region_and_rgb(detector, coarse_calls):
    from pinny.detection import BoundingBox

    page = cv2.cvtColor(synthetic_page(7), cv2.COLOR_GRAY2RGB)
    template = cv2.cvtColor(render_glyph(), cv2.COLOR_GRAY2RGB)
    region = BoundingBox(103, 57, 611, 509)
    full = detector.detect(page, template, ScanSettings(coarse_to_fine=False, search_region=region))
    coarse = detector.detect(page, template, ScanSettings(search_region=region))
    assert all(coarse_calls) and len(full.candidates) >= 10
    assert_equivalent(coarse, full)


def test_coarse_peak_cap_falls_back_to_full_resolution(detector, coarse_calls):
    page = synthetic_page(8)
    template = render_glyph()
    full = detector.detect(page, template, ScanSettings(coarse_to_fine=False))
    capped = detector.detect(page, template, ScanSettings(max_coarse_peaks=1))
    assert coarse_calls and not any(coarse_calls)
    assert [(c.box, c.rotation, c.score) for c in capped.candidates] == [
        (c.box, c.rotation, c.score) for c in full.candidates
    ]


def test_small_templates_skip_coarse_pass(detector, coarse_calls):
    small = cv2.resize(render_glyph(), (18, 27), interpolation=cv2.INTER_AREA)
    page = np.full((300, 300), 255, np.uint8)
    place(page, small, 100, 100)
    result = detector.detect(page, small)
    assert coarse_calls == []
    assert [(c.box.x, c.box.y) for c in result.candidates] == [(100, 100)]


# --- strip tiling and threads --------------------------------------------


@pytest.mark.parametrize("coarse_to_fine", [False, True])
@pytest.mark.parametrize("num_threads", [1, 3])
@pytest.mark.parametrize("strip_pixels", [900, 20_000])
def test_strips_identical_to_untiled(detector, monkeypatch, coarse_to_fine, num_threads, strip_pixels):
    """Strips as small as one score-map row: peaks straddling strip borders
    must still see their whole neighbourhood."""
    page = synthetic_page(9)
    template = render_glyph()
    settings = dict(coarse_to_fine=coarse_to_fine, threshold=0.6)
    monkeypatch.setattr(matcher, "_STRIP_RESULT_PIXELS", 10**12)
    untiled = detector.detect(page, template, ScanSettings(num_threads=1, **settings))
    monkeypatch.setattr(matcher, "_STRIP_RESULT_PIXELS", strip_pixels)
    tiled = detector.detect(page, template, ScanSettings(num_threads=num_threads, **settings))
    assert len(untiled.candidates) >= 20
    # Scores may differ by float rounding (~1e-6), which can reorder
    # near-tied candidates, so compare in position order.
    by_position = lambda r: sorted(r.candidates, key=lambda c: (c.box.y, c.box.x, c.rotation))
    assert [(c.box, c.rotation) for c in by_position(tiled)] == [
        (c.box, c.rotation) for c in by_position(untiled)
    ]
    for a, b in zip(by_position(tiled), by_position(untiled)):
        assert a.score == pytest.approx(b.score, abs=1e-5)
    assert tiled.warnings == untiled.warnings


def test_thread_count_does_not_change_results(detector):
    page = synthetic_page(10)
    template = render_glyph()
    results = [
        detector.detect(page, template, ScanSettings(coarse_to_fine=False, num_threads=n)).to_dict()
        for n in (0, 1, 2, 4)
    ]
    for r in results:
        r.pop("elapsed_seconds")
    assert all(r == results[0] for r in results)


# --- deadline --------------------------------------------------------------


def ticking_clock(step: float = 10.0):
    ticks = itertools.count(0.0, step)
    return lambda: next(ticks)


def glyph_page():
    page = np.full((300, 400), 255, np.uint8)
    place(page, render_glyph(), 50, 60)
    return page


def test_finished_scan_is_not_lost_to_the_deadline():
    """Regression: the deadline used to be checked after the last rotation,
    so a scan that had finished its work could still raise and lose results."""
    slow = OpenCVTemplateDetector(clock=ticking_clock())
    settings = ScanSettings(max_runtime_seconds=5, rotations=(0,), coarse_to_fine=False)
    result = slow.detect(glyph_page(), render_glyph(), settings)
    assert [(c.box.x, c.box.y) for c in result.candidates] == [(50, 60)]
    assert result.elapsed_seconds > 5  # it did overrun, but the work was complete


def test_finished_symmetric_scan_is_not_lost_to_the_deadline():
    """Rotations skipped by symmetry are not separate stages either."""
    sym = np.full((21, 21), 255, np.uint8)
    cv2.circle(sym, (10, 10), 7, 0, 2)
    page = np.full((200, 200), 255, np.uint8)
    place(page, sym, 40, 50)
    slow = OpenCVTemplateDetector(clock=ticking_clock())
    result = slow.detect(page, sym, ScanSettings(max_runtime_seconds=5))
    assert len(result.skipped_orientations) == 3
    assert [(c.box.x, c.box.y) for c in result.candidates] == [(40, 50)]


def test_deadline_checked_before_next_rotation():
    slow = OpenCVTemplateDetector(clock=ticking_clock())
    with pytest.raises(DetectionTimeout) as info:
        slow.detect(glyph_page(), render_glyph(), ScanSettings(max_runtime_seconds=5))
    assert "before rotation 90" in str(info.value)


@pytest.mark.parametrize("num_threads", [1, 2])
def test_deadline_checked_between_strips(monkeypatch, num_threads):
    monkeypatch.setattr(matcher, "_STRIP_RESULT_PIXELS", 2000)
    slow = OpenCVTemplateDetector(clock=ticking_clock())
    settings = ScanSettings(max_runtime_seconds=5, rotations=(0,), coarse_to_fine=False,
                            num_threads=num_threads)
    with pytest.raises(DetectionTimeout) as info:
        slow.detect(glyph_page(), render_glyph(), settings)
    assert "strip 2/" in str(info.value)


def test_generous_deadline_never_fires(monkeypatch):
    monkeypatch.setattr(matcher, "_STRIP_RESULT_PIXELS", 2000)
    slow = OpenCVTemplateDetector(clock=ticking_clock(0.001))
    result = slow.detect(glyph_page(), render_glyph(), ScanSettings(max_runtime_seconds=60))
    assert [(c.box.x, c.box.y, c.rotation) for c in result.candidates] == [(50, 60, 0)]
