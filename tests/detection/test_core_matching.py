"""Tests for preprocessing (blur), sparse validity checks, symmetry-aware
rotations and mirrored-symbol search in the OpenCV detector.

Synthetic fixtures: they prove implementation behaviour, not real-world
accuracy.
"""

from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

import pinny.detection.opencv_matcher as matcher
from pinny.detection import (
    DEFAULT_SCORE_THRESHOLD,
    BoundingBox,
    DetectionError,
    OpenCVTemplateDetector,
    ScanSettings,
)

GLYPH_W, GLYPH_H = 24, 36
SUPERSAMPLE = 8


def make_glyph() -> np.ndarray:
    """Same asymmetric receptacle-like glyph as test_opencv_matcher.py."""
    g = np.full((GLYPH_H, GLYPH_W), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (8, 16), (8, 24), 0, 2)
    cv2.line(g, (16, 16), (16, 24), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    cv2.rectangle(g, (0, 31), (5, 35), 0, -1)
    return g


def render_glyph(dx: float = 0.0, dy: float = 0.0, scale: float = 1.0) -> np.ndarray:
    """The glyph drawn at 8x resolution with a sub-pixel offset and a scale,
    then area-downsampled: what a PDF renderer produces for a symbol that is
    not pixel-aligned or is drawn at a slightly different size."""
    s = SUPERSAMPLE
    w, h = int(round(GLYPH_W * scale)) + 4, int(round(GLYPH_H * scale)) + 4
    g = np.full((h * s, w * s), 255, np.uint8)

    def p(x, y):
        return (int(round((x * scale + 2 + dx) * s)), int(round((y * scale + 2 + dy) * s)))

    lw = max(1, int(round(2 * scale * s)))
    cv2.circle(g, p(12, 20), int(round(9 * scale * s)), 0, lw)
    cv2.line(g, p(8, 16), p(8, 24), 0, lw)
    cv2.line(g, p(16, 16), p(16, 24), 0, lw)
    cv2.line(g, p(12, 0), p(12, 10), 0, lw)
    cv2.rectangle(g, p(0, 31), p(5, 35), 0, -1)
    return cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA)


def rotate(img: np.ndarray, rotation: int) -> np.ndarray:
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    return img if rotation == 0 else cv2.rotate(img, codes[rotation])


def mirror_then_rotate(img: np.ndarray, rotation: int) -> np.ndarray:
    return rotate(cv2.flip(img, 1), rotation)


def blank_page(w: int = 400, h: int = 300) -> np.ndarray:
    return np.full((h, w), 255, dtype=np.uint8)


def place(page: np.ndarray, img: np.ndarray, x: int, y: int) -> None:
    h, w = img.shape[:2]
    page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], img)


def found(result):
    return sorted(
        (c.box.x, c.box.y, c.box.width, c.box.height, c.rotation, c.mirrored)
        for c in result.candidates
    )


@pytest.fixture
def detector() -> OpenCVTemplateDetector:
    return OpenCVTemplateDetector()


@pytest.fixture
def glyph() -> np.ndarray:
    return make_glyph()


# --- blur -----------------------------------------------------------------


def best_score_at(detector, page, template, x, y, sigma, rotation=0):
    result = detector.detect(
        page,
        template,
        ScanSettings(blur_sigma=sigma, threshold=0.3, rotations=(rotation,)),
    )
    near = [c.score for c in result.candidates if abs(c.box.x - x) <= 4 and abs(c.box.y - y) <= 4]
    assert near, "symbol not found at all"
    return max(near)


def symbol_page(img: np.ndarray) -> np.ndarray:
    page = blank_page(200, 200)
    place(page, img, 80, 70)
    return page


def test_blur_improves_worst_sub_pixel_shift(detector):
    template = render_glyph()
    shifts = [(dx, dy) for dx in (0, 0.25, 0.5, 0.75) for dy in (0, 0.25, 0.5, 0.75)]
    worst = {
        sigma: min(
            best_score_at(detector, symbol_page(render_glyph(dx, dy)), template, 80, 70, sigma)
            for dx, dy in shifts
        )
        for sigma in (0.0, 1.0)
    }
    # Measured: 0.859 without blur, 0.947 with sigma=1.
    assert worst[0.0] < 0.90
    assert worst[1.0] > 0.93
    assert worst[1.0] > worst[0.0] + 0.05


@pytest.mark.parametrize("scale", [0.95, 1.05])
def test_blur_improves_small_scale_difference(detector, scale):
    template = render_glyph()
    page = symbol_page(render_glyph(scale=scale))
    unblurred = best_score_at(detector, page, template, 80, 70, 0.0)
    blurred = best_score_at(detector, page, template, 80, 70, 1.0)
    # Measured: about 0.82-0.83 without blur, 0.93 with sigma=1.
    assert unblurred < 0.85
    assert blurred > 0.90
    assert blurred > unblurred + 0.05


@pytest.mark.parametrize(
    "draw_wire",
    [
        lambda p: cv2.line(p, (0, 95), (199, 95), 0, 1),
        lambda p: cv2.line(p, (60, 60), (140, 140), 0, 1, cv2.LINE_AA),
    ],
    ids=["horizontal", "diagonal-antialiased"],
)
def test_blur_improves_wire_crossed_symbol(detector, draw_wire):
    template = render_glyph()
    page = symbol_page(template)
    draw_wire(page)
    unblurred = best_score_at(detector, page, template, 80, 70, 0.0)
    blurred = best_score_at(detector, page, template, 80, 70, 1.0)
    assert blurred > unblurred + 0.02
    assert blurred > DEFAULT_SCORE_THRESHOLD


def test_exact_symbol_scores_one_with_blur(detector, glyph):
    page = blank_page()
    place(page, glyph, 0, 0)  # touching the page corner
    place(page, glyph, 150, 100)
    result = detector.detect(page, glyph, ScanSettings(rotations=(0,), blur_sigma=1.5))
    assert found(result) == [(0, 0, GLYPH_W, GLYPH_H, 0, False), (150, 100, GLYPH_W, GLYPH_H, 0, False)]
    for c in result.candidates:
        assert c.score == pytest.approx(1.0, abs=1e-3)


def test_search_region_does_not_change_blurred_scores(detector, glyph):
    page = blank_page()
    place(page, glyph, 100, 100)
    cv2.line(page, (0, 99), (399, 99), 0, 1)  # line work right at the region edge
    whole = detector.detect(page, glyph, ScanSettings(threshold=0.5))
    region = detector.detect(
        page, glyph, ScanSettings(threshold=0.5, search_region=BoundingBox(100, 100, 60, 60))
    )
    inside = [c for c in whole.candidates if c.box.x >= 100 and c.box.y >= 100
              and c.box.x2 <= 160 and c.box.y2 <= 160]
    assert [(c.box, c.rotation) for c in region.candidates] == [(c.box, c.rotation) for c in inside]
    for a, b in zip(region.candidates, inside):
        assert a.score == pytest.approx(b.score, abs=1e-5)


def clutter_page(seed: int, similar_symbols: bool) -> np.ndarray:
    """Lines, text, circles and boxes; optionally near-variants of the glyph
    (circle with one prong, circle with a stem and no prongs)."""
    rng = np.random.default_rng(seed)
    p = np.full((600, 800), 255, np.uint8)
    for _ in range(25):
        x0, y0 = int(rng.integers(0, 800)), int(rng.integers(0, 600))
        length, width = int(rng.integers(30, 400)), int(rng.integers(1, 3))
        end = (x0 + length, y0) if rng.random() < 0.5 else (x0, y0 + length)
        cv2.line(p, (x0, y0), end, 0, width)
    for _ in range(15):
        text = str(rng.choice(["RM 101", "PANEL A", "20A", "GFI", "WP", "J-BOX", "(E)"]))
        cv2.putText(p, text, (int(rng.integers(0, 700)), int(rng.integers(20, 600))),
                    cv2.FONT_HERSHEY_SIMPLEX, float(rng.uniform(0.4, 1.0)), 0,
                    int(rng.integers(1, 3)))
    for _ in range(15):
        c = (int(rng.integers(20, 780)), int(rng.integers(20, 580)))
        kind = int(rng.integers(0, 4 if similar_symbols else 2))
        if kind == 0:
            cv2.circle(p, c, int(rng.integers(5, 15)), 0, 2)
        elif kind == 1:
            cv2.rectangle(p, c, (c[0] + int(rng.integers(8, 30)), c[1] + int(rng.integers(8, 30))), 0, 2)
        elif kind == 2:
            cv2.circle(p, c, 9, 0, 2)
            cv2.line(p, (c[0], c[1] - 4), (c[0], c[1] + 4), 0, 2)
        else:
            cv2.circle(p, c, 9, 0, 2)
            cv2.line(p, (c[0], c[1] - 20), (c[0], c[1] - 10), 0, 2)
    return p


@pytest.mark.parametrize("similar_symbols", [False, True], ids=["generic", "near-variants"])
def test_clutter_stays_below_default_threshold_with_blur(detector, glyph, similar_symbols):
    """Guards DEFAULT_SCORE_THRESHOLD = 0.80 with blur on. Measured maxima
    over these 20 pages: generic clutter 0.73 (0.70 unblurred), near-variant
    symbols 0.785 (0.78 unblurred)."""
    worst = -1.0
    for seed in range(20):
        result = detector.detect(clutter_page(seed, similar_symbols), glyph,
                                 ScanSettings(threshold=0.5))
        worst = max([worst] + [c.score for c in result.candidates])
    assert worst < DEFAULT_SCORE_THRESHOLD
    assert worst < (0.75 if not similar_symbols else 0.795)


# --- sparse validity --------------------------------------------------------


def busy_page(seed: int) -> np.ndarray:
    page = clutter_page(seed, similar_symbols=True)
    rng = np.random.default_rng(100 + seed)
    g = make_glyph()
    for i in range(12):
        img = rotate(g, int(rng.choice([0, 90, 180, 270])))
        place(page, img, int(rng.integers(0, 800 - 40)), int(rng.integers(0, 600 - 40)))
    return page


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("threshold", [0.8, 0.5, 0.2])
@pytest.mark.parametrize("blur_sigma", [0.0, 1.0])
def test_sparse_validity_matches_dense_passes(detector, glyph, monkeypatch, seed, threshold, blur_sigma):
    page = busy_page(seed)
    settings = ScanSettings(threshold=threshold, blur_sigma=blur_sigma, max_candidates=5000,
                            max_candidates_per_rotation=100000)
    sparse = detector.detect(page, glyph, settings)
    monkeypatch.setattr(matcher, "_DENSE_MIN_CANDIDATES", 0)
    monkeypatch.setattr(matcher, "_DENSE_CANDIDATE_FRACTION", 0.0)
    dense = detector.detect(page, glyph, settings)
    assert found(sparse) == found(dense)
    for a, b in zip(sorted(sparse.candidates, key=lambda c: (c.box.y, c.box.x)),
                    sorted(dense.candidates, key=lambda c: (c.box.y, c.box.x))):
        assert a.score == pytest.approx(b.score, abs=1e-6)
    assert sparse.warnings == dense.warnings


@pytest.mark.parametrize("threshold", [0.8, 0.3])
def test_local_max_loop_matches_dilation(detector, glyph, monkeypatch, threshold):
    page = busy_page(3)
    settings = ScanSettings(threshold=threshold, max_candidates=5000, coarse_to_fine=False)
    monkeypatch.setattr(matcher, "_LOCAL_MAX_LOOP_LIMIT", 10**9)
    loop = detector.detect(page, glyph, settings)
    monkeypatch.setattr(matcher, "_LOCAL_MAX_LOOP_LIMIT", 0)
    monkeypatch.setattr(matcher, "_LOCAL_MAX_PIXELS_PER_LOOP", 10**12)
    dilated = detector.detect(page, glyph, settings)
    assert len(loop.candidates) > 0
    assert [(c.box, c.rotation, c.score) for c in loop.candidates] == [
        (c.box, c.rotation, c.score) for c in dilated.candidates
    ]


def test_variance_gate_rejects_faint_copies(detector, glyph):
    """Correlation ignores contrast, so a faint ghost of the symbol (range 10
    grey levels, which passes the old range >= 2 flat test) scores ~1.0. Its
    variance is far below 5% of the template's, so it is rejected; a
    moderately faint copy (40% contrast) is still found."""
    ghost = (255 - (255 - glyph.astype(np.int32)) * 10 // 255).astype(np.uint8)
    faint = (255 - (255 - glyph.astype(np.int32)) * 100 // 255).astype(np.uint8)
    page = blank_page()
    place(page, ghost, 40, 40)
    place(page, faint, 200, 150)
    raw = cv2.matchTemplate(page, glyph, cv2.TM_CCOEFF_NORMED)
    assert raw[40, 40] > 0.95  # precondition: the ghost would match
    for sigma in (0.0, 1.0):
        result = detector.detect(page, glyph, ScanSettings(rotations=(0,), blur_sigma=sigma))
        assert found(result) == [(200, 150, GLYPH_W, GLYPH_H, 0, False)]


def test_threshold_minus_one_on_blank_page(detector, glyph):
    """Every window is a candidate and every one is flat."""
    result = detector.detect(blank_page(), glyph, ScanSettings(threshold=-1.0))
    assert result.candidates == ()


# --- symmetry-aware rotations ---------------------------------------------


def duplex_symbol() -> np.ndarray:
    """Symmetric under 180 degrees and under a horizontal flip; not square."""
    s = np.full((36, 24), 255, np.uint8)
    cv2.circle(s, (12, 18), 9, 0, 2)
    cv2.line(s, (8, 14), (8, 22), 0, 2)
    cv2.line(s, (12, 0), (12, 8), 0, 2)
    s = np.minimum(s, cv2.flip(s, 1))  # force exact symmetry
    return np.minimum(s, cv2.flip(s, 0))


def test_symmetric_template_skips_equivalent_rotations(detector):
    sym = duplex_symbol()
    page = blank_page()
    place(page, sym, 30, 30)
    place(page, rotate(sym, 90), 150, 40)
    place(page, rotate(sym, 180), 250, 200)
    place(page, rotate(sym, 270), 40, 200)
    result = detector.detect(page, sym)
    skipped = {(s.rotation, s.mirrored): (s.reported_rotation, s.reported_mirrored)
               for s in result.skipped_orientations}
    assert skipped == {(180, False): (0, False), (270, False): (90, False)}
    assert all(s.similarity >= 0.97 for s in result.skipped_orientations)
    assert sum("not searched" in w for w in result.warnings) == 2
    assert result.rotations_searched == (0, 90, 180, 270)
    # 180 and 270 instances are reported with the canonical label.
    assert found(result) == [
        (30, 30, 24, 36, 0, False),
        (40, 200, 36, 24, 90, False),
        (150, 40, 36, 24, 90, False),
        (250, 200, 24, 36, 0, False),
    ]
    assert all(c.score == pytest.approx(1.0, abs=1e-3) for c in result.candidates)
    d = json.loads(json.dumps(result.to_dict()))
    assert d["skipped_orientations"][0]["reported_as"] == {"rotation": 0, "mirrored": False}


def test_square_symmetric_template_searches_one_rotation(detector):
    sym = np.full((20, 20), 255, dtype=np.uint8)
    cv2.circle(sym, (10, 10), 7, 0, 2)
    for r in (90, 180, 270):  # force exact quarter-turn symmetry
        sym = np.minimum(sym, rotate(sym, r))
    page = blank_page()
    place(page, sym, 50, 60)
    result = detector.detect(page, sym)
    assert {s.rotation for s in result.skipped_orientations} == {90, 180, 270}
    assert {s.reported_rotation for s in result.skipped_orientations} == {0}
    assert found(result) == [(50, 60, 20, 20, 0, False)]


def test_canonical_label_is_smallest_requested_rotation(detector):
    sym = duplex_symbol()
    page = blank_page()
    place(page, sym, 30, 30)
    result = detector.detect(page, sym, ScanSettings(rotations=(180, 90, 270)))
    assert [(s.rotation, s.reported_rotation) for s in result.skipped_orientations] == [(270, 90)]
    assert found(result) == [(30, 30, 24, 36, 180, False)]


def test_asymmetric_template_searches_everything(detector, glyph):
    page = blank_page()
    place(page, glyph, 30, 30)
    result = detector.detect(page, glyph, ScanSettings(include_mirrored=True))
    assert result.skipped_orientations == ()
    assert not any("not searched" in w for w in result.warnings)


# --- mirrored symbols ------------------------------------------------------


def test_mirrored_off_by_default(detector, glyph):
    page = blank_page()
    place(page, glyph, 10, 20)
    result = detector.detect(page, glyph)
    assert all(not c.mirrored for c in result.candidates)
    d = result.to_dict()
    assert d["candidates"][0]["mirrored"] is False
    assert d["include_mirrored"] is False


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_mirrored_symbol_found_with_convention(detector, glyph, rotation):
    """mirrored=True, rotation=r means: flip the template left-right, then
    rotate it r degrees clockwise."""
    page = blank_page()
    place(page, glyph, 30, 40)
    place(page, mirror_then_rotate(glyph, rotation), 200, 150)
    result = detector.detect(page, glyph, ScanSettings(include_mirrored=True))
    w, h = (GLYPH_H, GLYPH_W) if rotation in (90, 270) else (GLYPH_W, GLYPH_H)
    assert found(result) == [
        (30, 40, GLYPH_W, GLYPH_H, 0, False),
        (200, 150, w, h, rotation, True),
    ]
    assert all(c.score == pytest.approx(1.0, abs=1e-3) for c in result.candidates)
    assert result.include_mirrored


def test_mirror_symmetric_template_skips_mirrored_orientations(detector):
    sym = duplex_symbol()
    page = blank_page()
    place(page, sym, 30, 30)
    result = detector.detect(page, sym, ScanSettings(include_mirrored=True))
    skipped = {(s.rotation, s.mirrored) for s in result.skipped_orientations}
    assert skipped == {(180, False), (270, False), (0, True), (90, True), (180, True), (270, True)}
    assert found(result) == [(30, 30, 24, 36, 0, False)]


# --- settings -------------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        ScanSettings(blur_sigma=-1),
        ScanSettings(blur_sigma=float("nan")),
        ScanSettings(blur_sigma=50),
        ScanSettings(include_mirrored=1),
        ScanSettings(coarse_to_fine="yes"),
        ScanSettings(coarse_slack=1.5),
        ScanSettings(max_coarse_peaks=0),
        ScanSettings(num_threads=-1),
        ScanSettings(num_threads=2.0),
        ScanSettings(num_threads=True),
    ],
)
def test_invalid_new_settings_rejected(detector, glyph, settings):
    with pytest.raises(DetectionError) as info:
        detector.detect(blank_page(), glyph, settings)
    assert info.value.code == "invalid_settings"


def test_settings_to_dict_is_json_and_complete():
    s = ScanSettings(search_region=BoundingBox(1, 2, 30, 40), include_mirrored=True)
    d = json.loads(json.dumps(s.to_dict()))
    assert d["search_region"] == {"x": 1, "y": 2, "width": 30, "height": 40}
    assert d["blur_sigma"] == 1.0 and d["include_mirrored"] is True
    assert d["coarse_to_fine"] is True and d["num_threads"] == 0
    import dataclasses

    assert set(d) == {f.name for f in dataclasses.fields(ScanSettings)}
