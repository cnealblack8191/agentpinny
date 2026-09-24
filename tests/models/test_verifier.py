"""Verifier inference (P6): dataset-identical crops, batching, speed."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from pinny.models.verifier import crop_origin, crop_patch, to_gray
from tests.training.test_verifier_fixture import build_verifier_dataset, reference_crop


def test_crop_matches_reference_everywhere():
    rng = np.random.default_rng(0)
    gray = rng.integers(0, 256, (130, 170), dtype=np.uint8)
    pts = [(0.0, 0.0), (170.0, 130.0), (-60.0, 20.0), (85.5, 64.5), (84.49, 64.51), (47.5, 48.0),
           (169.9, 0.1), (-140.0, 270.0)] + [tuple(p) for p in rng.uniform(-50, 220, (50, 2))]
    for x, y in pts:
        assert np.array_equal(crop_patch(gray, x, y), reference_crop(gray, x, y)), (x, y)
    assert (crop_patch(gray, 1e4, -1e4) == 255).all()


def test_crop_centring_convention():
    assert crop_origin(100.0, 100.0) == (52, 52)
    assert crop_origin(100.49, 99.5) == (52, 52)
    with pytest.raises(Exception):
        crop_origin(float("nan"), 0.0)


def test_to_gray_accepts_contract_layouts():
    rgb = np.random.default_rng(1).integers(0, 256, (10, 12, 3), dtype=np.uint8)
    g = to_gray(rgb)
    assert np.array_equal(g, cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    assert np.array_equal(to_gray(np.dstack([rgb, np.full((10, 12), 7, np.uint8)])), g)
    assert np.array_equal(to_gray(g), g) and np.array_equal(to_gray(g[:, :, None]), g)



@pytest.fixture
def trained(tmp_path, render_service):
    pytest.importorskip("torch")
    from pinny.training.verifier_train import TrainConfig, train_verifier

    root, pages = build_verifier_dataset(tmp_path / "ds", render_service)
    out = train_verifier(root, tmp_path / "models", TrainConfig(epochs=1, batch_size=16, max_steps=2))
    return root, pages, out


def test_score_uses_exactly_the_dataset_crops(trained):
    from pinny.models.verifier import Verifier

    root, pages, out = trained
    v = Verifier.load(out)
    for rgb, samples in pages.values():
        pts = [(s["x"], s["y"]) for s in samples]
        stored = np.stack([cv2.imread(str(root / s["path"]), cv2.IMREAD_UNCHANGED) for s in samples])
        assert np.array_equal(v.crops(rgb, pts), stored)
        # score() on the page equals the network on the stored crops, in any batch size.
        from pinny.models.verifier import predict_proba
        scores = v.score(rgb, pts)
        assert np.allclose(scores, predict_proba(v._net, stored, batch_size=5), atol=1e-6)
        assert all(0.0 <= s <= 1.0 for s in scores) and len(scores) == len(pts)
    assert v.score(rgb, []) == []


def test_scores_500_points_on_a_full_page_under_5s(tmp_path):
    torch = pytest.importorskip("torch")
    from pinny.models.artifact import save_artifact
    from pinny.models.verifier import Verifier, build_network

    torch.manual_seed(0)
    out = save_artifact(tmp_path, kind="verifier", arch="verifier-cnn-v1",
                        state_dict=build_network().state_dict(),
                        metadata={"input": {"channels": 1, "crop_px": 96},
                                  "operating_point": {"threshold": 0.5, "chosen_on": "val"}})
    v = Verifier.load(out)
    page = np.full((2200, 3400, 3), 255, np.uint8)
    rng = np.random.default_rng(0)
    pts = [tuple(p) for p in rng.uniform(0, [3400, 2200], (500, 2))]
    v.score(page, pts[:8])  # warm-up
    t = time.perf_counter()
    scores = v.score(page, pts)
    elapsed = time.perf_counter() - t
    assert len(scores) == 500
    assert elapsed < 5.0, f"{elapsed:.2f}s"
