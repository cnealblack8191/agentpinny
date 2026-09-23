"""Behavioural tests for the OpenCV template detector.

These use synthetic fixtures: they prove the implementation's geometry,
rotation handling, suppression and validation, not real-world accuracy.
"""

from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

from pinny.detection import (
    DEFAULT_SCORE_THRESHOLD,
    BoundingBox,
    DetectionError,
    DetectionTimeout,
    Detector,
    OpenCVTemplateDetector,
    ScanSettings,
    Template,
)
from pinny.detection.suppression import suppress_duplicates

GLYPH_W, GLYPH_H = 24, 36


def make_glyph() -> np.ndarray:
    """Receptacle-like symbol with no rotational symmetry: circle, two
    parallel prongs, a stem on top and a filled marker in one corner."""
    g = np.full((GLYPH_H, GLYPH_W), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (8, 16), (8, 24), 0, 2)
    cv2.line(g, (16, 16), (16, 24), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    cv2.rectangle(g, (0, 31), (5, 35), 0, -1)
    return g


def rotate(img: np.ndarray, rotation: int) -> np.ndarray:
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    return img if rotation == 0 else cv2.rotate(img, codes[rotation])


def blank_page(w: int = 400, h: int = 300) -> np.ndarray:
    return np.full((h, w), 255, dtype=np.uint8)


def place(page: np.ndarray, img: np.ndarray, x: int, y: int) -> None:
    h, w = img.shape[:2]
    page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], img)


def boxes_of(result):
    return sorted((c.box.x, c.box.y, c.box.width, c.box.height, c.rotation) for c in result.candidates)


@pytest.fixture
def detector() -> OpenCVTemplateDetector:
    return OpenCVTemplateDetector()


@pytest.fixture
def glyph() -> np.ndarray:
    return make_glyph()


def test_implements_detector_interface(detector):
    assert isinstance(detector, Detector)
    assert ScanSettings().threshold == DEFAULT_SCORE_THRESHOLD == 0.80


def test_known_placements(detector, glyph):
    page = blank_page()
    positions = [(30, 40), (150, 60), (300, 200)]
    for x, y in positions:
        place(page, glyph, x, y)
    result = detector.detect(page, glyph, ScanSettings(rotations=(0,)))
    assert boxes_of(result) == sorted((x, y, GLYPH_W, GLYPH_H, 0) for x, y in positions)
    for c in result.candidates:
        assert c.score == pytest.approx(1.0, abs=1e-4)
        assert c.center == (c.box.x + GLYPH_W / 2, c.box.y + GLYPH_H / 2)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_quarter_turn_rotations(detector, glyph, rotation):
    page = blank_page()
    rotated = rotate(glyph, rotation)
    place(page, rotated, 120, 90)
    result = detector.detect(page, glyph)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.rotation == rotation
    expected_w, expected_h = (GLYPH_H, GLYPH_W) if rotation in (90, 270) else (GLYPH_W, GLYPH_H)
    assert (c.box.x, c.box.y, c.box.width, c.box.height) == (120, 90, expected_w, expected_h)
    assert c.center == (120 + expected_w / 2, 90 + expected_h / 2)
    assert c.score == pytest.approx(1.0, abs=1e-4)


def test_mixed_rotations_on_one_page(detector, glyph):
    page = blank_page(500, 300)
    placements = {0: (20, 20), 90: (120, 30), 180: (260, 150), 270: (400, 200)}
    for rotation, (x, y) in placements.items():
        place(page, rotate(glyph, rotation), x, y)
    result = detector.detect(page, glyph)
    found = {c.rotation: (c.box.x, c.box.y) for c in result.candidates}
    assert found == placements


def test_duplicates_suppressed_across_rotations(detector, glyph):
    """At a low threshold every rotation fires on the one symbol; only the
    best candidate may survive."""
    page = blank_page()
    place(page, glyph, 100, 100)
    threshold = 0.55
    for rotation in (90, 180, 270):  # precondition: real cross-rotation duplicates
        raw = cv2.matchTemplate(page, rotate(glyph, rotation), cv2.TM_CCOEFF_NORMED)
        assert raw[80:130, 80:130].max() > threshold
    result = detector.detect(page, glyph, ScanSettings(threshold=threshold))
    assert len(result.candidates) == 1
    assert result.candidates[0].rotation == 0
    assert (result.candidates[0].box.x, result.candidates[0].box.y) == (100, 100)


def test_symmetric_symbol_reports_single_candidate(detector):
    """A rotationally symmetric symbol matches equally at all rotations."""
    sym = np.full((20, 20), 255, dtype=np.uint8)
    cv2.circle(sym, (10, 10), 7, 0, 2)
    page = blank_page()
    place(page, sym, 50, 60)
    result = detector.detect(page, sym)
    assert len(result.candidates) == 1
    assert (result.candidates[0].box.x, result.candidates[0].box.y) == (50, 60)


@pytest.mark.parametrize("gap", [0, 2, 6])
def test_nearby_distinct_symbols_kept_separate(detector, glyph, gap):
    page = blank_page()
    place(page, glyph, 100, 100)
    place(page, glyph, 100 + GLYPH_W + gap, 100)
    place(page, glyph, 100, 100 + GLYPH_H + gap)
    result = detector.detect(page, glyph)
    assert boxes_of(result) == sorted(
        [
            (100, 100, GLYPH_W, GLYPH_H, 0),
            (100 + GLYPH_W + gap, 100, GLYPH_W, GLYPH_H, 0),
            (100, 100 + GLYPH_H + gap, GLYPH_W, GLYPH_H, 0),
        ]
    )


def test_nearby_symbols_with_different_rotations(detector, glyph):
    page = blank_page()
    place(page, glyph, 100, 100)
    place(page, rotate(glyph, 90), 100 + GLYPH_W + 1, 100)
    result = detector.detect(page, glyph)
    assert boxes_of(result) == sorted(
        [(100, 100, GLYPH_W, GLYPH_H, 0), (100 + GLYPH_W + 1, 100, GLYPH_H, GLYPH_W, 90)]
    )


def test_page_edges_and_corners(detector, glyph):
    page = blank_page(200, 150)
    corners = [(0, 0), (200 - GLYPH_W, 0), (0, 150 - GLYPH_H), (200 - GLYPH_W, 150 - GLYPH_H)]
    for x, y in corners:
        place(page, glyph, x, y)
    result = detector.detect(page, glyph, ScanSettings(rotations=(0,)))
    assert boxes_of(result) == sorted((x, y, GLYPH_W, GLYPH_H, 0) for x, y in corners)
    for c in result.candidates:
        assert c.box.x >= 0 and c.box.y >= 0 and c.box.x2 <= 200 and c.box.y2 <= 150


def test_rotated_symbol_flush_with_edge(detector, glyph):
    page = blank_page(200, 150)
    place(page, rotate(glyph, 270), 200 - GLYPH_H, 150 - GLYPH_W)
    result = detector.detect(page, glyph)
    assert boxes_of(result) == [(200 - GLYPH_H, 150 - GLYPH_W, GLYPH_H, GLYPH_W, 270)]


def test_no_matches_on_unrelated_drawing(detector, glyph):
    page = blank_page()
    cv2.rectangle(page, (20, 20), (380, 280), 0, 2)
    cv2.line(page, (20, 150), (380, 150), 0, 1)
    cv2.putText(page, "PANEL A", (60, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
    result = detector.detect(page, glyph)
    assert result.candidates == ()
    assert not result.truncated


def test_blank_page_has_no_matches(detector, glyph):
    """Flat page windows have undefined correlation and must never match."""
    result = detector.detect(blank_page(), glyph, ScanSettings(threshold=-1.0))
    assert result.candidates == ()


def test_color_page_matches_grayscale(detector, glyph):
    gray_page = blank_page()
    place(gray_page, glyph, 70, 80)
    rgb_page = cv2.cvtColor(gray_page, cv2.COLOR_GRAY2RGB)
    rgb_template = cv2.cvtColor(glyph, cv2.COLOR_GRAY2RGB)
    result = detector.detect(rgb_page, rgb_template)
    assert boxes_of(result) == [(70, 80, GLYPH_W, GLYPH_H, 0)]


def test_template_from_page_crop(detector, glyph):
    page = blank_page()
    place(page, glyph, 40, 50)
    place(page, rotate(glyph, 180), 250, 200)
    template = Template.from_page_crop(page, BoundingBox(40, 50, GLYPH_W, GLYPH_H))
    result = detector.detect(page, template)
    assert boxes_of(result) == sorted(
        [(40, 50, GLYPH_W, GLYPH_H, 0), (250, 200, GLYPH_W, GLYPH_H, 180)]
    )


def test_search_region_offset_returns_page_coordinates(detector, glyph):
    page = blank_page()
    place(page, glyph, 30, 30)
    place(page, rotate(glyph, 90), 250, 180)
    region = BoundingBox(200, 150, 150, 120)
    result = detector.detect(page, glyph, ScanSettings(search_region=region))
    assert boxes_of(result) == [(250, 180, GLYPH_H, GLYPH_W, 90)]


def test_results_sorted_by_score(detector, glyph):
    page = blank_page()
    place(page, glyph, 20, 20)
    degraded = glyph.copy()
    degraded[10:20, :] = 255
    place(page, degraded, 200, 100)
    result = detector.detect(page, glyph, ScanSettings(threshold=0.5, rotations=(0,)))
    scores = [c.score for c in result.candidates]
    assert len(scores) == 2 and scores == sorted(scores, reverse=True)
    assert scores[0] > scores[1]


def test_candidate_cap_sets_truncated(detector, glyph):
    page = blank_page(600, 300)
    for i, j in itertools.product(range(8), range(4)):
        place(page, glyph, 10 + i * 60, 10 + j * 60)
    result = detector.detect(page, glyph, ScanSettings(max_candidates=5, rotations=(0,)))
    assert len(result.candidates) == 5
    assert result.truncated
    assert any("limit" in w for w in result.warnings)

    uncapped = detector.detect(page, glyph, ScanSettings(rotations=(0,)))
    assert len(uncapped.candidates) == 32 and not uncapped.truncated


def test_per_rotation_cap_sets_truncated(detector, glyph):
    page = blank_page(600, 300)
    for i in range(8):
        place(page, glyph, 10 + i * 60, 10)
    result = detector.detect(
        page, glyph, ScanSettings(max_candidates_per_rotation=3, rotations=(0,))
    )
    assert len(result.candidates) == 3 and result.truncated


def test_timeout_is_enforced(glyph):
    ticks = iter(range(0, 1000, 10))
    slow = OpenCVTemplateDetector(clock=lambda: float(next(ticks)))
    page = blank_page()
    place(page, glyph, 10, 10)
    with pytest.raises(DetectionTimeout) as info:
        slow.detect(page, glyph, ScanSettings(max_runtime_seconds=5))
    assert info.value.code == "timeout"


def test_result_serialises(detector, glyph):
    page = blank_page()
    place(page, glyph, 10, 20)
    d = detector.detect(page, glyph).to_dict()
    assert d["candidates"][0]["box"] == {"x": 10, "y": 20, "width": GLYPH_W, "height": GLYPH_H}
    assert d["candidates"][0]["center"] == {"x": 22.0, "y": 38.0}
    assert d["rotations_searched"] == [0, 90, 180, 270]
    assert d["detector"] == OpenCVTemplateDetector.name == "opencv-template"


def test_settings_serialise_for_scan_record():
    """docs/contracts.md section 4 stores ScanSettings as a dict."""
    import json

    settings = ScanSettings(threshold=0.9, rotations=(0, 180), search_region=BoundingBox(1, 2, 30, 40))
    d = settings.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["threshold"] == 0.9 and d["rotations"] == [0, 180]
    assert d["search_region"] == {"x": 1, "y": 2, "width": 30, "height": 40}
    assert ScanSettings().to_dict()["search_region"] is None


def test_contract_rgb_raster(detector, glyph):
    """The app passes uint8 RGB (H, W, 3) canonical rasters (contracts section 2)."""
    page = np.full((300, 400, 3), 255, dtype=np.uint8)
    rgb_glyph = cv2.cvtColor(rotate(glyph, 90), cv2.COLOR_GRAY2RGB)
    rgb_glyph[rgb_glyph[:, :, 0] == 0] = (200, 0, 0)  # red line work
    page[50 : 50 + GLYPH_W, 60 : 60 + GLYPH_H] = rgb_glyph
    template = Template.from_page_crop(page, BoundingBox(60, 50, GLYPH_H, GLYPH_W))
    result = detector.detect(page, template)
    assert boxes_of(result) == [(60, 50, GLYPH_H, GLYPH_W, 0)]


# --- invalid input -------------------------------------------------------


def expect_error(code, fn, *args, **kwargs):
    with pytest.raises(DetectionError) as info:
        fn(*args, **kwargs)
    assert info.value.code == code, str(info.value)
    assert str(info.value)
    return info.value


def test_blank_template_rejected(detector):
    expect_error("template_blank", detector.detect, blank_page(), np.full((20, 20), 255, np.uint8))


def test_near_uniform_template_rejected(detector):
    rng = np.random.default_rng(0)
    noisy = (250 + rng.integers(0, 3, size=(20, 20))).astype(np.uint8)
    expect_error("template_blank", detector.detect, blank_page(), noisy)


def test_tiny_template_rejected(detector, glyph):
    expect_error("template_too_small", detector.detect, blank_page(), glyph[:5, :5])


def test_oversized_template_rejected(detector, glyph):
    big = cv2.resize(glyph, (GLYPH_W * 10, GLYPH_H * 10), interpolation=cv2.INTER_NEAREST)
    expect_error("template_too_large", detector.detect, blank_page(), big)
    expect_error(
        "template_too_large",
        detector.detect,
        blank_page(),
        glyph,
        ScanSettings(max_template_side=20),
    )


def test_template_not_fitting_rotated_rejected(detector, glyph):
    page = blank_page(100, 30)  # glyph fits only when rotated
    err = expect_error("template_too_large", detector.detect, page, glyph)
    assert "rotated 0" in str(err)
    detector.detect(page, glyph, ScanSettings(rotations=(90, 270)))


@pytest.mark.parametrize(
    "box",
    [
        BoundingBox(-1, 0, 10, 10),
        BoundingBox(0, 0, 500, 10),
        BoundingBox(395, 295, 10, 10),
    ],
)
def test_invalid_crop_rejected(box):
    expect_error("invalid_crop", Template.from_page_crop, blank_page(), box)


@pytest.mark.parametrize("args", [(0, 0, 0, 10), (0, 0, 10, -1), (0.5, 0, 10, 10), (True, 0, 1, 1)])
def test_invalid_box_values_rejected(args):
    expect_error("invalid_box", BoundingBox, *args)


def test_non_uint8_and_nan_inputs_rejected(detector, glyph):
    nan_template = glyph.astype(np.float32)
    nan_template[0, 0] = np.nan
    expect_error("invalid_template", detector.detect, blank_page(), nan_template)
    expect_error("invalid_page", detector.detect, blank_page().astype(np.float64), glyph)
    expect_error("invalid_page", detector.detect, [[0, 1]], glyph)
    expect_error("invalid_template", detector.detect, blank_page(), np.zeros((0, 10), np.uint8))
    expect_error(
        "invalid_template", detector.detect, blank_page(), np.zeros((10, 10, 2), np.uint8)
    )


def test_channel_mismatch_rejected(detector, glyph):
    rgb_page = cv2.cvtColor(blank_page(), cv2.COLOR_GRAY2RGB)
    expect_error("channel_mismatch", detector.detect, rgb_page, glyph)


def test_page_too_large_rejected(detector, glyph):
    expect_error(
        "page_too_large", detector.detect, blank_page(), glyph, ScanSettings(max_page_pixels=1000)
    )


def test_search_region_outside_page_rejected(detector, glyph):
    expect_error(
        "invalid_search_region",
        detector.detect,
        blank_page(),
        glyph,
        ScanSettings(search_region=BoundingBox(300, 200, 200, 200)),
    )


@pytest.mark.parametrize(
    "settings",
    [
        ScanSettings(threshold=1.5),
        ScanSettings(threshold=float("nan")),
        ScanSettings(rotations=(45,)),
        ScanSettings(rotations=()),
        ScanSettings(rotations=(0, 0)),
        ScanSettings(nms_iou_threshold=1.0),
        ScanSettings(duplicate_center_ratio=-0.1),
        ScanSettings(max_candidates=0),
        ScanSettings(max_runtime_seconds=float("inf")),
        ScanSettings(min_template_side=50, max_template_side=40),
    ],
)
def test_invalid_settings_rejected(detector, glyph, settings):
    expect_error("invalid_settings", detector.detect, blank_page(), glyph, settings)


# --- suppression unit tests ----------------------------------------------


def test_suppression_iou_and_center_rules():
    boxes = np.array(
        [
            [0, 0, 20, 40],  # best
            [2, 2, 20, 40],  # heavy overlap -> duplicate
            [-10, 10, 40, 20],  # same center, rotated shape, IoU 1/3 -> duplicate by center
            [21, 0, 20, 40],  # adjacent, no overlap -> kept
        ]
    )
    scores = np.array([0.99, 0.95, 0.97, 0.9])
    keep = suppress_duplicates(boxes, scores, iou_threshold=0.5, center_ratio=0.5, limit=10)
    assert list(keep) == [0, 3]


def test_suppression_limit_and_empty():
    assert len(suppress_duplicates(np.empty((0, 4)), np.empty(0), 0.5, 0.5, 10)) == 0
    boxes = np.array([[i * 50, 0, 10, 10] for i in range(5)])
    keep = suppress_duplicates(boxes, np.linspace(1, 0.5, 5), 0.5, 0.5, limit=2)
    assert list(keep) == [0, 1]
