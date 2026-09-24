"""``python -m pinny.benchmark``

  run             Score each scan mode on a dataset's test split (P9).
  promote-report  Apply the P9 promotion gate and write a pinny.promotion v1 report.

Exit codes: 0 report written (including "promote": false), 2 rejected input, 64 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from pinny.errors import PinnyError

from .errors import BenchmarkError
from .gate import CANDIDATE_KINDS, build_promotion_report, render_promotion_markdown
from .modes import MODES
from .run import REFERENCE_STATUSES, run_benchmark

EXIT_REJECTED = 2
EXIT_USAGE = 64


def _read_model_json(model_dir: str) -> Optional[dict]:
    path = Path(model_dir) / "model.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_verifier(model_dir: str) -> Any:
    """Load a verifier through its P6 interface. Imports torch-backed code lazily."""
    from pinny.models.verifier import Verifier

    return Verifier.load(model_dir)


def load_detector(model_dir: str) -> Any:
    from pinny.models.point_detector import PointDetector

    return PointDetector.load(model_dir)


def _modes(text: str) -> List[str]:
    modes = [m.strip() for m in text.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if bad or not modes:
        raise argparse.ArgumentTypeError(f"modes must be a comma list of {', '.join(MODES)}")
    return modes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pinny.benchmark", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="benchmark scan modes on the test split")
    r.add_argument("--dataset", required=True, help="pinny.dataset v1 directory (with manifest.json)")
    r.add_argument("--modes", required=True, type=_modes, help=f"comma list of {', '.join(MODES)}")
    r.add_argument("--verifier", help="verifier model directory (needed for template+verifier)")
    r.add_argument("--detector", help="point detector model directory (needed for model)")
    r.add_argument("--out", required=True, help="output directory")
    r.add_argument("--template-threshold", type=float,
                   help="template matching threshold (default: the detector's default)")
    r.add_argument("--reference-status", choices=REFERENCE_STATUSES,
                   help="'verified' once a second person has checked every test page "
                        "(default: unverified; synthetic datasets are always 'synthetic')")
    r.add_argument("--overwrite", action="store_true", help="allow a non-empty --out")

    p = sub.add_parser("promote-report", help="apply the P9 promotion gate")
    p.add_argument("--benchmark", required=True, help="summary.json from `run`, or its directory")
    p.add_argument("--candidate", required=True, choices=sorted(CANDIDATE_KINDS),
                   help="mode to compare with the template baseline")
    p.add_argument("--out", help="write the pinny.promotion v1 JSON here (default: stdout)")
    p.add_argument("--md-out", help="also write a Markdown rendering here")
    return parser


def _run(args: argparse.Namespace) -> int:
    verifier = load_verifier(args.verifier) if args.verifier else None
    detector = load_detector(args.detector) if args.detector else None
    summary = run_benchmark(
        Path(args.dataset), args.modes, Path(args.out), verifier=verifier, detector=detector,
        verifier_meta=_read_model_json(args.verifier) if args.verifier else None,
        detector_meta=_read_model_json(args.detector) if args.detector else None,
        template_threshold=args.template_threshold, reference_status=args.reference_status,
        overwrite=args.overwrite)
    print(f"Benchmark of dataset {summary['dataset']['dataset_id']} ({summary['label']}), "
          f"{summary['page_count']} test pages, tolerance {summary['tolerance_px']} px:")
    for mode, res in summary["modes"].items():
        m = res["metrics"]
        fmt = lambda v: "undefined" if v is None else f"{v:.4f}"  # noqa: E731
        print(f"  {mode:<18} precision {fmt(m['precision']['value'])}  recall {fmt(m['recall']['value'])}")
    print(f"Wrote {Path(args.out) / 'summary.json'} and summary.md")
    return 0


def _promote(args: argparse.Namespace) -> int:
    path = Path(args.benchmark)
    if path.is_dir():
        path = path / "summary.json"
    try:
        raw = path.read_bytes()
        summary = json.loads(raw.decode("utf-8"))
    except OSError as exc:
        raise BenchmarkError("benchmark_not_found", f"Cannot read {path} ({exc}).") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("invalid_benchmark", f"{path} is not valid JSON ({exc}).") from exc
    report = build_promotion_report(summary, args.candidate,
                                    benchmark_sha256=hashlib.sha256(raw).hexdigest(),
                                    benchmark_path=str(path))
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if args.md_out:
        Path(args.md_out).write_text(render_promotion_markdown(report), encoding="utf-8")
    failed = ", ".join(report["failed_conditions"]) or "none"
    print(f"promote: {str(report['promote']).lower()} (failed conditions: {failed})",
          file=sys.stderr if not args.out else sys.stdout)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    try:
        if args.command == "run":
            return _run(args)
        return _promote(args)
    except PinnyError as exc:
        sys.stderr.write(f"REJECTED [{exc.code}]: {exc.message}\n")
        return EXIT_REJECTED
