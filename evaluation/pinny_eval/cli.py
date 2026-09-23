"""Command-line interface.

  score     Score original detector results against a separately supplied reference.
  pending   Record detector provenance for a page that has no verified reference yet.
  validate  Check a ground-truth file (e.g. after manual labelling).

Exit codes: 0 success, 2 rejected input (mismatch, malformed, corrected pins), 64 usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import List, Optional

from .inputs import Identity, InputError, check_consistency, load_detections, load_ground_truth
from .report import build_report, render_markdown

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
