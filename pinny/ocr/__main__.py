"""Command-line access to the optional OCR module.

    python -m pinny.ocr [--data-dir DIR] engines
    python -m pinny.ocr [--data-dir DIR] set-engine {none,tesseract}
    python -m pinny.ocr [--data-dir DIR] text IMAGE [options]
    python -m pinny.ocr [--data-dir DIR] sheet-info IMAGE [--region X,Y,W,H | --full-page] [options]

IMAGE is a page raster (PNG etc.) at the canonical 200 DPI, or pass --dpi.
``text`` and ``sheet-info`` use the saved engine and refuse to run while OCR
is off.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import registry
from .sheet_info import read_sheet_info
from .types import ALLOWED_ROTATIONS, OcrBox, OcrError, OcrSettings


def _load(path: str):
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - cv2 is a declared dependency
        raise OcrError("ocr_unavailable", "Reading image files needs opencv-python-headless.") from exc
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise OcrError("invalid_page", f"Could not read an image from {path!r}.")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _box(text: str) -> OcrBox:
    try:
        x, y, w, h = (int(v) for v in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected X,Y,W,H in canonical px") from exc
    return OcrBox(x, y, w, h)


def _rotations(text: str):
    try:
        values = tuple(int(v) for v in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a comma list from {ALLOWED_ROTATIONS}") from exc
    return values


def _settings(args) -> OcrSettings:
    return OcrSettings(
        language=args.language,
        dpi=args.dpi,
        upscale_to_dpi=None if args.no_upscale else args.upscale_to_dpi,
        rotations=args.rotations,
        min_confidence=args.min_confidence,
    )


def _engine(args):
    engine = registry.get_engine(data_dir=args.data_dir)
    if engine is None:
        raise OcrError(
            "ocr_disabled",
            "OCR is turned off. Turn it on with 'python -m pinny.ocr set-engine tesseract'.",
        )
    return engine


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pinny.ocr", description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", help="Pinny data directory (default: $PINNY_DATA_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("engines", help="list OCR engines and the current choice")
    p_set = sub.add_parser("set-engine", help="save the OCR engine in the app settings")
    p_set.add_argument("name", choices=registry.engine_names())
    for name in ("text", "sheet-info"):
        p = sub.add_parser(name)
        p.add_argument("image")
        p.add_argument("--language", default="eng")
        p.add_argument("--dpi", type=int, default=200, help="DPI of IMAGE (default 200, the canonical DPI)")
        p.add_argument("--upscale-to-dpi", type=int, default=300)
        p.add_argument("--no-upscale", action="store_true")
        p.add_argument("--rotations", type=_rotations, default=ALLOWED_ROTATIONS)
        p.add_argument("--min-confidence", type=float, default=0.0)
        p.add_argument("--region", type=_box, help="X,Y,W,H in canonical px")
        if name == "sheet-info":
            p.add_argument("--full-page", action="store_true", help="search the whole sheet, not the title-block corner")
    args = parser.parse_args(argv)

    try:
        if args.command == "engines":
            out = registry.engine_options(args.data_dir)
        elif args.command == "set-engine":
            out = {"engine": registry.set_engine(args.name, args.data_dir)}
        elif args.command == "text":
            engine = _engine(args)
            result = engine.recognize(_load(args.image), _settings(args), region=args.region)
            out = {**result.to_dict(), "text": result.text}
        else:
            engine = _engine(args)
            info = read_sheet_info(_load(args.image), engine, _settings(args),
                                   region=args.region, full_page=args.full_page)
            out = info.to_dict()
    except OcrError as err:
        print(json.dumps({"error": {"code": err.code, "message": str(err)}}), file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
