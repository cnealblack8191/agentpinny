"""Tiny synthetic pinny.dataset v1 builder and fake P6 models (no torch)."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import cv2
import numpy as np

W, H = 480, 320


def draw_receptacle(img: np.ndarray, x: int, y: int) -> None:
    cv2.circle(img, (x, y), 12, 0, 2)
    cv2.line(img, (x - 4, y - 7), (x - 4, y + 7), 0, 2)
    cv2.line(img, (x + 4, y - 7), (x + 4, y + 7), 0, 2)


def draw_page(points: Sequence[Tuple[int, int]], distractors: Sequence[Tuple[int, int]]) -> np.ndarray:
    img = np.full((H, W), 255, np.uint8)
    cv2.line(img, (10, H - 20), (W - 10, H - 20), 0, 3)
    for x, y in distractors:
        cv2.rectangle(img, (x - 10, y - 10), (x + 10, y + 10), 0, 2)
    for x, y in points:
        draw_receptacle(img, x, y)
    return img


# (document_id, split, pages); each page: (points, distractors)
LAYOUT = [
    ("doc-a", "test", [([(60, 60), (200, 80), (330, 150)], [(420, 60)]),
                       ([(80, 200), (250, 220)], [(150, 120)])]),
    ("doc-b", "test", [([(100, 100), (300, 100), (100, 250), (380, 240)], [(200, 180)])]),
    ("doc-c", "test", [([(240, 160)], [(60, 60), (400, 250)])]),
    ("doc-t", "train", [([(50, 50), (150, 50)], [])]),
]


def build_dataset(root: Path, *, synthetic: bool = True, layout=LAYOUT) -> Dict:
    root.mkdir(parents=True, exist_ok=True)
    entries: List[Dict] = []
    for doc_id, split, pages in layout:
        version = "sha256:" + hashlib.sha256(doc_id.encode()).hexdigest()
        for page_index, (points, distractors) in enumerate(pages):
            img = draw_page(points, distractors)
            key = f"{doc_id}-p{page_index}"
            rel = f"detector/{split}/{key}.png"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(root / rel), img)
            entries.append({
                "page_key": key, "split": split, "path": rel,
                "canonical_page_id": f"{version}#p{page_index}", "document_id": doc_id,
                "width": W, "height": H,
                "points": [{"x": float(x), "y": float(y)} for x, y in points],
            })
    manifest = {
        "format": "pinny.dataset", "format_version": 1,
        "dataset_id": "synthetic-test-dataset",
        "created_at": "2026-09-24T00:00:00Z",
        "source": {"export_sha256": "n/a", "store_schema_version": 1, "synthetic": synthetic},
        "split": {"method": "document_id_hash", "seed": 0,
                  "fractions": {"train": 0.7, "val": 0.15, "test": 0.15}},
        "verifier": [],
        "detector": entries,
        "counts": {},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def page_rgb(root: Path, entry: Dict) -> np.ndarray:
    img = cv2.imread(str(root / entry["path"]), cv2.IMREAD_GRAYSCALE)
    return np.ascontiguousarray(np.repeat(img[:, :, None], 3, axis=2))


def write_fake_model(model_dir: Path, kind: str, model_id: str, threshold: float, data: Dict) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.json").write_text(json.dumps({
        "format": "pinny.model", "format_version": 1, "model_id": model_id, "kind": kind,
        "arch": f"fake-{kind}", "dataset_id": "synthetic-test-dataset", "synthetic_only": True,
        "operating_point": {"threshold": threshold, "chosen_on": "val"},
    }))
    (model_dir / "fake.json").write_text(json.dumps(data))
    return model_dir


class FakeVerifier:
    """P6 Verifier: scores 0.9 near a known receptacle, else 0.1."""

    def __init__(self, model_id: str, threshold: float, points: List[List[float]]):
        self.model_id = model_id
        self.threshold = threshold
        self._points = points
        self.calls = 0

    @classmethod
    def load(cls, model_dir) -> "FakeVerifier":
        meta = json.loads((Path(model_dir) / "model.json").read_text())
        data = json.loads((Path(model_dir) / "fake.json").read_text())
        return cls(meta["model_id"], meta["operating_point"]["threshold"], data["points"])

    def score(self, page_rgb: np.ndarray, points) -> List[float]:
        assert page_rgb.dtype == np.uint8 and page_rgb.ndim == 3 and page_rgb.shape[2] == 3
        self.calls += 1
        return [0.9 if any(math.hypot(x - px, y - py) <= 6 for px, py in self._points) else 0.1
                for x, y in points]


class FakePointDetector:
    """P6 PointDetector: returns the points stored for a page (keyed by raster hash)."""

    def __init__(self, model_id: str, threshold: float, pages: Dict[str, List[Dict]]):
        self.model_id = model_id
        self.threshold = threshold
        self._pages = pages

    @classmethod
    def load(cls, model_dir) -> "FakePointDetector":
        meta = json.loads((Path(model_dir) / "model.json").read_text())
        data = json.loads((Path(model_dir) / "fake.json").read_text())
        return cls(meta["model_id"], meta["operating_point"]["threshold"], data["pages"])

    def detect_points(self, page_rgb: np.ndarray, *, threshold=None, max_points: int = 500):
        assert page_rgb.dtype == np.uint8 and page_rgb.shape[2] == 3
        key = hashlib.sha256(page_rgb.tobytes()).hexdigest()
        return list(self._pages.get(key, []))[:max_points]


def raster_key(rgb: np.ndarray) -> str:
    return hashlib.sha256(rgb.tobytes()).hexdigest()
