"""Command-line interface.

  score         Score original detector results against a separately supplied reference.
  score-corpus  Score every page listed in a manifest and pool the results.
  pending       Record detector provenance for a page that has no verified reference yet.
  validate      Check a ground-truth file (e.g. after manual labelling).
  from-points   Convert a generic x,y,page,document CSV into ground-truth files.

Exit codes:
  0   success (report written; baseline gate passed or not requested)
  2   rejected input (mismatch, malformed, corrected pins, tolerance/baseline mismatch)
  3   baseline regression: AP, recall or F1 dropped by more than --max-drop
      (reports are still written)
  64  usage error
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Callable, Dict, List, Optional

from .baseline import compare, load_baseline, make_baseline
from .convert import from_points
from .corpus import build_corpus_report, load_manifest, render_corpus_markdown, resolve_tolerance
from .inputs import Identity, InputError, check_consistency, load_detections, load_ground_truth
from .report import build_report, render_markdown
from .tolerance import Tolerance

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_REGRESSION = 3
EXIT_USAGE = 64
DEFAULT_MAX_DROP = 0.01


def _nonneg_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return value


_tolerance = _nonneg_float


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


def _add_tolerance_args(p: argparse.ArgumentParser, where: str) -> None:
    p.add_argument("--tolerance-px", type=_tolerance,
                   help=f"match radius in canonical raster pixels (no default; {where})")
    p.add_argument("--tolerance-rel", type=_tolerance,
                   help="match radius as a fraction of each detection box's short side; with "
                        "--tolerance-px too, the pixel radius is used for detections without a box")


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json-out", help="write the JSON report here")
    p.add_argument("--md-out", help="write the Markdown report here")


def _add_baseline_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--baseline", help="baseline file (pinny.eval_baseline or an earlier report JSON); "
                                      "exit 3 if AP/recall/F1 drop by more than --max-drop")
    p.add_argument("--max-drop", type=_nonneg_float, default=DEFAULT_MAX_DROP,
                   help=f"allowed absolute drop per metric (default {DEFAULT_MAX_DROP})")
    p.add_argument("--write-baseline", help="write this run's gated metrics as a baseline file")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pinny_eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("score", help="score detector results against a reference")
    s.add_argument("--detections", required=True, help="pinny.detections v1 file (original detector output)")
    s.add_argument("--ground-truth", required=True, help="pinny.ground_truth v1 file")
    _add_tolerance_args(s, "at least one of --tolerance-px/--tolerance-rel is required")
    _add_identity_args(s)
    _add_baseline_args(s)
    _add_output_args(s)

    c = sub.add_parser("score-corpus", help="score all pages in a manifest and pool the results")
    c.add_argument("--manifest", required=True, help="pinny.eval_manifest v1 file")
    _add_tolerance_args(c, "falls back to the manifest's value")
    c.add_argument("--worst", type=_nonneg_int, default=5, help="number of worst pages to list (default 5)")
    _add_baseline_args(c)
    _add_output_args(c)

    pd = sub.add_parser("pending", help="record provenance; accuracy not measured")
    pd.add_argument("--detections", required=True)
    _add_identity_args(pd)
    _add_output_args(pd)

    v = sub.add_parser("validate", help="validate a ground-truth file")
    v.add_argument("--ground-truth", required=True)

    fp = sub.add_parser("from-points", help="convert an x,y,page,document CSV into ground-truth files")
    fp.add_argument("--csv", required=True)
    fp.add_argument("--out-dir", required=True)
    fp.add_argument("--dataset-id", required=True)
    fp.add_argument("--labeled-by", required=True, help="who made the source labels (e.g. dataset name)")
    fp.add_argument("--status", default="unverified", choices=["unverified", "verified", "synthetic"])
    fp.add_argument("--document-version", help="used when the CSV has no document_version column")
    fp.add_argument("--width", type=_positive_int, help="used when the CSV has no width column")
    fp.add_argument("--height", type=_positive_int, help="used when the CSV has no height column")
    fp.add_argument("--scale", type=_nonneg_float, default=1.0, help="multiply x, y by this (default 1)")
    fp.add_argument("--notes", default="", help="labelling rules / class mapping, copied into dataset.notes")
    return parser


def _emit(report: Dict[str, Any], args: argparse.Namespace, render: Callable[[Dict[str, Any]], str]) -> None:
    text_json = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    md = render(report)
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
        for c in report["status"].get("caveats") or []:
            sys.stdout.write(f"CAVEAT: {c}\n")


def _gate(report: Dict[str, Any], args: argparse.Namespace) -> int:
    """Attach the baseline check (if requested) to the report; return the exit code."""
    if args.write_baseline:
        with open(args.write_baseline, "w", encoding="utf-8") as fh:
            json.dump(make_baseline(report), fh, indent=2)
            fh.write("\n")
    if not args.baseline:
        return EXIT_OK
    check = compare(load_baseline(args.baseline), report, args.max_drop, args.baseline)
    report["baseline_check"] = check
    return EXIT_OK if check["passed"] else EXIT_REGRESSION


def _finish(report: Dict[str, Any], args: argparse.Namespace, render) -> int:
    code = _gate(report, args)
    _emit(report, args, render)
    if code == EXIT_REGRESSION:
        failed = [c["metric"] for c in report["baseline_check"]["checks"] if not c["passed"]]
        sys.stderr.write(f"REGRESSION: {', '.join(failed)} dropped by more than {args.max_drop:g} "
                         f"against {args.baseline}\n")
    return code


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "score" and args.tolerance_px is None and args.tolerance_rel is None:
            parser.error("score needs --tolerance-px and/or --tolerance-rel (no default)")
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
            return EXIT_OK

        if args.command == "from-points":
            written = from_points(
                args.csv, args.out_dir, dataset_id=args.dataset_id, labeled_by=args.labeled_by,
                status=args.status, document_version=args.document_version, width=args.width,
                height=args.height, scale=args.scale, notes=args.notes)
            for path in written:
                print(f"wrote {path}")
            return EXIT_OK

        if args.command == "score-corpus":
            manifest = load_manifest(args.manifest)
            tol = resolve_tolerance(manifest, args.tolerance_px, args.tolerance_rel)
            report = build_corpus_report(manifest, tol, worst=args.worst)
            return _finish(report, args, render_corpus_markdown)

        identity = Identity(args.document_id, args.document_version, args.page)
        det = load_detections(args.detections)
        if args.command == "pending":
            check_consistency(identity, args.width, args.height, det, None)
            _emit(build_report(det, None, None), args, render_markdown)
            return EXIT_OK

        gt = load_ground_truth(args.ground_truth)
        check_consistency(identity, args.width, args.height, det, gt)
        report = build_report(det, gt, Tolerance(args.tolerance_px, args.tolerance_rel))
        return _finish(report, args, render_markdown)
    except InputError as exc:
        sys.stderr.write(f"REJECTED: {exc}\n")
        return EXIT_REJECTED
