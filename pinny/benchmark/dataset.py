"""Read the test split of a ``pinny.dataset`` v1 directory (P4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .errors import BenchmarkError

DATASET_FORMAT = "pinny.dataset"
TEST_SPLIT = "test"


@dataclass(frozen=True)
class SplitPage:
    page_key: str
    safe_key: str
    path: Path
    canonical_page_id: str
    document_id: str
    document_version: str
    page_index: int
    width: int
    height: int
    points: List[Dict[str, float]] = field(default_factory=list)

    def load_rgb(self) -> np.ndarray:
        """The page raster as uint8 RGB (H, W, 3), as ``RenderService.render_page`` gives it."""
        import cv2

        img = cv2.imread(str(self.path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise BenchmarkError("page_unreadable", f"Cannot read page image {self.path}.")
        if img.dtype != np.uint8:
            raise BenchmarkError("page_unreadable", f"{self.path}: expected an 8-bit image.")
        if img.ndim == 3:
            code = cv2.COLOR_BGRA2GRAY if img.shape[2] == 4 else cv2.COLOR_BGR2GRAY
            img = cv2.cvtColor(img, code)
        if img.shape != (self.height, self.width):
            raise BenchmarkError(
                "page_size_mismatch",
                f"{self.path}: image is {img.shape[1]}x{img.shape[0]}, the manifest says "
                f"{self.width}x{self.height}.",
            )
        return np.ascontiguousarray(np.repeat(img[:, :, None], 3, axis=2))


@dataclass(frozen=True)
class SplitData:
    root: Path
    manifest: Dict[str, Any]
    pages: List[SplitPage]

    @property
    def dataset_id(self) -> str:
        return self.manifest["dataset_id"]

    @property
    def synthetic_only(self) -> bool:
        source = self.manifest.get("source") or {}
        return bool(source.get("synthetic") or source.get("synthetic_only"))

    @property
    def document_ids(self) -> List[str]:
        return sorted({p.document_id for p in self.pages})

    @property
    def reference_points(self) -> int:
        return sum(len(p.points) for p in self.pages)


def _safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def _split_page_id(canonical_page_id: str, where: str) -> tuple:
    version, sep, index = canonical_page_id.rpartition("#p")
    if not sep or not version or not index.isdigit():
        raise BenchmarkError(
            "invalid_manifest",
            f"{where}: canonical_page_id {canonical_page_id!r} is not '<document_version>#p<page_index>'.",
        )
    return version, int(index)


def load_test_split(root: Path, split: str = TEST_SPLIT) -> SplitData:
    root = Path(root)
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError("dataset_not_found", f"Cannot read {manifest_path} ({exc}).") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError("invalid_manifest", f"{manifest_path} is not valid JSON ({exc}).") from exc
    if manifest.get("format") != DATASET_FORMAT or manifest.get("format_version") != 1:
        raise BenchmarkError("invalid_manifest", f"{manifest_path} is not a pinny.dataset v1 manifest.")
    if not manifest.get("dataset_id"):
        raise BenchmarkError("invalid_manifest", f"{manifest_path} has no dataset_id.")

    root_resolved = root.resolve()
    pages: List[SplitPage] = []
    seen_safe: Dict[str, str] = {}
    for i, entry in enumerate(manifest.get("detector") or []):
        if entry.get("split") != split:
            continue
        where = f"{manifest_path}: detector[{i}]"
        try:
            key = str(entry["page_key"])
            rel = entry["path"]
            cpid = entry["canonical_page_id"]
            width, height = int(entry["width"]), int(entry["height"])
            points = [{"x": float(p["x"]), "y": float(p["y"])} for p in entry.get("points") or []]
            document_id = str(entry["document_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkError("invalid_manifest", f"{where}: malformed entry ({exc}).") from exc
        path = (root / rel).resolve()
        if root_resolved not in path.parents:
            raise BenchmarkError("invalid_manifest", f"{where}: path {rel!r} leaves the dataset directory.")
        version, page_index = _split_page_id(cpid, where)
        safe = _safe_key(key)
        if safe in seen_safe:
            raise BenchmarkError("invalid_manifest",
                                 f"{where}: page_key {key!r} collides with {seen_safe[safe]!r}.")
        seen_safe[safe] = key
        pages.append(SplitPage(key, safe, path, cpid, document_id, version, page_index,
                              width, height, points))
    if not pages:
        raise BenchmarkError(
            "empty_test_split",
            f"{manifest_path} has no detector pages in the '{split}' split. Label complete pages "
            "for at least one test document (docs/phase2-evaluation.md).",
        )
    pages.sort(key=lambda p: p.page_key)
    return SplitData(root, manifest, pages)


def first_point_template_box(page: SplitPage, size: int = 40) -> Optional[Dict[str, int]]:
    """The template rule: a ``size`` px square centred on the page's first
    test point (manifest order), shifted to lie inside the page. ``None`` when
    the page has no points."""
    if not page.points:
        return None
    if page.width < size or page.height < size:
        raise BenchmarkError("page_too_small",
                             f"Page {page.page_key} is smaller than the {size} px template.")
    p = page.points[0]
    x = min(max(int(round(p["x"] - size / 2)), 0), page.width - size)
    y = min(max(int(round(p["y"] - size / 2)), 0), page.height - size)
    return {"x": x, "y": y, "width": size, "height": size}
