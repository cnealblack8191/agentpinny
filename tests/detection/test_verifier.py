"""Verifiers: features, kNN vote, §6 crop geometry, reranking, ONNX guard."""

from __future__ import annotations

import sys

import cv2
import numpy as np
import pytest

from pinny.detection import BoundingBox, Candidate, DetectionError, OpenCVTemplateDetector, ScanSettings
from pinny.detection.verifier import (
    CROP_MARGIN_PX,
    KnnVerifier,
    OnnxEmbeddingVerifier,
    Verifier,
    crop_with_margin,
    hog_intensity_features,
    margin_box,
)


def receptacle() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (8, 16), (8, 24), 0, 2)
    cv2.line(g, (16, 16), (16, 24), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def lookalike() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (6, 20), (18, 20), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def make_page(seed: int, n_each: int = 4):
    """RGB page with receptacles and look-alikes at random quarter turns plus
    light noise. Returns (page, real_boxes, fake_boxes)."""
    rng = np.random.default_rng(seed)
    page = np.full((260, 520), 255, dtype=np.uint8)
    real, fake = [], []
    slots = [(40 + 90 * i, 30 + 110 * j) for j in range(2) for i in range(5)]
    order = rng.permutation(len(slots))[: 2 * n_each]
    for k, si in enumerate(order):
        x, y = slots[si]
        glyph = receptacle() if k % 2 == 0 else lookalike()
        glyph = np.rot90(glyph, -int(rng.integers(0, 4))).copy()
        h, w = glyph.shape
        page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], glyph)
        (real if k % 2 == 0 else fake).append(BoundingBox(x, y, w, h))
    noise = rng.integers(-12, 13, page.shape)
    page = np.clip(page.astype(int) + noise, 0, 255).astype(np.uint8)
    return cv2.cvtColor(page, cv2.COLOR_GRAY2RGB), real, fake


def key(b):
    return (b.x, b.y)


def test_margin_box_clips_to_page():
    assert margin_box(BoundingBox(10, 5, 20, 30), (100, 200)) == (0, 0, 54, 59)
    assert margin_box(BoundingBox(180, 80, 20, 20), (100, 200, 3)) == (156, 56, 200, 100)
    page = np.zeros((100, 200, 3), np.uint8)
    assert crop_with_margin(page, BoundingBox(50, 40, 10, 10)).shape == (10 + 2 * CROP_MARGIN_PX, 58, 3)


def test_features_are_unit_length_and_discriminative():
    a, b = hog_intensity_features(receptacle()), hog_intensity_features(lookalike())
    assert a.ndim == 1 and np.linalg.norm(a) == pytest.approx(1.0)
    assert a @ a == pytest.approx(1.0) and a @ b < 0.99
    rgb = cv2.cvtColor(receptacle(), cv2.COLOR_GRAY2RGB)
    assert np.allclose(hog_intensity_features(rgb), a)


def test_score_requires_fit_and_examples():
    v = KnnVerifier()
    assert isinstance(v, Verifier)
    with pytest.raises(DetectionError) as e:
        v.score([receptacle()])
    assert e.value.code == "verifier_not_fitted"
    with pytest.raises(DetectionError):
        v.fit([], [])


def test_vote_with_laplace_smoothing_on_exact_neighbours():
    v = KnnVerifier(k=1, laplace=1.0, augment_rotations=False).fit([receptacle()], [lookalike()])
    p = v.score([receptacle(), lookalike()])
    # One neighbour with similarity 1: (1 + 1) / (1 + 2) and (0 + 1) / (1 + 2).
    assert p == pytest.approx([2 / 3, 1 / 3])
    assert v.score([]).shape == (0,)


def test_verifier_separates_lookalikes_after_few_examples():
    det = OpenCVTemplateDetector()
    template = cv2.cvtColor(receptacle(), cv2.COLOR_GRAY2RGB)
    settings = ScanSettings(threshold=0.6)

    train_page, train_real, train_fake = make_page(1)
    pos = [crop_with_margin(train_page, b) for b in train_real[:3]]
    neg = [crop_with_margin(train_page, b) for b in train_fake[:3]]
    verifier = KnnVerifier().fit(pos, neg)

    test_page, real, fake = make_page(2)
    cands = det.detect(test_page, template, settings).candidates
    by_key = {key(c.box): c for c in cands}
    # The matcher alone cannot separate them at this threshold...
    assert all(key(b) in by_key for b in real + fake)
    assert min(by_key[key(b)].score for b in fake) > 0.6

    ranked = verifier.rerank(test_page, [by_key[key(b)] for b in real + fake])
    p = {key(c.box): prob for c, prob in ranked}
    assert min(p[key(b)] for b in real) > 0.5 > max(p[key(b)] for b in fake)
    # ...and reranking puts every real symbol first.
    assert {key(c.box) for c, _ in ranked[: len(real)]} == {key(b) for b in real}


def test_rerank_is_deterministic_and_handles_empty():
    v = KnnVerifier().fit([receptacle()], [lookalike()])
    page = np.full((100, 100), 255, np.uint8)
    cands = [Candidate(0.9, BoundingBox(10, 10, 20, 20), 0), Candidate(0.8, BoundingBox(50, 50, 20, 20), 0)]
    # Identical blank crops -> equal p; order falls back to raw score.
    ranked = v.rerank(page, cands)
    assert [c.score for c, _ in ranked] == [0.9, 0.8]
    assert v.rerank(page, []) == []


def test_onnx_verifier_missing_onnxruntime_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(DetectionError) as e:
        OnnxEmbeddingVerifier(tmp_path / "model.onnx")
    assert e.value.code == "onnxruntime_missing"
    assert "pip install onnxruntime" in str(e.value)


def test_onnx_verifier_missing_model_file(tmp_path):
    pytest.importorskip("onnxruntime")
    with pytest.raises(DetectionError) as e:
        OnnxEmbeddingVerifier(tmp_path / "absent.onnx")
    assert e.value.code == "model_not_found"


def test_onnx_verifier_with_tiny_model(tmp_path):
    pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    # Embedding = 8x8 average-pooled image, flattened (N, 3*8*8).
    graph = helper.make_graph(
        [
            helper.make_node("AveragePool", ["x"], ["p"], kernel_shape=[4, 4], strides=[4, 4]),
            helper.make_node("Flatten", ["p"], ["y"], axis=1),
        ],
        "tiny",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [None, 3, 32, 32])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [None, 192])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    # A newer onnx writes an IR version the pinned onnxruntime may not load yet;
    # opset 13 needs only IR 7.
    model.ir_version = 7
    path = tmp_path / "tiny.onnx"
    onnx.save(model, str(path))
    v = OnnxEmbeddingVerifier(path, input_size=32, k=1, laplace=1.0, augment_rotations=False)
    v.fit([receptacle()], [lookalike()])
    p = v.score([receptacle(), lookalike()])
    assert p[0] > 0.5 > p[1]
