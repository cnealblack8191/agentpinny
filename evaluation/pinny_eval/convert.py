"""``from-points``: turn a generic point CSV into ``pinny.ground_truth`` files.

Meant for bootstrapping references from labelled public datasets (for
example symbol centres exported from FloorPlanCAD's socket classes). It is a
stub: one CSV in, one ground-truth file per (document, page) out.

CSV columns (header row required):

* required: ``x``, ``y``, ``page`` (0-based), ``document`` (a document_id)
* optional: ``id``, ``document_version``, ``width``, ``height``

Missing optional columns fall back to ``--document-version``, ``--width`` and
``--height``. ``--scale`` multiplies x and y (source units -> canonical 200-DPI
pixels). Every file written is re-validated with the normal loader, so
anything this writes is accepted by ``validate``/``score``.

Labels from an external dataset were made without Pinny's detector, so the
files say ``independent_of_detector: true``. Status defaults to ``unverified``:
someone still has to check that the dataset's notion of "receptacle" and the
exact point marked match Pinny's labelling rules before calling it verified.
"""

from __future__ import annotations

import csv
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .inputs import CANONICAL_DPI, GROUND_TRUTH_FORMAT, InputError, load_ground_truth


def _num(row: Dict[str, str], key: str, line: int) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        raise InputError(f"CSV line {line}: '{key}' must be a number (got {row.get(key)!r})")


def from_points(
    csv_path: str,
    out_dir: str,
    *,
    dataset_id: str,
    labeled_by: str,
    status: str = "unverified",
    document_version: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    scale: float = 1.0,
    notes: str = "",
) -> List[str]:
    if status not in ("unverified", "verified", "synthetic"):
        raise InputError("from-points writes independent labels: --status must be unverified, verified or synthetic")
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            missing = {"x", "y", "page", "document"} - set(reader.fieldnames or [])
            if missing:
                raise InputError(f"{csv_path}: missing CSV column(s) {sorted(missing)}")
            rows = list(reader)
    except OSError as exc:
        raise InputError(f"{csv_path}: cannot read ({exc})") from exc

    pages: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for n, row in enumerate(rows, start=2):
        doc = (row.get("document") or "").strip()
        if not doc:
            raise InputError(f"CSV line {n}: 'document' is empty")
        page = int(_num(row, "page", n))
        key = (doc, page)
        version = (row.get("document_version") or "").strip() or document_version
        w = int(_num(row, "width", n)) if (row.get("width") or "").strip() else width
        h = int(_num(row, "height", n)) if (row.get("height") or "").strip() else height
        if not version or not w or not h:
            raise InputError(f"CSV line {n}: need document_version, width and height "
                             "(as columns or --document-version/--width/--height)")
        entry = pages.setdefault(key, {"version": version, "width": w, "height": h, "points": []})
        if (entry["version"], entry["width"], entry["height"]) != (version, w, h):
            raise InputError(f"CSV line {n}: document '{doc}' page {page} has inconsistent version/size")
        pid = (row.get("id") or "").strip() or f"r{len(entry['points']) + 1:04d}"
        entry["points"].append({"id": pid, "x": _num(row, "x", n) * scale, "y": _num(row, "y", n) * scale})

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for (doc, page), entry in sorted(pages.items()):
        data = {
            "format": GROUND_TRUTH_FORMAT,
            "format_version": 1,
            "dataset": {
                "dataset_id": f"{dataset_id}:{doc}:p{page}",
                "labeled_by": labeled_by,
                "verification": {
                    "status": status,
                    "independent_of_detector": True,
                    "method": f"converted from point CSV {os.path.basename(csv_path)} (scale {scale:g})",
                    "reviewed_by": None,
                },
                "notes": notes,
            },
            "document": {"document_id": doc, "document_version": entry["version"], "page_index": page},
            "coordinate_frame": {"space": "canonical_raster_px", "dpi": CANONICAL_DPI, "width": entry["width"],
                                 "height": entry["height"], "origin": "top-left", "y_axis": "down"},
            "receptacles": entry["points"],
        }
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doc)
        path = os.path.join(out_dir, f"{safe}_p{page}.ground_truth.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        load_ground_truth(path)  # raises InputError on anything invalid (e.g. out-of-raster points)
        written.append(path)
    return written
