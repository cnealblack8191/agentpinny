"""Pure helpers for the learning loop (no database, no detector dependencies).

``LearningStore`` gathers the data (``review_stats``, ``template_bank_crops``,
``export_dataset`` ...); the arithmetic lives here so it can be tested and
reused on its own. Nothing here changes detector settings: a suggested
threshold is only ever a suggestion for a human to apply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_CLASS_LABEL = "receptacle"


@dataclass(frozen=True)
class ThresholdSuggestion:
    """Result of ``suggest_threshold``. ``value`` is ``None`` when no
    threshold can be suggested; ``reason`` then says why.

    * ``precision``: approved / reviewed among reviewed pins with
      ``score >= value``.
    * ``recall_proxy``: share of *reviewed approved* pins kept at ``value``.
      It ignores receptacles the detector never proposed (manual adds) and
      pins below the scan's own threshold, so it overstates true recall.
    """

    value: Optional[float]
    precision: Optional[float]
    recall_proxy: Optional[float]
    n: int
    reason: str


def suggest_threshold(stats: Iterable[Tuple[float, int]], target_precision: float = 0.95,
                      min_labels: int = 30) -> ThresholdSuggestion:
    """Lowest score threshold whose precision on reviewed pins is at least
    ``target_precision``.

    ``stats`` is ``[(score, label)]`` with label 1 = approved, 0 = rejected
    (``LearningStore.review_stats``). Candidates are the observed scores.
    Reasons: ``ok``, ``insufficient_labels``, ``no_positives``,
    ``target_not_reached``.
    """
    pairs = sorted(((float(s), 1 if int(l) else 0) for s, l in stats), key=lambda p: -p[0])
    n = len(pairs)
    if not 0.0 < target_precision <= 1.0:
        raise ValueError("target_precision must be in (0, 1]")
    if n < min_labels:
        return ThresholdSuggestion(None, None, None, n, "insufficient_labels")
    total_pos = sum(l for _, l in pairs)
    if total_pos == 0:
        return ThresholdSuggestion(None, None, None, n, "no_positives")
    best: Optional[ThresholdSuggestion] = None
    tp = fp = 0
    i = 0
    while i < n:  # walk distinct scores from high to low
        score = pairs[i][0]
        while i < n and pairs[i][0] == score:
            tp += pairs[i][1]
            fp += 1 - pairs[i][1]
            i += 1
        precision = tp / (tp + fp)
        if precision >= target_precision:
            best = ThresholdSuggestion(score, precision, tp / total_pos, n, "ok")
    return best or ThresholdSuggestion(None, None, None, n, "target_not_reached")


@dataclass(frozen=True)
class CropRecord:
    """A labelled training crop for the detection template bank.

    ``path`` is absolute when the file is written. ``box`` is the symbol box
    in page px (detection box, or the manual box when drawn); ``crop_box`` is
    the crop window in page px, so ``box - crop_box.origin`` locates the
    symbol inside the crop image.
    """

    crop_key: str
    path: Optional[str]
    sha256: Optional[str]
    label: str  # "positive" | "negative"
    class_label: Optional[str]
    status: str
    scan_id: str
    pin_id: str
    origin: str
    canonical_page_id: str
    document_id: str
    box: Optional[Dict[str, float]]
    crop_box: Dict[str, float]
    rotation: Optional[int]
    score: Optional[float]


# ------------------------------------------------------------------- boxes

Edges = Tuple[float, float, float, float]


def iou(a: Edges, b: Edges) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def dedupe_boxes(items: Sequence[Dict[str, Any]], iou_threshold: float = 0.5) -> List[Dict[str, Any]]:
    """Greedy de-duplication of overlapping annotations (the same symbol
    approved in two scans of one page). Keeps the first occurrence and its
    class, so one symbol never gets two boxes."""
    kept: List[Dict[str, Any]] = []
    for it in items:
        if any(iou(k["edges"], it["edges"]) > iou_threshold for k in kept):
            continue
        kept.append(it)
    return kept


def tile_windows(width: int, height: int, size: int, overlap: int = 0) -> List[Edges]:
    """Tiles of ``size`` px with ``overlap``; the last row/column is shifted
    back so every tile lies inside the page (pages smaller than a tile give
    one clipped tile)."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("tile size must be positive and overlap in [0, size)")
    stride = size - overlap

    def starts(extent: int) -> List[int]:
        if extent <= size:
            return [0]
        s = list(range(0, extent - size + 1, stride))
        if s[-1] != extent - size:
            s.append(extent - size)
        return s

    return [(x, y, min(x + size, width), min(y + size, height))
            for y in starts(height) for x in starts(width)]


def clip_to_tile(edges: Edges, tile: Edges, min_visible: float = 0.5) -> Optional[Edges]:
    """Box clipped to the tile, in tile coordinates, or ``None`` when less
    than ``min_visible`` of its area falls inside."""
    x0, y0 = max(edges[0], tile[0]), max(edges[1], tile[1])
    x1, y1 = min(edges[2], tile[2]), min(edges[3], tile[3])
    if x1 <= x0 or y1 <= y0:
        return None
    area = (edges[2] - edges[0]) * (edges[3] - edges[1])
    if area <= 0 or (x1 - x0) * (y1 - y0) / area < min_visible:
        return None
    return (x0 - tile[0], y0 - tile[1], x1 - tile[0], y1 - tile[1])


def image_stem(document_version: str, page_index: int) -> str:
    """File-system-safe image name for a canonical page."""
    return f"{document_version.replace(':', '-')}_p{int(page_index)}"


def build_coco(pages: Sequence[Dict[str, Any]], tile: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """COCO detection JSON from ``pages``.

    Each page: ``canonical_page_id, document_version, page_index, width,
    height`` and ``annotations`` = ``[{"category": str, "edges": (x0, y0, x1, y1),
    "pin": ...}]``. Images are *referenced*, never included: ``file_name`` is
    derived from the page (and tile), and the render service produces the
    pixels from ``canonical_page_id`` at 200 DPI.
    """
    cats = sorted({a["category"] for p in pages for a in p["annotations"]} or {DEFAULT_CLASS_LABEL})
    cat_id = {c: i + 1 for i, c in enumerate(cats)}
    images: List[Dict[str, Any]] = []
    anns: List[Dict[str, Any]] = []
    for p in pages:
        stem = image_stem(p["document_version"], p["page_index"])
        windows = ([(0, 0, p["width"], p["height"])] if tile is None
                   else tile_windows(p["width"], p["height"], tile[0], tile[1]))
        for win in windows:
            img_id = len(images) + 1
            img = {"id": img_id, "width": int(win[2] - win[0]), "height": int(win[3] - win[1]),
                   "canonical_page_id": p["canonical_page_id"],
                   "document_version": p["document_version"], "page_index": p["page_index"]}
            if tile is None:
                img["file_name"] = f"{stem}.png"
            else:
                img["file_name"] = f"{stem}_x{int(win[0])}_y{int(win[1])}.png"
                img["tile"] = {"x": win[0], "y": win[1], "width": win[2] - win[0],
                               "height": win[3] - win[1]}
            images.append(img)
            for a in p["annotations"]:
                e = a["edges"] if tile is None else clip_to_tile(a["edges"], win)
                if e is None:
                    continue
                w, h = e[2] - e[0], e[3] - e[1]
                anns.append({"id": len(anns) + 1, "image_id": img_id,
                             "category_id": cat_id[a["category"]],
                             "bbox": [_n(e[0]), _n(e[1]), _n(w), _n(h)], "area": _n(w * h),
                             "iscrowd": 0, "pinny": a.get("pin", {})})
    return {"info": {"description": "Pinny reviewed receptacle pins",
                     "coordinate_frame": "canonical_raster_px, 200 DPI, origin top-left"},
            "images": images, "annotations": anns,
            "categories": [{"id": cat_id[c], "name": c, "supercategory": "symbol"} for c in cats]}


def _n(v: float):
    v = round(float(v), 3) + 0.0
    return int(v) if v == math.floor(v) else v
