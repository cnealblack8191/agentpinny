"""Train the verifier (phase2 P1, P4, P5): ``python -m pinny.training train-verifier``.

Reads the ``verifier`` samples of a ``pinny.dataset`` v1 manifest, trains
``verifier-cnn-v1`` from scratch on CPU with a fixed seed, early-stops on
val loss, picks the threshold on val (max F1), scores test once at that
threshold, and writes a ``pinny.model`` v1 artifact.

Everything random (weight init, sampling, augmentation) is driven by
``seed``, so the same dataset and seed give the same model on the same
machine and torch build.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from pinny.errors import PinnyError
from pinny.models.artifact import save_artifact
from pinny.models.verifier import ARCH, CROP_PX, build_network, crops_to_tensor, parameter_count, predict_proba

DATASET_FORMAT = "pinny.dataset"
DATASET_FORMAT_VERSION = 1
SPLITS = ("train", "val", "test")


class TrainingError(PinnyError):
    pass


@dataclass
class AugmentConfig:
    quarter_rotations: bool = True
    flips: bool = True
    contrast: tuple[float, float] = (0.75, 1.25)   # ink multiplier
    brightness: tuple[float, float] = (-0.1, 0.1)  # ink offset
    blur_prob: float = 0.3
    blur_sigma: tuple[float, float] = (0.4, 1.0)


@dataclass
class TrainConfig:
    epochs: int = 30
    lr: float = 1e-3
    weight_decay: float = 1e-4
    batch_size: int = 64
    seed: int = 0
    patience: int = 5
    min_delta: float = 1e-4
    max_steps: int | None = None  # total optimiser steps cap (tests, smoke runs)
    augment: AugmentConfig = field(default_factory=AugmentConfig)


# ---- dataset -------------------------------------------------------------------

@dataclass
class SplitData:
    crops: np.ndarray    # (N, 96, 96) uint8
    labels: np.ndarray   # (N,) int64 in {0, 1}
    sample_ids: list[str]

    def __len__(self) -> int:
        return len(self.labels)


def load_manifest(dataset_dir: str | Path) -> dict[str, Any]:
    path = Path(dataset_dir) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TrainingError("dataset_not_found", f"No manifest.json in {dataset_dir}.") from None
    except (OSError, ValueError) as exc:
        raise TrainingError("invalid_dataset", f"Cannot read {path}: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("format") != DATASET_FORMAT \
            or manifest.get("format_version") != DATASET_FORMAT_VERSION:
        raise TrainingError("unsupported_dataset_format", f"{path} is not {DATASET_FORMAT} v{DATASET_FORMAT_VERSION}.")
    if not isinstance(manifest.get("verifier"), list):
        raise TrainingError("invalid_dataset", f"{path} has no 'verifier' sample list.")
    return manifest


def load_verifier_splits(dataset_dir: str | Path, manifest: Mapping[str, Any]) -> dict[str, SplitData]:
    import cv2

    root = Path(dataset_dir).resolve()
    rows: dict[str, list[tuple[str, np.ndarray, int]]] = {s: [] for s in SPLITS}
    for entry in manifest["verifier"]:
        split, label, rel = entry.get("split"), entry.get("label"), entry.get("path")
        if split not in rows or label not in (0, 1) or not isinstance(rel, str):
            raise TrainingError("invalid_dataset", f"Bad verifier sample {entry.get('sample_id')!r}.")
        path = (root / rel).resolve()
        if not path.is_relative_to(root):
            raise TrainingError("invalid_dataset", f"Sample path {rel!r} escapes the dataset directory.")
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise TrainingError("invalid_dataset", f"Cannot read verifier crop {path}.")
        if img.dtype != np.uint8 or img.shape != (CROP_PX, CROP_PX):
            raise TrainingError("invalid_dataset",
                                f"{path} must be a {CROP_PX}x{CROP_PX} grayscale uint8 PNG, got {img.shape} {img.dtype}.")
        rows[split].append((str(entry.get("sample_id")), img, int(label)))
    out = {}
    for split, items in rows.items():
        out[split] = SplitData(
            crops=np.stack([r[1] for r in items]) if items else np.zeros((0, CROP_PX, CROP_PX), np.uint8),
            labels=np.array([r[2] for r in items], dtype=np.int64),
            sample_ids=[r[0] for r in items],
        )
    return out


# ---- augmentation ----------------------------------------------------------------

def augment(x, gen, cfg: AugmentConfig):
    """Random dihedral transform, contrast/brightness and slight blur on an ink batch (B, 1, H, W)."""
    import torch
    import torch.nn.functional as F

    b = x.shape[0]
    x = x.clone()
    if cfg.quarter_rotations:
        ks = torch.randint(0, 4, (b,), generator=gen)
        for k in range(1, 4):
            idx = ks == k
            if idx.any():
                x[idx] = torch.rot90(x[idx], k, dims=(2, 3))
    if cfg.flips:
        h = torch.rand(b, generator=gen) < 0.5
        x[h] = torch.flip(x[h], dims=(3,))
        v = torch.rand(b, generator=gen) < 0.5
        x[v] = torch.flip(x[v], dims=(2,))
    lo, hi = cfg.contrast
    c = lo + (hi - lo) * torch.rand(b, 1, 1, 1, generator=gen)
    lo, hi = cfg.brightness
    o = lo + (hi - lo) * torch.rand(b, 1, 1, 1, generator=gen)
    x = (x * c + o).clamp_(0.0, 1.0)
    if cfg.blur_prob > 0:
        lo, hi = cfg.blur_sigma
        sigma = float(lo + (hi - lo) * torch.rand(1, generator=gen))
        g = torch.exp(-(torch.arange(-1, 2, dtype=torch.float32) ** 2) / (2 * sigma * sigma))
        g = g / g.sum()
        kernel = (g[:, None] * g[None, :]).view(1, 1, 3, 3)
        blurred = F.conv2d(F.pad(x, (1, 1, 1, 1), mode="replicate"), kernel)
        mask = (torch.rand(b, 1, 1, 1, generator=gen) < cfg.blur_prob)
        x = torch.where(mask, blurred, x)
    return x


# ---- metrics -------------------------------------------------------------------

def choose_threshold(probs: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """``(threshold, f1)`` maximising F1 for the rule ``p >= threshold``.

    Candidates are the distinct probabilities; ties go to the highest
    threshold (fewer false positives).
    """
    n_pos = int(labels.sum())
    if n_pos == 0:
        raise TrainingError("no_val_positives", "The val split has no positive samples; cannot choose a threshold.")
    order = np.argsort(-probs, kind="stable")
    p, y = probs[order], labels[order]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    last = np.r_[p[1:] != p[:-1], True]  # last index of each distinct value
    tp, fp, cut = tp[last], fp[last], p[last]
    f1 = 2 * tp / (2 * tp + fp + (n_pos - tp))
    best = int(np.argmax(f1))
    return float(cut[best]), float(f1[best])


def roc_auc(probs: np.ndarray, labels: np.ndarray) -> float | None:
    pos, neg = probs[labels == 1], probs[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    # Mann-Whitney U with average ranks for ties.
    allp = np.concatenate([pos, neg])
    order = np.argsort(allp, kind="stable")
    ranks = np.empty(len(allp))
    sorted_p = allp[order]
    i = 0
    while i < len(sorted_p):
        j = i
        while j + 1 < len(sorted_p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    u = ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2
    return float(u / (len(pos) * len(neg)))


def average_precision(probs: np.ndarray, labels: np.ndarray) -> float | None:
    n_pos = int(labels.sum())
    if n_pos == 0:
        return None
    order = np.argsort(-probs, kind="stable")
    y = labels[order]
    tp = np.cumsum(y)
    precision = tp / np.arange(1, len(y) + 1)
    return float((precision * y).sum() / n_pos)


def bce_loss(probs: np.ndarray, labels: np.ndarray) -> float | None:
    if len(labels) == 0:
        return None
    p = np.clip(probs, 1e-7, 1 - 1e-7)
    return float(-(labels * np.log(p) + (1 - labels) * np.log(1 - p)).mean())


def split_metrics(probs: np.ndarray, labels: np.ndarray, threshold: float) -> dict[str, Any]:
    pred = probs >= threshold
    tp = int((pred & (labels == 1)).sum())
    fp = int((pred & (labels == 0)).sum())
    fn = int((~pred & (labels == 1)).sum())
    tn = int((~pred & (labels == 0)).sum())
    n = len(labels)

    def ratio(a: int, b: int) -> float | None:
        return a / b if b else None

    return {
        "n": n, "pos": int(labels.sum()), "neg": int(n - labels.sum()),
        "threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn), "accuracy": ratio(tp + tn, n),
        "loss": bce_loss(probs, labels), "roc_auc": roc_auc(probs, labels),
        "average_precision": average_precision(probs, labels),
    }


# ---- training ------------------------------------------------------------------

def _require_both_classes(data: SplitData, split: str) -> None:
    if len(data) == 0 or data.labels.min() == data.labels.max():
        raise TrainingError("insufficient_data",
                            f"The {split} split needs both positive and negative verifier samples "
                            f"(has {int(data.labels.sum())} pos, {int(len(data) - data.labels.sum())} neg).")


def train_verifier(dataset_dir: str | Path, models_dir: str | Path, config: TrainConfig | None = None,
                   *, log=None) -> Path:
    """Train, choose the val threshold, score test once, save. Returns the artifact dir."""
    import torch
    import torch.nn.functional as F
    from torch.utils.data import WeightedRandomSampler

    cfg = config or TrainConfig()
    log = log or (lambda msg: None)
    manifest = load_manifest(dataset_dir)
    splits = load_verifier_splits(dataset_dir, manifest)
    train, val, test = splits["train"], splits["val"], splits["test"]
    _require_both_classes(train, "train")
    if len(val) == 0:
        raise TrainingError("insufficient_data", "The val split has no verifier samples.")

    torch.manual_seed(cfg.seed)
    gen = torch.Generator().manual_seed(cfg.seed)
    net = build_network()
    opt = torch.optim.AdamW(net.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)

    # Class-balanced sampling: each class is drawn with equal probability.
    counts = np.bincount(train.labels, minlength=2)
    weights = torch.from_numpy((1.0 / counts[train.labels]).astype(np.float64))
    sampler = WeightedRandomSampler(weights, num_samples=len(train), replacement=True, generator=gen)
    train_x = crops_to_tensor(train.crops)
    train_y = torch.from_numpy(train.labels.astype(np.float32))

    best_loss, best_state, best_epoch, bad_epochs = math.inf, copy.deepcopy(net.state_dict()), 0, 0
    steps, epochs_run, history = 0, 0, []
    started = time.perf_counter()
    for epoch in range(1, cfg.epochs + 1):
        net.train()
        order = torch.tensor(list(sampler), dtype=torch.long)
        running, seen = 0.0, 0
        for i in range(0, len(order), cfg.batch_size):
            idx = order[i:i + cfg.batch_size]
            if len(idx) < 2:  # BatchNorm needs more than one sample
                continue
            xb = augment(train_x[idx], gen, cfg.augment)
            loss = F.binary_cross_entropy_with_logits(net(xb).squeeze(1), train_y[idx])
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            running += loss.item() * len(idx)
            seen += len(idx)
            steps += 1
            if cfg.max_steps is not None and steps >= cfg.max_steps:
                break
        epochs_run = epoch
        val_loss = bce_loss(predict_proba(net, val.crops), val.labels)
        history.append({"epoch": epoch, "train_loss": running / max(seen, 1), "val_loss": val_loss})
        log(f"epoch {epoch}: train_loss={running / max(seen, 1):.4f} val_loss={val_loss:.4f}")
        if val_loss < best_loss - cfg.min_delta:
            best_loss, best_state, best_epoch, bad_epochs = val_loss, copy.deepcopy(net.state_dict()), epoch, 0
        else:
            bad_epochs += 1
        if bad_epochs >= cfg.patience or (cfg.max_steps is not None and steps >= cfg.max_steps):
            break
    train_seconds = time.perf_counter() - started

    net.load_state_dict(best_state)
    net.eval()
    val_probs = predict_proba(net, val.crops)
    threshold, _ = choose_threshold(val_probs, val.labels)
    metrics: dict[str, Any] = {"val": split_metrics(val_probs, val.labels, threshold)}
    # Test is scored exactly once, at the val threshold, and never used for tuning.
    metrics["test"] = split_metrics(predict_proba(net, test.crops), test.labels, threshold) if len(test) else {"n": 0}
    metrics["train"] = {
        "n": len(train), "pos": int(counts[1]), "neg": int(counts[0]), "seconds": round(train_seconds, 3),
        "epochs_run": epochs_run, "best_epoch": best_epoch, "steps": steps,
        "best_val_loss": best_loss, "parameters": parameter_count(net), "history": history,
    }

    train_config = asdict(cfg)
    train_config["optimizer"] = "adamw"
    train_config["sampling"] = "class_balanced"
    train_config["early_stopping"] = {"monitor": "val_loss", "patience": cfg.patience, "min_delta": cfg.min_delta}
    source = manifest.get("source") or {}
    return save_artifact(models_dir, kind="verifier", arch=ARCH, state_dict=net.state_dict(), metadata={
        "input": {"channels": 1, "crop_px": CROP_PX, "dpi": 200, "pad_value": 255},
        "dataset_id": manifest.get("dataset_id"),
        "synthetic_only": bool(source.get("synthetic", False)),
        "train_config": train_config,
        "operating_point": {"threshold": threshold, "chosen_on": "val", "criterion": "max_f1"},
        "metrics": metrics,
    })


# ---- CLI -------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    from pinny.learning.store import default_data_dir

    p = argparse.ArgumentParser(prog="python -m pinny.training train-verifier",
                                description="Train the verifier CNN on a pinny.dataset v1 directory.")
    p.add_argument("--dataset", required=True, help="dataset directory (holds manifest.json)")
    p.add_argument("--epochs", type=int, default=TrainConfig.epochs)
    p.add_argument("--batch-size", type=int, default=TrainConfig.batch_size)
    p.add_argument("--lr", type=float, default=TrainConfig.lr)
    p.add_argument("--seed", type=int, default=TrainConfig.seed)
    p.add_argument("--patience", type=int, default=TrainConfig.patience)
    p.add_argument("--models-dir", default=None, help="default: $PINNY_DATA_DIR/models")
    a = p.parse_args(argv)

    cfg = TrainConfig(epochs=a.epochs, batch_size=a.batch_size, lr=a.lr, seed=a.seed, patience=a.patience)
    models_dir = Path(a.models_dir) if a.models_dir else default_data_dir() / "models"
    try:
        out = train_verifier(a.dataset, models_dir, cfg, log=lambda m: print(m, file=sys.stderr))
    except PinnyError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    meta = json.loads((out / "model.json").read_text(encoding="utf-8"))
    summary = {"model_dir": str(out), "model_id": meta["model_id"],
               "threshold": meta["operating_point"]["threshold"],
               "val": {k: meta["metrics"]["val"].get(k) for k in ("precision", "recall", "f1", "roc_auc")},
               "test": {k: meta["metrics"]["test"].get(k) for k in ("n", "precision", "recall", "f1", "roc_auc")},
               "train_seconds": meta["metrics"]["train"]["seconds"]}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
