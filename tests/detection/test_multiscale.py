"""Multi-scale template search.

Symbols here are *redrawn* at each size with a fixed line weight, as a
drawing plotted at a different scale would be, rather than resized copies
of the template. These are still synthetic: they prove the scale search
finds resized symbols and reports them in page coordinates, not real-world
accuracy.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from pinny.detection import (
    DEFAULT_SCALE_STEP,
    DetectionError,
    DetectionTimeout,
    OpenCVTemplateDetector,
    ScanSettings,
    scale_range,
)

BASE_W, BASE_H = 40, 48
_SHIFT = 4  # cv2 fixed-point bits, for sub-pixel drawing
_F = 1 << _SHIFT


def draw_symbol(scale: float) -> np.ndarray:
    """Receptacle-like symbol drawn at ``scale`` with a 2 px line weight:
    circle, two prongs, a stem on top and a filled corner marker (so no
    rotational symmetry)."""
    w, h = int(round(BASE_W * scale)), int(round(BASE_H * scale))
    g = np.full((h, w), 255, dtype=np.uint8)

    def p(x, y):
        return (int(round(x * scale * _F)), int(round(y * scale * _F)))

    cv2.circle(g, p(20, 28), int(round(12 * scale * _F)), 0, 2, cv2.LINE_AA, _SHIFT)
    cv2.line(g, p(14, 22), p(14, 34), 0, 2, cv2.LINE_AA, _SHIFT)
    cv2.line(g, p(26, 22), p(26, 34), 0, 2, cv2.LINE_AA, _SHIFT)
    cv2.line(g, p(20, 2), p(20, 15), 0, 2, cv2.LINE_AA, _SHIFT)
    cv2.rectangle(g, p(1, 41), p(8, 46), 0, -1, cv2.LINE_AA, _SHIFT)
    return g


def place(page: np.ndarray, img: np.ndarray, x: int, y: int) -> None:
    h, w = img.shape[:2]
    page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], img)


def blank_page(w: int = 600, h: int = 400) -> np.ndarray:
    return np.full((h, w), 255, dtype=np.uint8)


@pytest.fixture
def detector() -> OpenCVTemplateDetector:
    return OpenCVTemplateDetector()


@pytest.fixture
def template() -> np.ndarray:
    return draw_symbol(1.0)


def test_default_is_single_scale_and_serialises(detector, template):
    page = blank_page()
    place(page, template, 30, 40)
    result = detector.detect(page, template)
    assert ScanSettings().scales == (1.0,)
    assert result.scales_searched == (1.0,)
    (c,) = result.candidates
    assert c.scale == 1.0
    d = result.to_dict()
    assert d["scales_searched"] == [1.0]
    assert d["candidates"][0]["scale"] == 1.0


# The v1.1 blur already tolerates size differences of about 12%, so these
# sizes lie beyond what a single-scale search finds.
@pytest.mark.parametrize("true_scale", [0.8, 0.85, 1.2, 1.25])
def test_resized_symbol_missed_at_one_scale_found_with_range(detector, template, true_scale):
    page = blank_page()
    symbol = draw_symbol(true_scale)
    place(page, symbol, 100, 80)
    true_center = (100 + symbol.shape[1] / 2, 80 + symbol.shape[0] / 2)

    assert detector.detect(page, template).candidates == ()

    result = detector.detect(page, template, ScanSettings(scales=scale_range(0.8, 1.25)))
    (c,) = result.candidates
    assert c.rotation == 0
    assert abs(c.scale - true_scale) <= 0.03
    assert math.dist(c.center, true_center) <= 2.0
    # The box is the scaled template's footprint, in page coordinates.
    assert c.box.width == int(round(BASE_W * c.scale))
    assert c.box.height == int(round(BASE_H * c.scale))


def test_mixed_sizes_and_rotations_on_one_page(detector, template):
    page = blank_page(800, 500)
    placed = []
    for i, (scale, rotation) in enumerate([(0.85, 0), (1.0, 90), (1.15, 180), (0.95, 270), (1.2, 90)]):
        symbol = np.rot90(draw_symbol(scale), -rotation // 90)  # clockwise
        x, y = 40 + i * 150, 60 + (i % 2) * 200
        place(page, symbol, x, y)
        placed.append(((x + symbol.shape[1] / 2, y + symbol.shape[0] / 2), scale, rotation))

    result = detector.detect(page, template, ScanSettings(scales=scale_range(0.8, 1.25)))
    assert len(result.candidates) == len(placed)
    for center, scale, rotation in placed:
        (c,) = [c for c in result.candidates if math.dist(c.center, center) <= 2.0]
        assert c.rotation == rotation
        assert abs(c.scale - scale) <= 0.03


def test_neighbouring_scales_collapse_to_one_candidate(detector, template):
    page = blank_page()
    place(page, template, 50, 50)
    # A fine step: several scales match the one symbol above threshold.
    scales = scale_range(0.9, 1.1, 1.01)
    result = detector.detect(page, template, ScanSettings(scales=scales, threshold=0.6))
    (c,) = result.candidates
    assert c.scale == 1.0
    assert c.score == pytest.approx(1.0, abs=1e-4)
    # Scales that round to an identical template size are searched once,
    # keeping the one closest to 1.0.
    assert 0.9905 in scales and 0.9905 not in result.scales_searched
    assert 1.0 in result.scales_searched
    sizes = {(round(BASE_W * s), round(BASE_H * s)) for s in result.scales_searched}
    assert len(sizes) == len(result.scales_searched)
    assert any("same template size" in w for w in result.warnings)


def test_adjacent_symbols_of_different_sizes_kept_separate(detector, template):
    page = blank_page()
    small, large = draw_symbol(0.85), draw_symbol(1.2)
    place(page, small, 100, 100)
    place(page, large, 100 + small.shape[1] + 2, 100)
    result = detector.detect(page, template, ScanSettings(scales=scale_range(0.8, 1.25)))
    found = sorted(c.scale for c in result.candidates)
    assert len(found) == 2
    assert found[0] == pytest.approx(0.85, abs=0.03)
    assert found[1] == pytest.approx(1.2, abs=0.03)


def test_multiscale_on_rgb_page(detector, template):
    page = blank_page()
    symbol = draw_symbol(1.1)
    place(page, symbol, 200, 120)
    rgb = np.dstack([page] * 3)
    tmpl = np.dstack([template] * 3)
    (c,) = detector.detect(rgb, tmpl, ScanSettings(scales=scale_range(0.9, 1.2))).candidates
    assert abs(c.scale - 1.1) <= 0.03


def test_search_region_offset_with_scales(detector, template):
    page = blank_page()
    symbol = draw_symbol(0.9)
    place(page, symbol, 300, 200)
    from pinny.detection import BoundingBox

    region = BoundingBox(250, 150, 200, 200)
    settings = ScanSettings(scales=scale_range(0.85, 1.0), search_region=region)
    (c,) = detector.detect(page, template, settings).candidates
    assert math.dist(c.center, (300 + symbol.shape[1] / 2, 200 + symbol.shape[0] / 2)) <= 2.0


def test_timeout_checked_between_scale_passes(template):
    ticks = iter(range(0, 1000, 2))
    slow = OpenCVTemplateDetector(clock=lambda: float(next(ticks)))
    page = blank_page()
    with pytest.raises(DetectionTimeout) as info:
        slow.detect(page, template, ScanSettings(rotations=(0,), scales=(0.9, 1.0, 1.1), max_runtime_seconds=3))
    assert "scale" in str(info.value)


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "scales",
    [(), [], None, "1.0", (0,), (-1.0,), (0.2,), (4.5,), (float("nan"),), (float("inf"),), (True,), (1.0, 1.0), ("1",)],
)
def test_invalid_scales_rejected(detector, template, scales):
    with pytest.raises(DetectionError) as info:
        detector.detect(blank_page(), template, ScanSettings(scales=scales))
    assert info.value.code == "invalid_settings"


def test_scale_shrinking_template_below_minimum_rejected(detector, template):
    with pytest.raises(DetectionError) as info:
        detector.detect(blank_page(), template, ScanSettings(scales=(0.25, 1.0), min_template_side=16))
    assert info.value.code == "template_too_small"
    assert "scaled by 0.25" in str(info.value)


def test_scaled_template_not_fitting_region_rejected(detector, template):
    page = blank_page(100, 60)
    with pytest.raises(DetectionError) as info:
        detector.detect(page, template, ScanSettings(scales=(1.0, 2.0), rotations=(0,)))
    assert info.value.code == "template_too_large"
    assert "scaled by 2" in str(info.value)


# --- scale_range -------------------------------------------------------------


@pytest.mark.parametrize(
    "low,high,step",
    [(0.8, 1.25, DEFAULT_SCALE_STEP), (0.9, 1.1, 1.05), (1.1, 1.5, 1.04), (0.5, 0.9, 1.1), (0.3, 3.0, 1.2)],
)
def test_scale_range_properties(low, high, step):
    values = scale_range(low, high, step)
    assert values == tuple(sorted(set(values)))
    assert values[0] == pytest.approx(low) and values[-1] == pytest.approx(high)
    if low <= 1.0 <= high:
        assert 1.0 in values
    ratios = [b / a for a, b in zip(values, values[1:])]
    assert all(r <= step + 1e-3 for r in ratios)
    ScanSettings(scales=values).validate()


def test_scale_range_single_value():
    assert scale_range(1.0, 1.0) == (1.0,)
    assert scale_range(0.9, 0.9) == (0.9,)


@pytest.mark.parametrize(
    "args",
    [(1.2, 1.0), (0.1, 1.0), (1.0, 5.0), (0.9, 1.1, 1.0), (0.9, 1.1, 3.0), (float("nan"), 1.0), (0.9, True)],
)
def test_scale_range_invalid(args):
    with pytest.raises(DetectionError) as info:
        scale_range(*args)
    assert info.value.code == "invalid_settings"
