"""Tiny synthetic detector datasets for the detector tests (no test functions here).

Chat A's ``pinny.training.synthetic`` is the real generator; this is a
self-contained stand-in so the detector tests pass on their own.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


def draw_receptacle(img: np.ndarray, x: float, y: float, r: int = 17) -> None:
    """Duplex-receptacle-like glyph (circle + two bars), like tests/factory.py at 200 DPI."""
    c = (int(round(x - 0.5)), int(round(y - 0.5)))
    cv2.circle(img, c, r, 0, 3, cv2.LINE_AA)
    for dx in (-0.35 * r, 0.35 * r):
        cv2.line(img, (int(c[0] + dx), int(c[1] - 0.6 * r)), (int(c[0] + dx), int(c[1] + 0.6 * r)), 0, 3,
                 cv2.LINE_AA)


def synthetic_page(rng: np.random.Generator, width: int, height: int, n_points: int,
                   n_distractors: int = 6) -> tuple[np.ndarray, list[dict]]:
    img = np.full((height, width), 255, np.uint8)
    for _ in range(3):  # wall lines
        if rng.random() < 0.5:
            y = int(rng.integers(0, height))
            cv2.line(img, (0, y), (width, y), 0, 4)
        else:
            x = int(rng.integers(0, width))
            cv2.line(img, (x, 0), (x, height), 0, 4)
    for _ in range(n_distractors):
        x, y = int(rng.integers(20, width - 60)), int(rng.integers(20, height - 60))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + 30, y + 30), 0, 3)
        else:
            cv2.putText(img, "AB12", (x, y + 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
    pts: list[dict] = []
    while len(pts) < n_points:
        x, y = float(rng.uniform(30, width - 30)), float(rng.uniform(30, height - 30))
        if all((x - p["x"]) ** 2 + (y - p["y"]) ** 2 > 60 ** 2 for p in pts):
            draw_receptacle(img, x, y)
            pts.append({"x": x, "y": y})
    return img, pts


def write_dataset(root: Path, *, seed: int = 0, splits: dict[str, int] | None = None,
                  size: tuple[int, int] = (640, 560), n_points: int = 4) -> Path:
    """Write a pinny.dataset v1 directory with only detector pages. Returns root."""
    rng = np.random.default_rng(seed)
    splits = splits or {"train": 3, "val": 1, "test": 1}
    entries = []
    for split, n in splits.items():
        (root / "detector" / split).mkdir(parents=True, exist_ok=True)
        for i in range(n):
            img, pts = synthetic_page(rng, size[0], size[1], n_points)
            key = f"{split}-{i}"
            rel = f"detector/{split}/{key}.png"
            cv2.imwrite(str(root / rel), img)
            entries.append({"page_key": key, "split": split, "path": rel, "canonical_page_id": f"sha256:x#p{i}",
                            "document_id": f"doc-{split}-{i}", "width": size[0], "height": size[1],
                            "points": pts})
    manifest = {"format": "pinny.dataset", "format_version": 1, "dataset_id": f"synthetic-test-{seed}",
                "created_at": "2026-09-24T00:00:00Z", "source": {"synthetic": True},
                "verifier": [], "detector": entries}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root
