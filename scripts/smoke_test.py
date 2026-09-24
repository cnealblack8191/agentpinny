"""Smoke-test the receptacle detector on one page of a real PDF.

This is a stand-in until the foundation's render service lands
(docs/contracts.md section 9). It renders the page at the canonical 200 DPI
with PyMuPDF, crops a template you point at, runs the detector and writes:

  page.png         the canonical RGB raster
  grid.png         the raster with a labelled pixel grid (use it to find a box)
  template.png     the cropped symbol
  overlay.png      the raster with every detection boxed and scored
  result.json      the raw DetectionResult
  detections.json  a ``pinny.detections`` v1 export the evaluator can score

Usage:
  # 1. Render only, then open grid.png and find one receptacle symbol.
  python scripts/smoke_test.py drawing.pdf --page 0

  # 2. Scan with a tight box around that symbol (canonical px).
  python scripts/smoke_test.py drawing.pdf --page 0 --template-box 1830,2210,42,42

Requires: pip install -e ".[smoke]"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pinny.detection import (  # noqa: E402
    ALLOWED_ROTATIONS,
    DEFAULT_SCORE_THRESHOLD,
    BoundingBox,
    DetectionError,
    OpenCVTemplateDetector,
    ScanSettings,
    Template,
)

CANONICAL_DPI = 200
GRID_STEP_PX = 250


def render_page(pdf_path: Path, page_index: int) -> tuple:
    """Return (document_version, RGB uint8 raster) at the canonical DPI.

    PyMuPDF applies the page's /Rotate, so the raster matches what a viewer
    shows upright.
    """
    import pymupdf

    data = pdf_path.read_bytes()
    version = "sha256:" + hashlib.sha256(data).hexdigest()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if not 0 <= page_index < doc.page_count:
            raise SystemExit(
                f"--page {page_index} is out of range; {pdf_path.name} has "
                f"{doc.page_count} page(s) (0-based)."
            )
        pix = doc[page_index].get_pixmap(dpi=CANONICAL_DPI, alpha=False, colorspace=pymupdf.csRGB)
        raster = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
    return version, raster


def parse_box(text: str) -> BoundingBox:
    try:
        x, y, w, h = (int(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected x,y,width,height as integers, e.g. 1830,2210,42,42")
    return BoundingBox(x, y, w, h)


def parse_rotations(text: str) -> tuple:
    try:
        return tuple(int(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected a comma list drawn from 0,90,180,270")


def write_png(path: Path, rgb: np.ndarray) -> None:
    if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
        raise SystemExit(f"Could not write {path}.")


def draw_grid(rgb: np.ndarray) -> np.ndarray:
    out = rgb.copy()
    h, w = out.shape[:2]
    for x in range(0, w, GRID_STEP_PX):
        cv2.line(out, (x, 0), (x, h - 1), (0, 160, 255), 1)
        cv2.putText(out, str(x), (x + 3, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 90, 200), 2)
    for y in range(0, h, GRID_STEP_PX):
        cv2.line(out, (0, y), (w - 1, y), (0, 160, 255), 1)
        cv2.putText(out, str(y), (3, y - 5 if y else 60), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 90, 200), 2)
    return out


def draw_overlay(rgb: np.ndarray, candidates, template_box: BoundingBox) -> np.ndarray:
    out = rgb.copy()
    tb = template_box
    cv2.rectangle(out, (tb.x - 4, tb.y - 4), (tb.x2 + 3, tb.y2 + 3), (0, 90, 255), 2)
    for i, c in enumerate(candidates, start=1):
        b = c.box
        cv2.rectangle(out, (b.x, b.y), (b.x2 - 1, b.y2 - 1), (230, 0, 0), 3)
        label = f"{i}:{c.score:.2f}"
        cv2.putText(out, label, (b.x, max(b.y - 6, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (230, 0, 0), 2)
    return out


def git_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return f"git:{sha}"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_export(*, document_id, document_version, page_index, raster, template, settings, result) -> dict:
    """Build a scan result (contracts section 4) as a ``pinny.detections`` v1 export."""
    h, w = raster.shape[:2]
    tb = template.source_box
    settings_dict = {
        "threshold": settings.threshold,
        "rotations": list(settings.rotations),
        "nms_iou_threshold": settings.nms_iou_threshold,
        "duplicate_center_ratio": settings.duplicate_center_ratio,
        "max_candidates": settings.max_candidates,
    }
    detections = []
    for i, c in enumerate(result.candidates, start=1):
        cx, cy = c.center
        detections.append({
            "id": f"det-{i}",
            "box": c.box.to_dict(),
            "x": cx,
            "y": cy,
            "score": c.score,
            "confidence": c.score,
            "rotation": c.rotation,
            "source": "detector",
        })
    return {
        "format": "pinny.detections",
        "format_version": 1,
        "provenance": "original_detector_output",
        "scan_id": str(uuid.uuid4()),
        "document": {
            "document_id": document_id,
            "document_version": document_version,
            "page_index": page_index,
        },
        "coordinate_frame": {
            "space": "canonical_raster_px", "dpi": CANONICAL_DPI, "width": w, "height": h,
            "origin": "top-left", "y_axis": "down",
        },
        "template": {
            "box": tb.to_dict(),
            "sha256": hashlib.sha256(np.ascontiguousarray(template.image).tobytes()).hexdigest(),
        },
        "detector": {"name": result.detector, "version": git_version(), "settings": settings_dict},
        "created_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detections": detections,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("pdf", type=Path, help="path to a PDF drawing")
    p.add_argument("--page", type=int, default=0, help="0-based page index (default 0)")
    p.add_argument("--template-box", type=parse_box,
                   help="x,y,width,height of one symbol in canonical px; omit to render only")
    p.add_argument("--threshold", type=float, default=DEFAULT_SCORE_THRESHOLD,
                   help=f"minimum matching score (default {DEFAULT_SCORE_THRESHOLD})")
    p.add_argument("--rotations", type=parse_rotations, default=ALLOWED_ROTATIONS,
                   help="comma list of quarter turns to search (default 0,90,180,270)")
    p.add_argument("--document-id", default=None,
                   help="document id for the export (default: a new uuid4)")
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "smoke-out",
                   help="output directory (default smoke-out/, git-ignored)")
    args = p.parse_args(argv)

    if not args.pdf.is_file():
        p.error(f"{args.pdf} does not exist")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    version, raster = render_page(args.pdf, args.page)
    h, w = raster.shape[:2]
    write_png(args.out_dir / "page.png", raster)
    write_png(args.out_dir / "grid.png", draw_grid(raster))
    print(f"Rendered page {args.page} at {CANONICAL_DPI} DPI: {w}x{h} px ({version[:19]}...)")

    if args.template_box is None:
        print(f"Open {args.out_dir / 'grid.png'}, find one receptacle symbol, then re-run with "
              "--template-box x,y,width,height (a tight box around the symbol).")
        return 0

    settings = ScanSettings(threshold=args.threshold, rotations=args.rotations)
    try:
        template = Template.from_page_crop(raster, args.template_box)
        write_png(args.out_dir / "template.png", template.image)
        result = OpenCVTemplateDetector().detect(raster, template, settings)
    except DetectionError as exc:
        print(f"Detection failed [{exc.code}]: {exc}", file=sys.stderr)
        return 2

    export = build_export(
        document_id=args.document_id or str(uuid.uuid4()),
        document_version=version,
        page_index=args.page,
        raster=raster,
        template=template,
        settings=settings,
        result=result,
    )
    (args.out_dir / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
    (args.out_dir / "detections.json").write_text(json.dumps(export, indent=2))
    write_png(args.out_dir / "overlay.png", draw_overlay(raster, result.candidates, template.source_box))

    scores = [c.score for c in result.candidates]
    print(f"Found {len(scores)} candidate(s) at threshold {settings.threshold} "
          f"in {result.elapsed_seconds:.1f}s.")
    if scores:
        print(f"Scores: max {max(scores):.3f}, min {min(scores):.3f}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    if result.truncated:
        print("Warning: candidate cap hit; more matches may exist. Raise --threshold.")
    print(f"Wrote overlay.png, detections.json and result.json to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
