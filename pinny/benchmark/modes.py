"""Run one scan mode on one page and return a contracts section 4 scan
result with the P7 ``mode`` field and per-detection extras."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from pinny.detection import BoundingBox, OpenCVTemplateDetector, ScanSettings, Template

from .dataset import SplitPage
from .errors import BenchmarkError

TEMPLATE = "template"
TEMPLATE_VERIFIER = "template+verifier"
MODEL = "model"
MODES = (TEMPLATE, TEMPLATE_VERIFIER, MODEL)

DETECTOR_NAMES = {
    TEMPLATE: "opencv-template",
    TEMPLATE_VERIFIER: "opencv-template+verifier",
    MODEL: "pinny-point-detector",
}
MODEL_BOX_PX = 40
MAX_POINTS = 500

_SCAN_NS = uuid.UUID("6f1c2b9e-8a44-4f0e-b0f5-2a7d3c9e5b11")


def git_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        sha = ""
    return f"git:{sha}" if sha else "git:unknown"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _frame(page: SplitPage) -> Dict[str, Any]:
    return {"space": "canonical_raster_px", "dpi": 200, "width": page.width, "height": page.height,
            "origin": "top-left", "y_axis": "down"}


def _base_scan(page: SplitPage, mode: str, dataset_id: str, detector: Dict[str, Any],
               template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    scan: Dict[str, Any] = {
        "scan_id": str(uuid.uuid5(_SCAN_NS, json.dumps([dataset_id, mode, page.page_key]))),
        "mode": mode,
        "document": {"document_id": page.document_id, "document_version": page.document_version,
                     "page_index": page.page_index},
        "coordinate_frame": _frame(page),
        "detector": detector,
        "created_at": _now(),
        "detections": [],
    }
    if template is not None:
        scan["template"] = template
    return scan


def _template_candidates(page_rgb: np.ndarray, box: Dict[str, int], settings: ScanSettings):
    template = Template.from_page_crop(page_rgb, BoundingBox(**box))
    result = OpenCVTemplateDetector().detect(page_rgb, template, settings)
    info = {"box": dict(box), "sha256": hashlib.sha256(template.image.tobytes()).hexdigest()}
    return result, info


def _scores(values: Any, n: int, who: str) -> List[float]:
    out = [float(v) for v in values]
    if len(out) != n:
        raise BenchmarkError("bad_model_output", f"{who} returned {len(out)} scores for {n} points.")
    return out


def run_mode(mode: str, page: SplitPage, page_rgb: np.ndarray, *, dataset_id: str,
             template_box: Optional[Dict[str, int]], settings: ScanSettings,
             template_version: str, verifier: Any = None, detector: Any = None) -> Dict[str, Any]:
    if mode == TEMPLATE:
        det_block = {"name": DETECTOR_NAMES[mode], "version": template_version,
                     "settings": settings.to_dict()}
        if template_box is None:
            return _base_scan(page, mode, dataset_id, det_block, None)
        result, tinfo = _template_candidates(page_rgb, template_box, settings)
        scan = _base_scan(page, mode, dataset_id, det_block, tinfo)
        for n, c in enumerate(result.candidates, start=1):
            cx, cy = c.center
            scan["detections"].append({"id": f"det-{n}", "box": c.box.to_dict(), "x": cx, "y": cy,
                                       "score": float(c.score), "rotation": c.rotation,
                                       "source": "detector"})
        scan["truncated"] = result.truncated
        return scan

    if mode == TEMPLATE_VERIFIER:
        if verifier is None:
            raise BenchmarkError("missing_model", "Mode 'template+verifier' needs --verifier.")
        threshold = float(verifier.threshold)
        det_block = {"name": DETECTOR_NAMES[mode], "version": str(verifier.model_id),
                     "settings": {"template": settings.to_dict(), "template_version": template_version,
                                  "verifier_threshold": threshold}}
        if template_box is None:
            scan = _base_scan(page, mode, dataset_id, det_block, None)
            scan["suppressed"] = []
            return scan
        result, tinfo = _template_candidates(page_rgb, template_box, settings)
        scan = _base_scan(page, mode, dataset_id, det_block, tinfo)
        cands = list(result.candidates)
        vscores = _scores(verifier.score(page_rgb, [c.center for c in cands]), len(cands), "Verifier")
        kept, dropped = [], []
        for c, v in zip(cands, vscores):
            cx, cy = c.center
            item = {"box": c.box.to_dict(), "x": cx, "y": cy, "score": v, "rotation": c.rotation,
                    "template_score": float(c.score), "verifier_score": v, "source": "detector"}
            (kept if v >= threshold else dropped).append(item)
        scan["detections"] = [{"id": f"det-{n}", **d} for n, d in enumerate(kept, start=1)]
        scan["suppressed"] = [{"id": f"sup-{n}", **d} for n, d in enumerate(dropped, start=1)]
        scan["truncated"] = result.truncated
        return scan

    if mode == MODEL:
        if detector is None:
            raise BenchmarkError("missing_model", "Mode 'model' needs --detector.")
        det_block = {"name": DETECTOR_NAMES[mode], "version": str(detector.model_id),
                     "settings": {"threshold": float(detector.threshold), "max_points": MAX_POINTS}}
        scan = _base_scan(page, mode, dataset_id, det_block, None)
        points = detector.detect_points(page_rgb, max_points=MAX_POINTS)
        half = MODEL_BOX_PX / 2
        for n, p in enumerate(points, start=1):
            x, y, s = float(p["x"]), float(p["y"]), float(p["score"])
            scan["detections"].append({
                "id": f"det-{n}",
                "box": {"x": x - half, "y": y - half, "width": MODEL_BOX_PX, "height": MODEL_BOX_PX},
                "x": x, "y": y, "score": s, "rotation": 0, "source": "detector",
            })
        return scan

    raise BenchmarkError("invalid_mode", f"Unknown mode {mode!r}; expected one of {', '.join(MODES)}.")
