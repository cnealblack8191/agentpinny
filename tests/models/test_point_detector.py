"""Point detector inference: tiling, tile merging, peak picking, artifact round trip."""

from __future__ import annotations

import importlib.util
import json
import re
import time

import numpy as np
import pytest

from pinny.errors import PinnyError
from pinny.models.point_detector import (
    OUT_STRIDE, STRIDE_PX, TILE_PX, PointDetector, pick_peaks, run_tiled, tile_origins, to_gray)

needs_torch = pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch not installed")

EDGE_CELLS = 16


def fake_infer(batch: np.ndarray):
    """A translation-equivariant stand-in for the net, with worse output near
    tile borders (like a real net with less context there): heat is the max
    ink in each 4 x 4 cell, halved near the border, and the offset is the ink
    centroid inside the cell, but deliberately wrong (0) near the border. Merging
    must therefore take both from the tile where the point is interior."""
    n, _, t, _ = batch.shape
    r = t // OUT_STRIDE
    cells = batch[:, 0].reshape(n, r, OUT_STRIDE, r, OUT_STRIDE)
    heat = cells.max(axis=(2, 4))
    mass = cells.sum(axis=(2, 4))
    ax = np.arange(OUT_STRIDE, dtype=np.float32) + 0.5
    with np.errstate(invalid="ignore", divide="ignore"):
        ox = np.where(mass > 0, (cells.sum(axis=2) * ax).sum(axis=-1) / mass / OUT_STRIDE, 0)
        oy = np.where(mass > 0, (cells.sum(axis=4) * ax[:, None]).sum(axis=2) / mass / OUT_STRIDE, 0)
    idx = np.arange(r)
    edge = (np.minimum(idx, r - 1 - idx) < EDGE_CELLS)
    edge2 = edge[:, None] | edge[None, :]
    heat = np.where(edge2, heat * 0.5, heat).astype(np.float32)
    off = np.stack([np.where(edge2, 0, ox), np.where(edge2, 0, oy)], axis=1).astype(np.float32)
    return heat, off


def test_tile_origins_cover_and_align():
    assert tile_origins(100) == [0]
    assert tile_origins(512) == [0]
    assert tile_origins(513) == [0, 384]
    for length in (513, 896, 897, 2200, 3400):
        o = tile_origins(length)
        assert o[-1] + TILE_PX >= length and (len(o) == 1 or o[-2] + TILE_PX < length)
        assert all(x % STRIDE_PX == 0 and x % OUT_STRIDE == 0 for x in o)


@pytest.mark.parametrize("x,y", [
    (385.0, 202.0),   # just inside the second tile's left edge
    (511.0, 202.0),   # just inside the first tile's right edge
    (510.0, 386.0),   # where four tiles overlap
    (202.0, 385.0),
    (1002.0, 602.0),
])
def test_point_on_tile_seam_is_found_once_in_the_right_place(x, y):
    gray = np.full((1100, 1300), 255, np.uint8)
    # a 2 x 2 dark block whose centroid is exactly (x, y); x, y are not multiples
    # of 4, so the block lies inside one output cell
    gray[int(y) - 1:int(y) + 1, int(x) - 1:int(x) + 1] = 0
    heat, off = run_tiled(gray, fake_infer)
    assert heat.shape == (275, 325) and off.shape == (2, 275, 325)
    pts = pick_peaks(heat, off, threshold=0.5, width=1300, height=1100)
    assert len(pts) == 1
    assert pts[0]["x"] == pytest.approx(x, abs=1e-4) and pts[0]["y"] == pytest.approx(y, abs=1e-4)
    assert pts[0]["score"] == pytest.approx(1.0)


def test_page_smaller_than_a_tile_and_odd_sizes():
    gray = np.full((301, 203), 255, np.uint8)
    gray[149:151, 101:103] = 0
    heat, off = run_tiled(gray, fake_infer)
    assert heat.shape == (76, 51)
    # a point in the only tile's border band still comes out (at half score)
    pts = pick_peaks(heat, off, threshold=0.4, width=203, height=301)
    assert len(pts) == 1 and pts[0]["x"] == pytest.approx(102.0) and pts[0]["y"] == pytest.approx(150.0)


def test_pick_peaks_nms_threshold_order_and_limits():
    heat = np.zeros((50, 50), np.float32)
    off = np.full((2, 50, 50), 0.5, np.float32)
    heat[10, 10] = 0.9   # (42, 42)
    heat[10, 12] = 0.8   # 8 px away: suppressed by the 10 px NMS
    heat[20, 10] = 0.7   # (42, 82): 40 px away, kept
    heat[20, 13] = 0.75  # 12 px from it: kept
    heat[40, 40] = 0.2   # below threshold
    pts = pick_peaks(heat, off, threshold=0.5, width=200, height=200)
    assert [(p["x"], p["y"]) for p in pts] == [(42.0, 42.0), (54.0, 82.0), (42.0, 82.0)]
    assert [p["score"] for p in pts] == sorted((p["score"] for p in pts), reverse=True)
    assert len(pick_peaks(heat, off, threshold=0.5, width=200, height=200, max_points=2)) == 2
    assert pick_peaks(heat, off, threshold=0.5, width=200, height=200, max_points=0) == []
    assert pick_peaks(heat, off, threshold=0.95, width=200, height=200) == []
    # a plateau of equal cells gives one point, and points are clipped to the page
    heat2 = np.zeros((10, 10), np.float32)
    heat2[9, 8:10] = 0.9
    off2 = np.ones((2, 10, 10), np.float32)
    p2 = pick_peaks(heat2, off2, threshold=0.5, width=38, height=38)
    assert len(p2) == 1 and p2[0]["x"] <= 38 and p2[0]["y"] == 38


def test_to_gray_accepts_channels_and_rejects_bad_input():
    rgb = np.zeros((4, 5, 3), np.uint8)
    rgb[..., 0] = 255
    assert to_gray(rgb).shape == (4, 5)
    assert to_gray(np.zeros((4, 5, 4), np.uint8)).shape == (4, 5)
    assert to_gray(np.zeros((4, 5, 1), np.uint8)).shape == (4, 5)
    with pytest.raises(PinnyError):
        to_gray(np.zeros((4, 5), np.float32))
    with pytest.raises(PinnyError):
        to_gray(np.zeros((4, 5, 2), np.uint8))


# ------------------------------------------------------------ torch needed


def _seeded_net():
    import torch

    from pinny.models.point_detector import build_net

    torch.manual_seed(0)
    net = build_net()
    for m in net.modules():  # non-trivial BN stats so the round trip checks them too
        if isinstance(m, torch.nn.BatchNorm2d):
            m.running_mean.uniform_(-0.1, 0.1)
            m.running_var.uniform_(0.5, 1.5)
    return net


def _save(net, tmp_path, threshold=0.4, created_at=None):
    from pinny.models.artifact import save_model
    from pinny.models.point_detector import ARCH

    return save_model(tmp_path / "models", kind="detector", arch=ARCH, state_dict=net.state_dict(),
                      input={"channels": 1, "tile_px": 512, "stride_px": 384, "dpi": 200},
                      dataset_id="ds", synthetic_only=True, train_config={"epochs": 0},
                      operating_point={"threshold": threshold, "chosen_on": "val"},
                      metrics={"val": {}, "test": {}}, created_at=created_at)


@needs_torch
def test_save_and_load_round_trip(tmp_path):
    net = _seeded_net()
    model_dir = _save(net, tmp_path)
    meta = json.loads((model_dir / "model.json").read_text())
    assert re.fullmatch(r"detector-\d{8}T\d{6}Z-[0-9a-f]{8}", meta["model_id"])
    assert meta["model_id"].endswith(meta["weights_sha256"][:8]) and model_dir.name == meta["model_id"]
    assert meta["format"] == "pinny.model" and meta["format_version"] == 1
    loaded = PointDetector.load(model_dir)
    assert loaded.model_id == meta["model_id"] and loaded.threshold == 0.4
    rng = np.random.default_rng(1)
    page = np.full((700, 900, 3), 255, np.uint8)
    page[rng.random((700, 900)) < 0.01] = 0
    a = PointDetector(net, model_id="x", threshold=0.4).heatmap(page)
    b = loaded.heatmap(page)
    np.testing.assert_array_equal(a[0], b[0])
    np.testing.assert_array_equal(a[1], b[1])


@needs_torch
def test_load_refuses_corrupt_weights_wrong_kind_and_overwrite(tmp_path):
    import datetime as dt

    from pinny.models.artifact import load_model

    when = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
    model_dir = _save(_seeded_net(), tmp_path, created_at=when)
    with pytest.raises(PinnyError) as e:
        _save(_seeded_net(), tmp_path, created_at=when)
    assert e.value.code == "model_exists"
    with pytest.raises(PinnyError) as e:
        load_model(model_dir, kind="verifier")
    assert e.value.code == "model_kind_mismatch"
    w = model_dir / "weights.pt"
    data = bytearray(w.read_bytes())
    data[len(data) // 2] ^= 0xFF
    w.write_bytes(bytes(data))
    with pytest.raises(PinnyError) as e:
        PointDetector.load(model_dir)
    assert e.value.code == "model_weights_corrupt"


@needs_torch
def test_full_page_under_30_seconds_on_cpu():
    det = PointDetector(_seeded_net(), model_id="x", threshold=0.5)
    page = np.full((2200, 3400, 3), 255, np.uint8)
    t = time.monotonic()
    pts = det.detect_points(page, threshold=0.0, max_points=500)
    assert time.monotonic() - t < 30
    assert len(pts) <= 500
    assert all(0 <= p["x"] <= 3400 and 0 <= p["y"] <= 2200 for p in pts)
