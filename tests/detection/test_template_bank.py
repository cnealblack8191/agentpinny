"""Template bank: clustering, sizing and recovering a missed variant."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from pinny.detection import BoundingBox, DetectionError, OpenCVTemplateDetector, ScanSettings
from pinny.detection.multi import detect_multi
from pinny.detection.template_bank import (
    NegativeBank,
    build_template_bank,
    fit_to_size,
    k_medoids,
    ncc,
    trim_margin,
)


def receptacle(scale: float = 1.0, thickness: int = 2) -> np.ndarray:
    s = lambda v: int(round(v * scale))  # noqa: E731
    g = np.full((s(36), s(24)), 255, dtype=np.uint8)
    cv2.circle(g, (s(12), s(20)), s(9), 0, thickness)
    cv2.line(g, (s(8), s(16)), (s(8), s(24)), 0, thickness)
    cv2.line(g, (s(16), s(16)), (s(16), s(24)), 0, thickness)
    cv2.line(g, (s(12), 0), (s(12), s(10)), 0, thickness)
    return g


def lookalike() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (6, 20), (18, 20), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def place(page: np.ndarray, img: np.ndarray, x: int, y: int) -> BoundingBox:
    h, w = img.shape[:2]
    page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], img)
    return BoundingBox(x, y, w, h)


def test_fit_to_size_crops_and_pads_with_background():
    img = np.full((10, 6), 200, dtype=np.uint8)
    img[4:6, :] = 0
    out = fit_to_size(img, (8, 10))
    assert out.shape == (8, 10)
    assert out[0, 0] == 200 and out[:, 0].max() == 200  # padded with border median
    assert (out[3:5, 2:8] == 0).all()  # centre rows kept


def test_ncc_identity_and_flat():
    g = receptacle()
    assert ncc(g, g) == pytest.approx(1.0)
    assert ncc(g, np.full_like(g, 255)) == 0.0


def test_trim_margin():
    crop = np.zeros((36 + 48, 24 + 48), dtype=np.uint8)
    assert trim_margin(crop).shape == (36, 24)
    with pytest.raises(DetectionError):
        trim_margin(np.zeros((40, 40), np.uint8), 24)


def test_k_medoids_two_obvious_clusters_is_deterministic():
    pts = np.array([0.0, 0.1, 0.2, 10.0, 10.1, 10.3])
    dist = np.abs(pts[:, None] - pts[None, :])
    medoids, labels = k_medoids(dist, 2)
    assert sorted(medoids) == [1, 4]
    assert list(labels) == [1, 1, 1, 4, 4, 4]
    again, labels_again = k_medoids(dist, 2)
    assert again == medoids and (labels_again == labels).all()


def test_bank_collapses_duplicates_and_separates_variant():
    orig = receptacle()
    variant = receptacle(1.25, 3)
    positives = [orig.copy(), orig.copy(), variant, variant.copy(), variant.copy()]
    bank = build_template_bank(positives, max_templates=4, min_similarity=0.9, original=orig)
    assert len(bank) == 2
    assert bank[0].is_original and bank[0].support == 3
    assert bank[1].support == 3 and bank[1].image.shape == variant.shape  # native size
    assert all(b.min_member_similarity >= 0.9 for b in bank)
    # All inputs are accounted for exactly once.
    members = sorted(i for b in bank for i in b.member_indices)
    assert members == list(range(6))


def test_bank_respects_max_templates_and_empty_input():
    crops = [receptacle(), lookalike(), receptacle(1.25, 3), np.rot90(receptacle()).copy()]
    bank = build_template_bank(crops, max_templates=2, min_similarity=0.99)
    assert len(bank) == 2
    assert sum(b.support for b in bank) == 4
    assert build_template_bank([]) == []
    with pytest.raises(DetectionError):
        build_template_bank(crops, max_templates=0)


def test_bank_uses_most_common_size_without_original():
    crops = [receptacle(), receptacle(), receptacle(1.25, 3)]
    bank = build_template_bank(crops, max_templates=1)
    assert len(bank) == 1 and bank[0].image.shape == (36, 24)


def test_bank_recovers_scaled_heavier_variant_missed_by_single_template():
    orig = receptacle()
    variant = receptacle(1.25, 3)
    page = np.full((300, 420), 255, dtype=np.uint8)
    orig_boxes = [place(page, orig, x, 20) for x in (20, 120)]
    var_boxes = [place(page, variant, x, 150) for x in (20, 140, 260)]
    rgb = cv2.cvtColor(page, cv2.COLOR_GRAY2RGB)
    det = OpenCVTemplateDetector()
    settings = ScanSettings(rotations=(0,), threshold=0.8)

    single = det.detect(rgb, cv2.cvtColor(orig, cv2.COLOR_GRAY2RGB), settings)
    assert {(c.box.x, c.box.y) for c in single.candidates} == {(b.x, b.y) for b in orig_boxes}

    # The reviewer added one variant pin; its tight crop joins the bank.
    added = [page[b.y : b.y2, b.x : b.x2].copy() for b in var_boxes[:1]]
    bank = build_template_bank(added, original=orig)
    assert len(bank) == 2
    multi = detect_multi(det, rgb, bank, settings)
    found = {(c.box.x, c.box.y): ti for c, ti in multi.pairs()}
    assert set(found) == {(b.x, b.y) for b in orig_boxes + var_boxes}
    assert {found[(b.x, b.y)] for b in orig_boxes} == {0}
    assert {found[(b.x, b.y)] for b in var_boxes} == {1}


def test_negative_bank_similarity_is_rotation_aware():
    neg = NegativeBank.from_crops([lookalike()])
    assert len(neg) == 1
    assert neg.max_similarity(np.rot90(lookalike()).copy()) == pytest.approx(1.0)
    assert neg.max_similarity(receptacle()) < 0.9
    assert NegativeBank().max_similarity(receptacle()) == -1.0


def test_negative_bank_compacts_to_max_items():
    crops = [receptacle()] * 5 + [lookalike()] * 5
    neg = NegativeBank.from_crops(crops, max_items=2)
    assert len(neg) == 2
    assert neg.max_similarity(receptacle()) == pytest.approx(1.0)
    assert neg.max_similarity(lookalike()) == pytest.approx(1.0)
