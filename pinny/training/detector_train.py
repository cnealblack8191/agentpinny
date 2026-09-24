"""Train the point detector (docs/phase2-contracts.md P1, P4, P5, P9).

``python -m pinny.training train-detector --dataset <dir> [--epochs N]``

Reads the ``detector`` entries of a ``pinny.dataset`` v1 manifest, trains
``detector-centernet-v1`` from scratch on CPU with a fixed seed, chooses the
operating threshold on ``val`` with the P9 matching rule (12 px, one-to-one),
scores ``test`` once at that threshold, and writes a ``pinny.model`` v1
artifact. See docs/phase2-detector.md.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from pinny.errors import PinnyError
from pinny.models.point_detector import (
    ARCH, DPI, NMS_MIN_DIST_PX, OUT_STRIDE, STRIDE_PX, TILE_PX, PointDetector, build_net, ink, tile_origins)

TOLERANCE_PX = 12.0  # P9, fixed in advance
SIGMA_CELLS = 4.0  # Gaussian sigma at output resolution
CANDIDATE_THRESHOLD = 0.05
THRESHOLD_GRID = [round(0.05 + 0.025 * i, 3) for i in range(37)]  # 0.05 .. 0.95
SYNTHETIC_NOTE = "synthetic — not real-drawing accuracy"


class DetectorTrainError(PinnyError):
    pass


@dataclass
class TrainConfig:
    epochs: int = 16
    tiles_per_epoch: int = 2048
    batch_size: int = 8
    lr: float = 2e-3
    weight_decay: float = 1e-4
    seed: int = 0
    pages_per_chunk: int = 16  # pages decoded at once while sampling tiles
    augment: dict[str, Any] = field(default_factory=lambda: {
        "rot90": [0, 1, 2, 3], "hflip": 0.5, "scale": [0.9, 1.1], "contrast": [0.6, 1.2],
        "jitter_px": 32, "noise_std": 0.03, "noise_p": 0.3,
    })


@dataclass
class Page:
    page_key: str
    split: str
    path: Path
    width: int
    height: int
    points: np.ndarray  # (K, 2) canonical px

    def load(self) -> np.ndarray:
        img = cv2.imread(str(self.path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise DetectorTrainError("dataset_page_unreadable", f"cannot read detector page {self.path}")
        return img


# ------------------------------------------------------------------ dataset


def load_manifest(dataset: str | os.PathLike) -> tuple[dict[str, Any], dict[str, list[Page]]]:
    root = Path(dataset)
    mpath = root / "manifest.json" if root.is_dir() else root
    root = mpath.parent
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DetectorTrainError("dataset_unreadable", f"cannot read {mpath}: {exc}") from exc
    if manifest.get("format") != "pinny.dataset" or manifest.get("format_version") != 1:
        raise DetectorTrainError("dataset_format_unsupported", f"{mpath} is not pinny.dataset v1")
    splits: dict[str, list[Page]] = {"train": [], "val": [], "test": []}
    for e in manifest.get("detector", []):
        pts = np.array([[p["x"], p["y"]] for p in e.get("points", [])], np.float32).reshape(-1, 2)
        page = Page(e["page_key"], e["split"], root / e["path"], int(e["width"]), int(e["height"]), pts)
        splits.setdefault(page.split, []).append(page)
    for pages in splits.values():
        pages.sort(key=lambda p: p.page_key)
    if not splits["train"]:
        raise DetectorTrainError("dataset_no_train_pages", f"{mpath} has no detector pages in the train split")
    return manifest, splits


# ------------------------------------------------------- tiles and targets


def page_tiles(page: Page, tile: int = TILE_PX, stride: int = STRIDE_PX) -> tuple[list, list]:
    """Grid tile origins of a page, split into (with points, without points)."""
    pos, neg = [], []
    for oy in tile_origins(page.height, tile, stride):
        for ox in tile_origins(page.width, tile, stride):
            p = page.points
            inside = ((p[:, 0] >= ox) & (p[:, 0] < ox + tile) & (p[:, 1] >= oy) & (p[:, 1] < oy + tile)).any()
            (pos if inside else neg).append((ox, oy))
    return pos, neg


def rot90_points(pts: np.ndarray, k: int, size: int) -> np.ndarray:
    """Point transform matching ``np.rot90(tile, k)`` (continuous px, size x size tile)."""
    pts = pts.copy()
    for _ in range(k % 4):
        pts = np.stack([pts[:, 1], size - pts[:, 0]], axis=1)
    return pts


def augment_tile(gray: np.ndarray, points: np.ndarray, origin: tuple[int, int], rng: np.random.Generator,
                 aug: dict[str, Any] | None, tile: int = TILE_PX) -> tuple[np.ndarray, np.ndarray]:
    """Cut one (augmented) tile from a page. Returns ink float32 (T, T) and points (K, 2) in tile px."""
    ox, oy = origin
    cx, cy = ox + tile / 2, oy + tile / 2
    s = 1.0
    if aug:
        j = aug.get("jitter_px", 0)
        if j:
            cx += rng.uniform(-j, j)
            cy += rng.uniform(-j, j)
        lo, hi = aug.get("scale", (1.0, 1.0))
        s = rng.uniform(lo, hi)
    # continuous coords: dst = s * (src - c) + T/2; cv2 maps pixel indices (centre = idx + 0.5)
    m = np.array([[s, 0, s * (0.5 - cx) + tile / 2 - 0.5],
                  [0, s, s * (0.5 - cy) + tile / 2 - 0.5]], np.float64)
    out = cv2.warpAffine(gray, m, (tile, tile), flags=cv2.INTER_LINEAR if s != 1.0 else cv2.INTER_NEAREST,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    pts = (points - np.array([cx, cy], np.float32)) * s + tile / 2 if len(points) else points.reshape(0, 2)
    x = ink(out)
    if aug:
        k = int(rng.choice(aug.get("rot90", [0])))
        if k:
            x = np.rot90(x, k)
            pts = rot90_points(pts, k, tile)
        if rng.random() < aug.get("hflip", 0.0):
            x = x[:, ::-1]
            pts = np.stack([tile - pts[:, 0], pts[:, 1]], axis=1) if len(pts) else pts
        lo, hi = aug.get("contrast", (1.0, 1.0))
        x = x * rng.uniform(lo, hi)
        if rng.random() < aug.get("noise_p", 0.0):
            x = x + rng.normal(0.0, aug.get("noise_std", 0.0), x.shape).astype(np.float32)
        x = np.clip(x, 0.0, 1.0)
    keep = (pts[:, 0] >= 0) & (pts[:, 0] < tile) & (pts[:, 1] >= 0) & (pts[:, 1] < tile) if len(pts) else []
    return np.ascontiguousarray(x, np.float32), np.asarray(pts, np.float32).reshape(-1, 2)[keep]


def make_targets(points: np.ndarray, tile: int = TILE_PX,
                 sigma: float = SIGMA_CELLS) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Heatmap (R, R) with a Gaussian of peak exactly 1 at each point's cell,
    offset target (2, R, R) = sub-cell position in [0, 1), and its mask (R, R)."""
    r = tile // OUT_STRIDE
    heat = np.zeros((r, r), np.float32)
    off = np.zeros((2, r, r), np.float32)
    mask = np.zeros((r, r), np.float32)
    rad = int(math.ceil(3 * sigma))
    ax = np.arange(-rad, rad + 1, dtype=np.float32)
    g = np.exp(-(ax[None, :] ** 2 + ax[:, None] ** 2) / (2 * sigma * sigma))
    for x, y in points:
        u, v = x / OUT_STRIDE, y / OUT_STRIDE
        j, i = min(int(u), r - 1), min(int(v), r - 1)
        y0, y1, x0, x1 = max(0, i - rad), min(r, i + rad + 1), max(0, j - rad), min(r, j + rad + 1)
        np.maximum(heat[y0:y1, x0:x1], g[y0 - i + rad:y1 - i + rad, x0 - j + rad:x1 - j + rad],
                   out=heat[y0:y1, x0:x1])
        off[:, i, j] = (u - j, v - i)
        mask[i, j] = 1.0
    return heat, off, mask


def focal_loss(logits, gt):
    """CenterNet penalty-reduced focal loss (alpha 2, beta 4), normalised by the number of peaks."""
    import torch

    p = torch.sigmoid(logits).clamp(1e-4, 1 - 1e-4)
    pos = gt.eq(1).float()
    pos_loss = torch.log(p) * (1 - p) ** 2 * pos
    neg_loss = torch.log(1 - p) * p ** 2 * (1 - gt) ** 4 * (1 - pos)
    return -(pos_loss.sum() + neg_loss.sum()) / pos.sum().clamp(min=1.0)


def detector_loss(heat_logits, off_logits, heat_gt, off_gt, mask):
    import torch

    focal = focal_loss(heat_logits[:, 0], heat_gt)
    off = (torch.abs(torch.sigmoid(off_logits) - off_gt) * mask[:, None]).sum() / mask.sum().clamp(min=1.0)
    return focal + off, focal, off


# ----------------------------------------------------------------- sampling


def iter_batches(pages: Sequence[Page], cfg: TrainConfig, rng: np.random.Generator,
                 tile: int = TILE_PX):
    """One epoch of batches: pages are visited in shuffled chunks (so only a
    chunk is decoded at a time); within a chunk tiles are drawn 50/50 from
    grid tiles with and without points."""
    order = rng.permutation(len(pages))
    chunks = [order[i:i + cfg.pages_per_chunk] for i in range(0, len(order), cfg.pages_per_chunk)]
    remaining = cfg.tiles_per_epoch
    buf: list[tuple[np.ndarray, np.ndarray]] = []
    for ci, chunk in enumerate(chunks):
        n = remaining if ci == len(chunks) - 1 else round(cfg.tiles_per_epoch * len(chunk) / len(pages))
        remaining -= n
        imgs = {int(k): pages[k].load() for k in chunk}
        pos, neg = [], []
        for k in chunk:
            p, q = page_tiles(pages[k], tile)
            pos += [(int(k), o) for o in p]
            neg += [(int(k), o) for o in q]
        picks = []
        for i in range(n):
            pool = (pos if i % 2 == 0 else neg) or pos or neg
            picks.append(pool[rng.integers(len(pool))])
        rng.shuffle(picks)
        for k, o in picks:
            buf.append(augment_tile(imgs[k], pages[k].points, o, rng, cfg.augment, tile))
            if len(buf) == cfg.batch_size:
                yield _collate(buf, tile)
                buf = []
    if buf:
        yield _collate(buf, tile)


def _collate(samples, tile):
    import torch

    xs, hs, os_, ms = [], [], [], []
    for x, pts in samples:
        h, o, m = make_targets(pts, tile)
        xs.append(x[None]); hs.append(h); os_.append(o); ms.append(m)
    return tuple(torch.from_numpy(np.stack(a)) for a in (xs, hs, os_, ms))


# --------------------------------------------------------------- evaluation


def match_count(preds: Sequence[tuple[float, float]], refs: Sequence[tuple[float, float]],
                tolerance: float = TOLERANCE_PX) -> int:
    """Size of a maximum one-to-one matching with distance <= tolerance (P9).
    This is the TP count of the Phase 1 evaluator (its distance tie-break
    does not change the count)."""
    if not len(preds) or not len(refs):
        return 0
    p = np.asarray(preds, np.float64).reshape(-1, 2)
    r = np.asarray(refs, np.float64).reshape(-1, 2)
    d = np.hypot(p[:, None, 0] - r[None, :, 0], p[:, None, 1] - r[None, :, 1])
    adj = [np.nonzero(row <= tolerance)[0].tolist() for row in d]
    match_r = [-1] * len(r)

    def augment(u: int, seen: set[int]) -> bool:  # Kuhn's augmenting path
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                if match_r[v] == -1 or augment(match_r[v], seen):
                    match_r[v] = u
                    return True
        return False

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, len(p) + 100))
    try:
        return sum(augment(u, set()) for u in range(len(p)) if adj[u])
    finally:
        sys.setrecursionlimit(limit)


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def candidates(detector: PointDetector, pages: Sequence[Page], threshold: float) -> list[list[dict]]:
    out = []
    for page in pages:
        out.append(detector.detect_points(page.load(), threshold=threshold, max_points=100_000))
    return out


def score_at(cands: Sequence[list[dict]], pages: Sequence[Page], threshold: float) -> dict[str, Any]:
    tp = fp = fn = 0
    for dets, page in zip(cands, pages):
        pts = [(d["x"], d["y"]) for d in dets if d["score"] >= threshold]
        t = match_count(pts, page.points.tolist())
        tp += t; fp += len(pts) - t; fn += len(page.points) - t
    return {"threshold": threshold, "tp": tp, "fp": fp, "fn": fn, **prf(tp, fp, fn)}


def choose_threshold(cands: Sequence[list[dict]], pages: Sequence[Page],
                     grid: Sequence[float] = THRESHOLD_GRID) -> dict[str, Any]:
    """Best F1 over the grid (ties go to the higher threshold)."""
    best = None
    for t in grid:
        s = score_at(cands, pages, t)
        if best is None or s["f1"] >= best["f1"]:
            best = s
    return best


def _split_metrics(s: dict[str, Any], pages: Sequence[Page], split: str, synthetic: bool) -> dict[str, Any]:
    m = {**s, "split": split, "pages": len(pages), "points": int(sum(len(p.points) for p in pages)),
         "tolerance_px": TOLERANCE_PX}
    if synthetic:
        m["note"] = SYNTHETIC_NOTE
    return m


# ------------------------------------------------------------------- train


def train_detector(dataset: str | os.PathLike, models_dir: str | os.PathLike | None = None,
                   cfg: TrainConfig | None = None, *, log: Callable[[str], None] | None = print,
                   save: bool = True) -> dict[str, Any]:
    """Train, choose the threshold on val, score test once, save the artifact.

    Returns ``{"model_dir", "metadata", "history"}``; ``history`` is the mean
    training loss per epoch.
    """
    import torch

    cfg = cfg or TrainConfig()
    log = log or (lambda _m: None)
    manifest, splits = load_manifest(dataset)
    synthetic = bool(manifest.get("source", {}).get("synthetic", False))

    random.seed(cfg.seed)
    np.random.seed(cfg.seed)
    torch.manual_seed(cfg.seed)
    rng = np.random.default_rng(cfg.seed)

    net = build_net()
    opt = torch.optim.AdamW(net.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    steps_per_epoch = math.ceil(cfg.tiles_per_epoch / cfg.batch_size)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=cfg.lr, total_steps=max(1, cfg.epochs * steps_per_epoch),
                                                pct_start=0.1)
    history: list[float] = []
    t0 = time.monotonic()
    for epoch in range(cfg.epochs):
        net.train()
        total, n = 0.0, 0
        for x, h, o, m in iter_batches(splits["train"], cfg, rng):
            hl, ol = net(x)
            loss, _, _ = detector_loss(hl, ol, h, o, m)
            opt.zero_grad()
            loss.backward()
            opt.step()
            if sched.last_epoch + 1 < sched.total_steps:
                sched.step()
            total += loss.item() * len(x)
            n += len(x)
        history.append(total / max(n, 1))
        log(f"epoch {epoch + 1}/{cfg.epochs}: loss {history[-1]:.4f} ({time.monotonic() - t0:.0f}s)")

    detector = PointDetector(net, model_id="unsaved", threshold=0.5)
    metrics: dict[str, Any] = {}
    if splits["val"]:
        val = choose_threshold(candidates(detector, splits["val"], CANDIDATE_THRESHOLD), splits["val"])
        threshold, chosen_on = val["threshold"], "val"
        metrics["val"] = _split_metrics(val, splits["val"], "val", synthetic)
    else:
        threshold, chosen_on = 0.5, "default"
        metrics["val"] = {}
        log("warning: no val pages; threshold left at the default 0.5")
    if splits["test"]:  # scored once, at the val threshold
        test = score_at(candidates(detector, splits["test"], threshold), splits["test"], threshold)
        metrics["test"] = _split_metrics(test, splits["test"], "test", synthetic)
    else:
        metrics["test"] = {}
    for split in ("val", "test"):
        if metrics[split]:
            s = metrics[split]
            log(f"{split}: P {s['precision']:.3f} R {s['recall']:.3f} F1 {s['f1']:.3f} "
                f"@ {threshold} (tol {TOLERANCE_PX:g} px){' — ' + SYNTHETIC_NOTE if synthetic else ''}")

    train_config = {k: v for k, v in asdict(cfg).items()}
    train_config.update({"sigma_cells": SIGMA_CELLS, "loss": "centernet-focal+offset-l1",
                         "optimizer": "adamw+onecycle", "nms_min_dist_px": NMS_MIN_DIST_PX,
                         "train_seconds": round(time.monotonic() - t0, 1)})
    fields = dict(
        kind="detector", arch=ARCH, state_dict=net.state_dict(),
        input={"channels": 1, "tile_px": TILE_PX, "stride_px": STRIDE_PX, "dpi": DPI, "out_stride": OUT_STRIDE},
        dataset_id=manifest.get("dataset_id", ""), synthetic_only=synthetic, train_config=train_config,
        operating_point={"threshold": threshold, "chosen_on": chosen_on,
                         "tolerance_px": TOLERANCE_PX, "criterion": "max_f1"},
        metrics=metrics)
    model_dir = None
    meta = {k: v for k, v in fields.items() if k != "state_dict"}
    if save:
        from pinny.models.artifact import default_models_dir, read_metadata, save_model

        model_dir = save_model(models_dir or default_models_dir(), **fields)
        meta = read_metadata(model_dir)
        log(f"saved {model_dir}")
    return {"model_dir": model_dir, "metadata": meta, "history": history, "net": net}


def main(argv: list[str] | None = None) -> int:
    d = TrainConfig()
    ap = argparse.ArgumentParser(prog="python -m pinny.training train-detector",
                                 description="Train the template-free point detector (P1, P6).")
    ap.add_argument("--dataset", required=True, help="dataset directory (with manifest.json)")
    ap.add_argument("--epochs", type=int, default=d.epochs)
    ap.add_argument("--tiles-per-epoch", type=int, default=d.tiles_per_epoch)
    ap.add_argument("--batch-size", type=int, default=d.batch_size)
    ap.add_argument("--lr", type=float, default=d.lr)
    ap.add_argument("--seed", type=int, default=d.seed)
    ap.add_argument("--models-dir", default=None, help="default: $PINNY_DATA_DIR/models")
    a = ap.parse_args(argv)
    cfg = TrainConfig(epochs=a.epochs, tiles_per_epoch=a.tiles_per_epoch, batch_size=a.batch_size,
                      lr=a.lr, seed=a.seed)
    try:
        res = train_detector(a.dataset, a.models_dir, cfg)
    except PinnyError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps({"model_dir": str(res["model_dir"]), "model_id": res["metadata"]["model_id"],
                      "metrics": res["metadata"]["metrics"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
