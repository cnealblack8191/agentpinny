"""Command-line interface.

  score     Score original detector results against a separately supplied reference.
  pending   Record detector provenance for a page that has no verified reference yet.
  validate  Check a ground-truth file (e.g. after manual labelling).
  adapt-scan  Convert a contracts section 4 scan result into a pinny.detections v1 file.

Exit codes: 0 success, 2 rejected input (mismatch, malformed, corrected pins), 64 usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from typing import List, Optional

from .inputs import Identity, InputError, check_consistency, load_detections, load_ground_truth
from .report import build_report, render_markdown
from .scan_adapter import scan_result_to_detections

EXIT_REJECTED = 2
EXIT_USAGE = 64


def _tolerance(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("tolerance must be a number")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("tolerance must be finite and >= 0")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _nonneg_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _add_identity_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("expected identity (all required; both inputs must agree)")
    g.add_argument("--document-id", required=True)
    g.add_argument("--document-version", required=True, help="e.g. the content hash of the uploaded file")
    g.add_argument("--page", required=True, type=_nonneg_int, help="0-based page index")
    g.add_argument("--width", required=True, type=_positive_int, help="canonical raster width in px")
    g.add_argument("--height", required=True, type=_positive_int, help="canonical raster height in px")


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json-out", help="write the JSON report here")
    p.add_argument("--md-out", help="write the Markdown report here")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pinny_eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("score", help="score detector results against a reference")
    s.add_argument("--detections", required=True, help="pinny.detections v1 file (original detector output)")
    s.add_argument("--ground-truth", required=True, help="pinny.ground_truth v1 file")
    s.add_argument("--tolerance-px", required=True, type=_tolerance,
                   help="match radius in canonical raster pixels (explicit, no default)")
    _add_identity_args(s)
    _add_output_args(s)

    pd = sub.add_parser("pending", help="record provenance; accuracy not measured")
    pd.add_argument("--detections", required=True)
    _add_identity_args(pd)
    _add_output_args(pd)

    v = sub.add_parser("validate", help="validate a ground-truth file")
    v.add_argument("--ground-truth", required=True)

    a = sub.add_parser("adapt-scan", help="contracts section 4 scan result -> pinny.detections v1")
    a.add_argument("--scan", required=True, help="scan result JSON (docs/contracts.md section 4)")
    a.add_argument("--out", required=True, help="pinny.detections v1 file to write")
    a.add_argument("--overwrite", action="store_true", help="replace --out if it already exists")
    return parser


def _emit(report: dict, args: argparse.Namespace) -> None:
    text_json = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    md = render_markdown(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text_json)
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md)
    if not args.json_out and not args.md_out:
        sys.stdout.write(md)
    else:
        sys.stdout.write(f"Real-drawing accuracy: {report['status']['real_drawing_accuracy']}\n")


def _adapt_scan(args: argparse.Namespace) -> int:
    if os.path.exists(args.out) and not args.overwrite:
        raise InputError(f"{args.out}: already exists (pass --overwrite to replace it)")
    try:
        with open(args.scan, "rb") as fh:
            scan = json.loads(fh.read().decode("utf-8"))
    except OSError as exc:
        raise InputError(f"{args.scan}: cannot read file ({exc})") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{args.scan}: not valid UTF-8 JSON ({exc})") from exc
    detections = scan_result_to_detections(scan, where=args.scan)

    # Write next to the target, validate with the same loader `score` uses,
    # and only then move it into place, so a bad scan never leaves a file behind.
    out_dir = os.path.dirname(os.path.abspath(args.out))
    fd, tmp = tempfile.mkstemp(prefix=".adapt-", suffix=".json", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(detections, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        loaded = load_detections(tmp)
        os.replace(tmp, args.out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(
        f"Wrote {args.out}: scan '{loaded.scan_id}', {len(loaded.points)} detections, "
        f"document '{loaded.identity.document_id}' version '{loaded.identity.document_version}' "
        f"page {loaded.identity.page_index}, raster {loaded.frame.width}x{loaded.frame.height} at "
        f"{loaded.frame.dpi} DPI"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0

    try:
        if args.command == "validate":
            gt = load_ground_truth(args.ground_truth)
            print(
                f"OK: dataset '{gt.dataset['dataset_id']}', {len(gt.points)} receptacles, "
                f"document '{gt.identity.document_id}' version '{gt.identity.document_version}' "
                f"page {gt.identity.page_index}, raster {gt.frame.width}x{gt.frame.height}, "
                f"verification '{gt.verification_status}'"
            )
            return 0

        if args.command == "adapt-scan":
            return _adapt_scan(args)

        identity = Identity(args.document_id, args.document_version, args.page)
        det = load_detections(args.detections)
        if args.command == "pending":
            check_consistency(identity, args.width, args.height, det, None)
            _emit(build_report(det, None, None), args)
            return 0

        gt = load_ground_truth(args.ground_truth)
        check_consistency(identity, args.width, args.height, det, gt)
        _emit(build_report(det, gt, args.tolerance_px), args)
        return 0
    except InputError as exc:
        sys.stderr.write(f"REJECTED: {exc}\n")
        return EXIT_REJECTED
