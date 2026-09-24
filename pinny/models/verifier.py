"""Verifier: rescores template-match candidates with a small CNN (phase2 P1, P6).

The crop rule here is the one the dataset uses (P4): a 96 x 96 grayscale
``uint8`` window centred on the pin, cut from the canonical raster, with
out-of-page pixels padded white (255). :func:`to_gray` and
:func:`crop_patch` are the reference implementation and need no torch, so
the dataset builder can import them and produce byte-identical crops.

Centring: pixel ``(i, j)`` covers ``[i, i+1) x [j, j+1)`` (contracts §2),
so the window centred on ``(x, y)`` is ``[x - 48, x + 48)``. Snapped to
the pixel grid, its top-left pixel is ``(floor(x + 0.5) - 48,
floor(y + 0.5) - 48)``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Any

import numpy as np

from pinny.errors import PinnyError

from .artifact import ModelArtifactError, load_artifact

ARCH = "verifier-cnn-v1"
CROP_PX = 96
PAD_VALUE = 255
DEFAULT_BATCH = 256


class VerifierError(PinnyError):
    pass


# ---- preprocessing (torch-free, shared with the dataset builder) -----------------

def to_gray(page: np.ndarray) -> np.ndarray:
    """Canonical raster -> grayscale uint8 (H, W).

    RGB uses OpenCV's ITU-R 601 weights (``cv2.COLOR_RGB2GRAY``); RGBA drops
    alpha first; a 2-D or single-channel array is returned as is.
    """
    import cv2

    a = np.asarray(page)
    if a.dtype != np.uint8:
        raise VerifierError("invalid_page", f"page must be uint8, got {a.dtype}.")
    if a.ndim == 2:
        return a
    if a.ndim == 3 and a.shape[2] == 1:
        return a[:, :, 0]
    if a.ndim == 3 and a.shape[2] in (3, 4):
        return cv2.cvtColor(np.ascontiguousarray(a[:, :, :3]), cv2.COLOR_RGB2GRAY)
    raise VerifierError("invalid_page", f"page must be (H, W), (H, W, 1), (H, W, 3) or (H, W, 4); got {a.shape}.")


def crop_origin(x: float, y: float, size: int = CROP_PX) -> tuple[int, int]:
    """Top-left pixel of the ``size`` x ``size`` window centred on ``(x, y)``."""
    if not (math.isfinite(x) and math.isfinite(y)):
        raise VerifierError("invalid_point", f"Point ({x}, {y}) is not finite.")
    half = size // 2
    return math.floor(x + 0.5) - half, math.floor(y + 0.5) - half


def crop_patch(gray: np.ndarray, x: float, y: float, size: int = CROP_PX) -> np.ndarray:
    """The ``size`` x ``size`` uint8 crop centred on ``(x, y)``, white-padded."""
    x0, y0 = crop_origin(x, y, size)
    h, w = gray.shape[:2]
    out = np.full((size, size), PAD_VALUE, dtype=np.uint8)
    sx0, sy0 = max(x0, 0), max(y0, 0)
    sx1, sy1 = min(x0 + size, w), min(y0 + size, h)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = gray[sy0:sy1, sx0:sx1]
    return out


def crop_batch(gray: np.ndarray, points: Sequence[tuple[float, float]], size: int = CROP_PX) -> np.ndarray:
    """``(N, size, size)`` uint8 crops for ``points``."""
    out = np.empty((len(points), size, size), dtype=np.uint8)
    for i, (x, y) in enumerate(points):
        out[i] = crop_patch(gray, float(x), float(y), size)
    return out


# ---- network ---------------------------------------------------------------------

def crops_to_tensor(crops: np.ndarray):
    """uint8 (N, H, W) crops -> float (N, 1, H, W) "ink" in [0, 1]: white 0, black 1."""
    import torch

    t = torch.from_numpy(np.ascontiguousarray(crops)).to(torch.float32)
    return (1.0 - t / 255.0).unsqueeze(1)


def build_network():
    """``verifier-cnn-v1``: four conv/BN/ReLU/max-pool blocks and a small MLP head.

    96 -> 48 -> 24 -> 12 -> 6 spatially, then adaptive-avg-pooled to 3 x 3
    (keeps coarse position, so "centred symbol" differs from "symbol near
    the edge"). Outputs one logit per crop.
    """
    from torch import nn

    def block(cin: int, cout: int) -> list:
        return [nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout),
                nn.ReLU(inplace=True), nn.MaxPool2d(2)]

    return nn.Sequential(
        *block(1, 16), *block(16, 32), *block(32, 64), *block(64, 64),
        nn.AdaptiveAvgPool2d(3), nn.Flatten(),
        nn.Linear(64 * 9, 64), nn.ReLU(inplace=True), nn.Dropout(0.3),
        nn.Linear(64, 1),
    )


def parameter_count(net) -> int:
    return sum(p.numel() for p in net.parameters())


def predict_proba(net, crops: np.ndarray, batch_size: int = DEFAULT_BATCH) -> np.ndarray:
    """Probabilities for uint8 crops, in eval mode, batched."""
    import torch

    if len(crops) == 0:
        return np.zeros((0,), dtype=np.float64)
    was_training = net.training
    net.eval()
    parts = []
    try:
        with torch.inference_mode():
            for i in range(0, len(crops), batch_size):
                logits = net(crops_to_tensor(crops[i:i + batch_size])).squeeze(1)
                parts.append(torch.sigmoid(logits).double().numpy())
    finally:
        net.train(was_training)
    return np.concatenate(parts)


# ---- inference API (P6) ----------------------------------------------------------

class Verifier:
    """A loaded verifier artifact. ``score`` returns ``p(receptacle)`` per point."""

    def __init__(self, net, meta: dict[str, Any], *, batch_size: int = DEFAULT_BATCH):
        self._net = net
        self.meta = meta
        self.model_id: str = meta["model_id"]
        self.threshold: float = float(meta["operating_point"]["threshold"])
        self.crop_px: int = int(meta["input"].get("crop_px", CROP_PX))
        self.batch_size = batch_size

    @classmethod
    def load(cls, model_dir: str | os.PathLike) -> "Verifier":
        meta, state = load_artifact(model_dir, kind="verifier")
        if meta["arch"] != ARCH:
            raise ModelArtifactError("unsupported_model_arch", f"Verifier arch {meta['arch']!r} is not {ARCH!r}.")
        if int(meta["input"].get("crop_px", CROP_PX)) != CROP_PX or int(meta["input"].get("channels", 1)) != 1:
            raise ModelArtifactError("unsupported_model_input", f"{ARCH} takes 1 x {CROP_PX} x {CROP_PX} crops.")
        if "threshold" not in meta.get("operating_point", {}):
            raise ModelArtifactError("invalid_model_metadata", "Verifier model.json has no operating_point.threshold.")
        net = build_network()
        try:
            net.load_state_dict(state, strict=True)
        except RuntimeError as exc:
            raise ModelArtifactError("invalid_model_weights", f"Weights do not fit {ARCH}: {exc}") from None
        net.eval()
        return cls(net, meta)

    def crops(self, page_rgb: np.ndarray, points: Sequence[tuple[float, float]]) -> np.ndarray:
        """The exact uint8 crops that :meth:`score` feeds the network."""
        return crop_batch(to_gray(page_rgb), points, self.crop_px)

    def score(self, page_rgb: np.ndarray, points: Sequence[tuple[float, float]]) -> list[float]:
        if len(points) == 0:
            return []
        probs = predict_proba(self._net, self.crops(page_rgb, points), self.batch_size)
        return [float(p) for p in probs]
