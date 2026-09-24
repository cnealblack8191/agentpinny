"""Verifier trainer: a few steps on the tiny fixture, then save/load (P4, P5)."""

from __future__ import annotations

import json

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from pinny.models.artifact import load_artifact, read_metadata  # noqa: E402
from pinny.training import verifier_train as vt  # noqa: E402
from pinny.training.__main__ import main as cli_main  # noqa: E402
from tests.training.test_verifier_fixture import build_verifier_dataset  # noqa: E402

FAST = dict(epochs=2, batch_size=16, max_steps=4)


@pytest.fixture
def dataset(tmp_path, render_service):
    root, _ = build_verifier_dataset(tmp_path / "ds", render_service)
    return root


def test_train_writes_a_complete_artifact(dataset, tmp_path):
    out = vt.train_verifier(dataset, tmp_path / "models", vt.TrainConfig(**FAST))
    assert sorted(p.name for p in out.iterdir()) == ["model.json", "weights.pt"]
    meta = read_metadata(out)
    manifest = json.loads((dataset / "manifest.json").read_text())
    assert meta["kind"] == "verifier" and meta["arch"] == "verifier-cnn-v1"
    assert meta["model_id"].startswith("verifier-") and meta["model_id"].endswith(meta["weights_sha256"][:8])
    assert meta["dataset_id"] == manifest["dataset_id"] and meta["synthetic_only"] is True
    assert meta["input"]["crop_px"] == 96 and meta["input"]["channels"] == 1
    op = meta["operating_point"]
    assert op["chosen_on"] == "val" and 0.0 <= op["threshold"] <= 1.0
    assert meta["metrics"]["val"]["threshold"] == op["threshold"] == meta["metrics"]["test"]["threshold"]
    assert meta["metrics"]["test"]["n"] == manifest["counts"]["verifier"]["test"]["pos"] + \
        manifest["counts"]["verifier"]["test"]["neg"]
    assert meta["metrics"]["train"]["steps"] == 4
    assert meta["train_config"]["seed"] == 0 and meta["train_config"]["sampling"] == "class_balanced"
    _, state = load_artifact(out, kind="verifier")
    assert all(isinstance(v, torch.Tensor) for v in state.values())


def test_same_seed_gives_same_weights(dataset, tmp_path):
    a = read_metadata(vt.train_verifier(dataset, tmp_path / "m1", vt.TrainConfig(**FAST)))
    b = read_metadata(vt.train_verifier(dataset, tmp_path / "m2", vt.TrainConfig(**FAST)))
    assert a["weights_sha256"] == b["weights_sha256"]


def test_learns_the_fixture(dataset, tmp_path):
    out = vt.train_verifier(dataset, tmp_path / "models", vt.TrainConfig(epochs=12, batch_size=16, patience=12))
    val = read_metadata(out)["metrics"]["val"]
    assert val["roc_auc"] > 0.8, val


def test_early_stopping_restores_best_epoch(dataset, tmp_path):
    out = vt.train_verifier(dataset, tmp_path / "models",
                            vt.TrainConfig(epochs=40, batch_size=16, patience=1, lr=3e-2))
    tr = read_metadata(out)["metrics"]["train"]
    assert tr["epochs_run"] < 40
    assert tr["best_val_loss"] == min(h["val_loss"] for h in tr["history"])


def test_choose_threshold_maximises_f1():
    probs = np.array([0.9, 0.8, 0.7, 0.4, 0.3, 0.1])
    labels = np.array([1, 1, 0, 1, 0, 0])
    t, f1 = vt.choose_threshold(probs, labels)
    assert t == 0.4 and f1 == pytest.approx(6 / 7)
    assert vt.roc_auc(probs, labels) == pytest.approx(8 / 9)


def test_augment_preserves_shape_and_range():
    x = torch.rand(8, 1, 96, 96)
    y = vt.augment(x, torch.Generator().manual_seed(1), vt.AugmentConfig())
    assert y.shape == x.shape and float(y.min()) >= 0.0 and float(y.max()) <= 1.0


def test_rejects_bad_datasets(tmp_path, dataset):
    with pytest.raises(vt.TrainingError) as e:
        vt.train_verifier(tmp_path / "nope", tmp_path / "m")
    assert e.value.code == "dataset_not_found"
    manifest = json.loads((dataset / "manifest.json").read_text())
    manifest["verifier"] = [s for s in manifest["verifier"] if not (s["split"] == "train" and s["label"] == 0)]
    (dataset / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(vt.TrainingError) as e:
        vt.train_verifier(dataset, tmp_path / "m", vt.TrainConfig(**FAST))
    assert e.value.code == "insufficient_data"
    manifest["verifier"][0]["path"] = "../../escape.png"
    (dataset / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(vt.TrainingError) as e:
        vt.train_verifier(dataset, tmp_path / "m", vt.TrainConfig(**FAST))
    assert e.value.code == "invalid_dataset"


def test_cli_train_verifier(dataset, tmp_path, capsys):
    rc = cli_main(["train-verifier", "--dataset", str(dataset), "--epochs", "1",
                   "--models-dir", str(tmp_path / "models")])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert read_metadata(summary["model_dir"])["model_id"] == summary["model_id"]
    assert cli_main(["no-such-command"]) == 2
