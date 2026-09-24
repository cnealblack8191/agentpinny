"""Template-free receptacle point detector (docs/phase2-contracts.md P1, P6).

A small CenterNet-style fully-convolutional net predicts a heatmap at
stride 4 plus a sub-pixel offset. A page is cut into 512 x 512 tiles with
a stride of 384 (P4); overlapping tile outputs are merged by taking the
maximum; peaks are picked with a 3 x 3 local-max test, the threshold, and
a greedy 10 px non-maximum suppression. See docs/phase2-detector.md.

Tiling, merging and peak picking are numpy-only. torch is imported only
when a net is built or run, so this module imports without torch.
"""

from __future__ import annotations

import functools
import math
import os
from collections.abc import Callable
from typing import Any

import cv2
import numpy as np

from pinny.errors import PinnyError

ARCH = "detector-centernet-v1"
TILE_PX = 512
STRIDE_PX = 384
OUT_STRIDE = 4
NMS_MIN_DIST_PX = 10.0
DPI = 200

# infer(batch float32 (N, 1, T, T)) -> (heat (N, T/4, T/4) in [0, 1], offset (N, 2, T/4, T/4) in [0, 1])
InferFn = Callable[[np.ndarray], tuple[np.ndarray, np.ndarray]]


class PointDetectorError(PinnyError):
    pass


# --------------------------------------------------------------------- input


def to_gray(page: np.ndarray) -> np.ndarray:
    """uint8 (H, W), (H, W, 1), RGB or RGBA -> uint8 grayscale (H, W)."""
    a = np.asarray(page)
    if a.dtype != np.uint8:
        raise PointDetectorError("invalid_page_dtype", f"page must be uint8, got {a.dtype}")
    if a.ndim == 2:
        return a
    if a.ndim == 3 and a.shape[2] == 1:
        return a[:, :, 0]
    if a.ndim == 3 and a.shape[2] == 3:
        return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    if a.ndim == 3 and a.shape[2] == 4:
        return cv2.cvtColor(a, cv2.COLOR_RGBA2GRAY)
    raise PointDetectorError("invalid_page_shape", f"page must be (H, W[, 1|3|4]), got {a.shape}")


def ink(gray: np.ndarray) -> np.ndarray:
    """Net input: 0 on white paper, 1 on black ink (so zero padding is paper)."""
    return (255.0 - gray.astype(np.float32)) * (1.0 / 255.0)


# -------------------------------------------------------------------- tiling


def tile_origins(length: int, tile: int = TILE_PX, stride: int = STRIDE_PX) -> list[int]:
    """Tile start offsets along one axis. Every origin is a multiple of the
    stride (so of OUT_STRIDE too), and the tiles cover ``[0, length)``; the
    last tile may run past the page, where the page is padded with white."""
    n = 1 if length <= tile else math.ceil((length - tile) / stride) + 1
    return [i * stride for i in range(n)]


def run_tiled(gray: np.ndarray, infer: InferFn, *, tile: int = TILE_PX, stride: int = STRIDE_PX,
              batch_size: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """Run ``infer`` over overlapping tiles and merge them into page maps.

    Returns ``heat (ceil(H/4), ceil(W/4))`` and ``offset (2, ceil(H/4), ceil(W/4))``.
    Output cell ``(i, j)`` covers page px ``[4j, 4j+4) x [4i, 4i+4)``. Where
    tiles overlap the heat is the maximum, and the offset comes from the tile
    that gave that maximum (the first one on a tie), so a point on a seam is
    one peak in one place.
    """
    if tile % OUT_STRIDE or stride % OUT_STRIDE:
        raise ValueError("tile and stride must be multiples of the output stride")
    h, w = gray.shape
    ys, xs = tile_origins(h, tile, stride), tile_origins(w, tile, stride)
    ph, pw = ys[-1] + tile, xs[-1] + tile
    padded = np.full((ph, pw), 255, np.uint8)
    padded[:h, :w] = gray
    x = ink(padded)
    t4 = tile // OUT_STRIDE
    heat = np.full((ph // OUT_STRIDE, pw // OUT_STRIDE), -1.0, np.float32)
    off = np.zeros((2, ph // OUT_STRIDE, pw // OUT_STRIDE), np.float32)
    origins = [(oy, ox) for oy in ys for ox in xs]
    for b in range(0, len(origins), batch_size):
        chunk = origins[b:b + batch_size]
        batch = np.stack([x[oy:oy + tile, ox:ox + tile] for oy, ox in chunk])[:, None]
        th, to = infer(batch)
        for k, (oy, ox) in enumerate(chunk):
            r, c = oy // OUT_STRIDE, ox // OUT_STRIDE
            region = heat[r:r + t4, c:c + t4]
            better = th[k] > region
            region[better] = th[k][better]
            for ch in range(2):
                off[ch, r:r + t4, c:c + t4][better] = to[k, ch][better]
    hc, wc = math.ceil(h / OUT_STRIDE), math.ceil(w / OUT_STRIDE)
    return heat[:hc, :wc].copy(), off[:, :hc, :wc].copy()


# ------------------------------------------------------------- peak picking


def pick_peaks(heat: np.ndarray, offset: np.ndarray, *, threshold: float, width: int, height: int,
               min_dist: float = NMS_MIN_DIST_PX, max_points: int = 500) -> list[dict]:
    """3 x 3 local maxima at or above ``threshold``, decoded to canonical px,
    then greedy NMS (a kept point suppresses any other within ``min_dist``).
    Returns ``[{"x", "y", "score"}]`` sorted by score, highest first."""
    if max_points <= 0:
        return []
    local_max = heat >= cv2.dilate(heat, np.ones((3, 3), np.uint8))
    ii, jj = np.nonzero(local_max & (heat >= threshold))
    if ii.size == 0:
        return []
    scores = heat[ii, jj]
    px = np.clip((jj + offset[0, ii, jj]) * OUT_STRIDE, 0.0, float(width))
    py = np.clip((ii + offset[1, ii, jj]) * OUT_STRIDE, 0.0, float(height))
    order = np.lexsort((px, py, -scores))
    cell = max(min_dist, 1e-6)
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    out: list[dict] = []
    d2 = min_dist * min_dist
    for k in order:
        x, y = float(px[k]), float(py[k])
        gx, gy = int(x // cell), int(y // cell)
        if any((x - qx) ** 2 + (y - qy) ** 2 < d2
               for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               for qx, qy in grid.get((gx + dx, gy + dy), ())):
            continue
        grid.setdefault((gx, gy), []).append((x, y))
        out.append({"x": x, "y": y, "score": float(scores[k])})
        if len(out) >= max_points:
            break
    return out


# ------------------------------------------------------------------- the net


@functools.cache
def _net_class():
    import torch
    from torch import nn

    def conv_bn(cin: int, cout: int, stride: int = 1, dilation: int = 1) -> nn.Sequential:
        return nn.Sequential(
            nn.Conv2d(cin, cout, 3, stride, padding=dilation, dilation=dilation, bias=False),
            nn.BatchNorm2d(cout), nn.ReLU(inplace=True))

    class HeatmapNet(nn.Module):
        """Input (N, 1, T, T) ink in [0, 1]; output logits heat (N, 1, T/4, T/4)
        and offset (N, 2, T/4, T/4). Encoder to stride 16, two top-down fusions
        back to stride 4, then a heatmap head and an offset head."""

        def __init__(self) -> None:
            super().__init__()
            self.stem = nn.Sequential(conv_bn(1, 16, 2), conv_bn(16, 32, 2), conv_bn(32, 32))
            self.down8 = nn.Sequential(conv_bn(32, 64, 2), conv_bn(64, 64))
            self.down16 = nn.Sequential(conv_bn(64, 96, 2), conv_bn(96, 96, dilation=2))
            self.lat16 = nn.Conv2d(96, 64, 1)
            self.fuse8 = conv_bn(64, 64)
            self.lat8 = nn.Conv2d(64, 32, 1)
            self.fuse4 = conv_bn(32, 32)
            self.heat = nn.Sequential(nn.Conv2d(32, 32, 3, padding=1), nn.ReLU(inplace=True), nn.Conv2d(32, 1, 1))
            self.offset = nn.Sequential(nn.Conv2d(32, 32, 3, padding=1), nn.ReLU(inplace=True), nn.Conv2d(32, 2, 1))
            nn.init.constant_(self.heat[-1].bias, -2.19)  # p = 0.1 at start (CenterNet)

        def forward(self, x):
            f4 = self.stem(x)
            f8 = self.down8(f4)
            f16 = self.down16(f8)
            up = nn.functional.interpolate(self.lat16(f16), size=f8.shape[-2:], mode="nearest")
            f8 = self.fuse8(f8 + up)
            up = nn.functional.interpolate(self.lat8(f8), size=f4.shape[-2:], mode="nearest")
            f4 = self.fuse4(f4 + up)
            return self.heat(f4), self.offset(f4)

    _ = torch  # torch is needed at class creation
    return HeatmapNet


def build_net():
    """A fresh, randomly initialised ``HeatmapNet`` (arch ``detector-centernet-v1``)."""
    return _net_class()()


def torch_infer(net) -> InferFn:
    import torch

    def infer(batch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        with torch.inference_mode():
            h, o = net(torch.from_numpy(batch))
            return torch.sigmoid(h)[:, 0].numpy(), torch.sigmoid(o).numpy()

    return infer


# ------------------------------------------------------------ the interface


class PointDetector:
    """P6 inference interface. ``detect_points`` returns canonical px."""

    def __init__(self, net, *, model_id: str, threshold: float, metadata: dict[str, Any] | None = None):
        self.net = net.eval()
        self.model_id = model_id
        self.threshold = float(threshold)
        self.metadata = dict(metadata or {})
        inp = self.metadata.get("input", {})
        self.tile_px = int(inp.get("tile_px", TILE_PX))
        self.stride_px = int(inp.get("stride_px", STRIDE_PX))

    @classmethod
    def load(cls, model_dir: str | os.PathLike) -> "PointDetector":
        from .artifact import load_artifact

        meta, state = load_artifact(model_dir, kind="detector")
        if meta.get("arch") != ARCH:
            raise PointDetectorError("model_arch_unsupported",
                                     f"expected arch {ARCH}, got {meta.get('arch')!r}")
        net = build_net()
        net.load_state_dict(state)
        return cls(net, model_id=meta["model_id"], threshold=meta["operating_point"]["threshold"],
                   metadata=meta)

    def heatmap(self, page: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return run_tiled(to_gray(page), torch_infer(self.net), tile=self.tile_px, stride=self.stride_px)

    def detect_points(self, page_rgb: np.ndarray, *, threshold: float | None = None,
                      max_points: int = 500) -> list[dict]:
        gray = to_gray(page_rgb)
        heat, off = run_tiled(gray, torch_infer(self.net), tile=self.tile_px, stride=self.stride_px)
        t = self.threshold if threshold is None else float(threshold)
        return pick_peaks(heat, off, threshold=t, width=gray.shape[1], height=gray.shape[0],
                          max_points=max_points)
