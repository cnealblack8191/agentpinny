"""Fake P6 model classes and artifact helpers for integration tests.

``FakeVerifier`` and ``FakePointDetector`` satisfy the P6 inference
interface (docs/phase2-contracts.md) without torch or trained weights. Their
behaviour comes from a ``"fake"`` block in the artifact's ``model.json``,
so a test says what the "model" will answer.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np


def _meta(model_dir) -> dict:
    return json.loads((Path(model_dir) / "model.json").read_text())


def _check_page(page_rgb) -> None:
    assert isinstance(page_rgb, np.ndarray) and page_rgb.dtype == np.uint8
    assert page_rgb.ndim == 3 and page_rgb.shape[2] == 3


class FakeVerifier:
    """Scores ``low`` near any point in ``fake.reject`` and ``high`` elsewhere."""

    loads = 0

    def __init__(self, meta: dict) -> None:
        self.model_id = meta["model_id"]
        self.threshold = float(meta["operating_point"]["threshold"])
        fake = meta.get("fake", {})
        self._reject = [tuple(p) for p in fake.get("reject", [])]
        self._low, self._high = fake.get("low", 0.1), fake.get("high", 0.9)
        self.calls: List[int] = []

    @classmethod
    def load(cls, model_dir) -> "FakeVerifier":
        cls.loads += 1
        return cls(_meta(model_dir))

    def score(self, page_rgb: np.ndarray, points: Sequence[Tuple[float, float]]) -> List[float]:
        _check_page(page_rgb)
        self.calls.append(len(points))
        return [self._low if any(abs(x - rx) <= 3 and abs(y - ry) <= 3 for rx, ry in self._reject)
                else self._high for x, y in points]


class FakePointDetector:
    """Returns the ``fake.points`` ([x, y, score]) whose score is >= threshold."""

    loads = 0

    def __init__(self, meta: dict) -> None:
        self.model_id = meta["model_id"]
        self.threshold = float(meta["operating_point"]["threshold"])
        self._points = [tuple(p) for p in meta.get("fake", {}).get("points", [])]

    @classmethod
    def load(cls, model_dir) -> "FakePointDetector":
        cls.loads += 1
        return cls(_meta(model_dir))

    def detect_points(self, page_rgb: np.ndarray, *, threshold: Optional[float] = None,
                      max_points: int = 500) -> List[dict]:
        _check_page(page_rgb)
        t = self.threshold if threshold is None else threshold
        out = [{"x": x, "y": y, "score": s} for x, y, s in self._points if s >= t]
        return sorted(out, key=lambda p: -p["score"])[:max_points]


FAKE_CLASSES = {"verifier": FakeVerifier, "detector": FakePointDetector}


def make_model(data_dir, kind: str, *, threshold: float = 0.5, fake: Optional[dict] = None,
               stamp: str = "20260924T120000Z", synthetic_only: bool = False,
               weights: Optional[bytes] = None) -> str:
    """Write a pinny.model v1 artifact (P5) with placeholder weights; returns its id."""
    weights = weights if weights is not None else os.urandom(64)
    sha = hashlib.sha256(weights).hexdigest()
    model_id = f"{kind}-{stamp}-{sha[:8]}"
    d = Path(data_dir) / "models" / model_id
    d.mkdir(parents=True)
    (d / "weights.pt").write_bytes(weights)
    meta = {
        "format": "pinny.model", "format_version": 1, "model_id": model_id, "kind": kind,
        "arch": f"fake-{kind}", "input": {"channels": 1, "crop_px": 96, "tile_px": 512,
                                          "stride_px": 384, "dpi": 200},
        "dataset_id": "d" * 64, "synthetic_only": synthetic_only,
        "train_config": {"epochs": 0, "lr": 0.0, "batch_size": 0, "seed": 0, "augment": {}},
        "weights_sha256": sha, "code_version": "git:test",
        "operating_point": {"threshold": threshold, "chosen_on": "val"},
        "metrics": {"val": {}, "test": {}},
        "created_at": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:"
                      f"{stamp[13:15]}Z",
    }
    if fake is not None:
        meta["fake"] = fake
    (d / "model.json").write_text(json.dumps(meta))
    return model_id


def write_evidence(path, model_id: str, *, promote=True, mode: Optional[str] = None,
                   **extra) -> Path:
    """Write a pinny.promotion v1 report (P9) for ``model_id``."""
    kind = model_id.split("-", 1)[0]
    mode = mode or {"verifier": "template+verifier", "detector": "model"}.get(kind, "model")
    report = {"format": "pinny.promotion", "format_version": 1, "promote": promote,
              "dataset_id": "d" * 64, "split": "test", "tolerance_px": 12,
              "baseline": {"mode": "template"},
              "candidate": {"mode": mode, "model_id": model_id}}
    report.update(extra)
    path = Path(path)
    path.write_text(json.dumps(report))
    return path


def promote(data_dir, model_id: str, tmp_path) -> None:
    from pinny.models.registry import ModelRegistry
    ev = write_evidence(Path(tmp_path) / f"evidence-{model_id}.json", model_id)
    ModelRegistry(data_dir).promote(model_id, ev)


def points_near(points: Iterable[Tuple[float, float]], x: float, y: float, tol: float = 3) -> bool:
    return any(abs(px - x) <= tol and abs(py - y) <= tol for px, py in points)
