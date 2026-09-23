"""Adapter: contracts section 4 scan result -> `pinny.detections` v1.

Works on the JSON form of the scan result only. It does not import `pinny`
(evaluation/ is standalone and stdlib-only; docs/contracts.md section 8).

Mapping (docs/contracts.md sections 3 and 4):
  * The whole scan object is carried through unchanged (scan_id, document,
    coordinate_frame, template, detector, created_at, and every detection's
    box/score/rotation), plus "format", "format_version": 1 and
    "provenance": "original_detector_output".
  * Each detection's point is its box center, (x + width/2, y + height/2).
    If the scan item also has x/y, they must equal that center (to 1e-6 px),
    or the scan is rejected: a disagreement means the scan does not follow
    the contract, and guessing which one is right would score the wrong point.
  * "score" is copied into "confidence". It remains a raw matching score,
    not a probability.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, List

from .inputs import DETECTIONS_FORMAT, SUPPORTED_VERSION, InputError

CENTER_TOLERANCE_PX = 1e-6


def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputError(f"{where}: must be a finite number")
    return float(value)


def scan_result_to_detections(scan: Any, where: str = "scan") -> Dict[str, Any]:
    if not isinstance(scan, dict):
        raise InputError(f"{where}: top level must be a JSON object")
    if "format" in scan or "provenance" in scan:
        raise InputError(
            f"{where}: already has 'format'/'provenance'. The adapter takes a contracts section 4 "
            "scan result, not a pinny.detections file or a corrected pin set"
        )
    for key in ("scan_id", "document", "coordinate_frame", "detector", "detections"):
        if key not in scan:
            raise InputError(f"{where}: missing required field '{key}' (contracts section 4)")
    items = scan["detections"]
    if not isinstance(items, list):
        raise InputError(f"{where}: detections must be a list")

    out_items: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        iw = f"{where}: detections[{i}]"
        if not isinstance(item, dict):
            raise InputError(f"{iw}: must be an object")
        label = f"{iw} ('{item.get('id')}')"
        source = item.get("source")
        if source != "detector":
            raise InputError(
                f"{label}: source is {source!r}; a scan result contains only 'detector' items. "
                "Manual or corrected pins are not detector output"
            )
        box = item.get("box")
        if not isinstance(box, dict):
            raise InputError(f"{label}: missing 'box' object")
        bx = _num(box.get("x"), f"{label}.box.x")
        by = _num(box.get("y"), f"{label}.box.y")
        bw = _num(box.get("width"), f"{label}.box.width")
        bh = _num(box.get("height"), f"{label}.box.height")
        if bw <= 0 or bh <= 0:
            raise InputError(f"{label}: box width and height must be > 0")
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        for axis, center in (("x", cx), ("y", cy)):
            if axis in item:
                given = _num(item[axis], f"{label}.{axis}")
                if abs(given - center) > CENTER_TOLERANCE_PX:
                    raise InputError(
                        f"{label}: {axis}={given} does not equal the box center {center} "
                        "(contracts section 3: a detection's point is its box center)"
                    )
        if "score" not in item:
            raise InputError(f"{label}: missing 'score'")
        score = _num(item["score"], f"{label}.score")

        new_item = copy.deepcopy(item)
        new_item["x"] = cx
        new_item["y"] = cy
        new_item["confidence"] = score
        out_items.append(new_item)

    body = copy.deepcopy(scan)
    body["detections"] = out_items
    return {
        "format": DETECTIONS_FORMAT,
        "format_version": SUPPORTED_VERSION,
        "provenance": "original_detector_output",
        **body,
    }
