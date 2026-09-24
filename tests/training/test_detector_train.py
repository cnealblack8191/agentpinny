"""Point detector training: targets, augmentation, P9 matching, loss, artifact, CLI."""

from __future__ import annotations

import importlib.util
import json

import numpy as np
import pytest

from pinny.errors import PinnyError
from pinny.training.detector_train import (
    TrainConfig, augment_tile, choose_threshold, load_manifest, make_targets, match_count, page_tiles, score_at)
from tests.training.test_detector_fixtures import write_dataset

needs_torch = pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch not installed")


def test_targets_have_unit_peak_and_subcell_offset():
    heat, off, mask = make_targets(np.array([[101.0, 42.0], [511.9, 0.5]], np.float32))
    assert heat.shape == (128, 128) and off.shape == (2, 128, 128)
    assert heat[10, 25] == 1.0 and mask[10, 25] == 1.0
    assert off[:, 10, 25] == pytest.approx([0.25, 0.5])
    assert heat[0, 127] == 1.0 and mask.sum() == 2
    assert heat[10, 25 + 4] == pytest.approx(np.exp(-0.5), rel=1e-5)  # sigma = 4 cells
    assert (heat == 1.0).sum() == 2


@pytest.mark.parametrize("seed", range(6))
def test_augmentation_moves_points_with_the_ink(seed):
    gray = np.full((900, 900), 255, np.uint8)
    pts = np.array([[500.5, 450.5], [620.5, 380.5]], np.float32)
    for x, y in pts:
        gray[int(y) - 2:int(y) + 3, int(x) - 2:int(x) + 3] = 0
    cfg = TrainConfig()
    x, out = augment_tile(gray, pts, (384, 0), np.random.default_rng(seed), cfg.augment)
    assert x.shape == (512, 512) and x.dtype == np.float32 and 0 <= x.min() and x.max() <= 1
    assert len(out) == 2
    for px, py in out:
        r, c = int(py), int(px)
        assert x[r - 1:r + 1, c - 1:c + 1].min() > 0.3, (seed, px, py)


def test_tiles_split_by_points_and_manifest_loads(tmp_path):
    root = write_dataset(tmp_path / "ds", splits={"train": 1, "val": 1}, size=(900, 600), n_points=1)
    manifest, splits = load_manifest(root)
    assert manifest["source"]["synthetic"] and len(splits["train"]) == 1 and splits["test"] == []
    pos, neg = page_tiles(splits["train"][0])
    assert len(pos) + len(neg) == 3 * 2 and len(pos) >= 1  # x origins 0, 384, 768
    with pytest.raises(PinnyError):
        load_manifest(tmp_path / "missing")


def test_match_count_is_one_to_one_within_tolerance():
    assert match_count([], [(0, 0)]) == 0
    assert match_count([(0, 0), (1, 0)], [(0, 0)]) == 1         # a duplicate earns nothing
    assert match_count([(0, 0)], [(12, 0)]) == 1                # tolerance is inclusive
    assert match_count([(0, 0)], [(12.01, 0)]) == 0
    # greedy nearest would match p0-r1 and lose r0; the maximum matching gets both
    assert match_count([(5, 0), (15, 0)], [(0, 0), (8, 0)]) == 2


def test_threshold_is_chosen_by_f1_and_scored_with_the_p9_rule():
    class P:
        points = np.array([[0.0, 0.0], [100.0, 0.0]])

    cands = [[{"x": 1.0, "y": 0.0, "score": 0.9}, {"x": 99.0, "y": 0.0, "score": 0.6},
              {"x": 300.0, "y": 0.0, "score": 0.3}]]
    best = choose_threshold(cands, [P()], grid=[0.2, 0.5, 0.8])
    assert best["threshold"] == 0.5 and (best["tp"], best["fp"], best["fn"]) == (2, 0, 0)
    s = score_at(cands, [P()], 0.8)
    assert (s["tp"], s["fp"], s["fn"]) == (1, 0, 1) and s["recall"] == 0.5


@needs_torch
def test_a_few_training_steps_reduce_the_loss(tmp_path):
    import torch

    from pinny.models.point_detector import build_net
    from pinny.training.detector_train import detector_loss, iter_batches

    root = write_dataset(tmp_path / "ds", splits={"train": 2}, size=(640, 560), n_points=4)
    _, splits = load_manifest(root)
    torch.manual_seed(0)
    cfg = TrainConfig(tiles_per_epoch=4, batch_size=4)
    x, h, o, m = next(iter_batches(splits["train"], cfg, np.random.default_rng(0)))
    assert m.sum() > 0
    net = build_net().train()
    opt = torch.optim.Adam(net.parameters(), lr=2e-3)
    losses = []
    for _ in range(8):
        loss, _, _ = detector_loss(*net(x), h, o, m)
        opt.zero_grad()
        loss.backward()
        opt.step()
        losses.append(loss.item())
    assert losses[-1] < 0.5 * losses[0], losses


@needs_torch
def test_train_detector_cli_writes_an_artifact_with_a_val_threshold(tmp_path, capsys):
    from pinny.models.point_detector import PointDetector
    from pinny.training.__main__ import main

    root = write_dataset(tmp_path / "ds", splits={"train": 2, "val": 1, "test": 1}, size=(600, 520))
    rc = main(["train-detector", "--dataset", str(root), "--epochs", "1", "--tiles-per-epoch", "8",
               "--batch-size", "4", "--models-dir", str(tmp_path / "models")])
    assert rc == 0
    printed = capsys.readouterr().out
    summary = json.loads(printed[printed.rindex("\n{"):])
    dirs = list((tmp_path / "models").iterdir())
    assert len(dirs) == 1
    meta = json.loads((dirs[0] / "model.json").read_text())
    assert meta["kind"] == "detector" and meta["arch"] == "detector-centernet-v1"
    assert meta["synthetic_only"] is True and meta["dataset_id"] == "synthetic-test-0"
    assert meta["operating_point"]["chosen_on"] == "val"
    assert meta["train_config"]["epochs"] == 1 and meta["train_config"]["seed"] == 0
    for split in ("val", "test"):
        m = meta["metrics"][split]
        assert m["split"] == split and m["tolerance_px"] == 12.0 and m["pages"] == 1
        assert m["threshold"] == meta["operating_point"]["threshold"]
        assert "synthetic" in m["note"]
    det = PointDetector.load(dirs[0])
    assert det.threshold == meta["operating_point"]["threshold"]
    assert summary["model_id"] == meta["model_id"] == det.model_id


def test_cli_rejects_unknown_command(capsys):
    from pinny.training.__main__ import main

    assert main(["no-such-command"]) == 2
