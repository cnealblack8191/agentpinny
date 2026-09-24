"""Bridge to the stdlib evaluator in ``evaluation/pinny_eval``.

``evaluation/`` is standalone and never imports ``pinny`` (contracts section
8). The dependency runs one way: the benchmark imports the evaluator and
scores every page through the same loaders and report builder that
``python -m pinny_eval score`` uses.

The evaluator is found at ``$PINNY_EVAL_DIR`` if set, otherwise at
``<repo>/evaluation`` next to the ``pinny`` package.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from .errors import BenchmarkError


def evaluation_dir() -> Path:
    env = os.environ.get("PINNY_EVAL_DIR")
    root = Path(env) if env else Path(__file__).resolve().parents[2] / "evaluation"
    if not (root / "pinny_eval" / "__init__.py").is_file():
        raise BenchmarkError(
            "evaluator_not_found",
            f"The stdlib evaluator was not found at {root}. Run from a repository checkout "
            "or set PINNY_EVAL_DIR to the evaluation/ directory.",
        )
    return root


def load_evaluator() -> SimpleNamespace:
    root = str(evaluation_dir())
    if root not in sys.path:
        sys.path.insert(0, root)
    pkg = importlib.import_module("pinny_eval")
    return SimpleNamespace(
        version=pkg.__version__,
        inputs=importlib.import_module("pinny_eval.inputs"),
        report=importlib.import_module("pinny_eval.report"),
        scan_adapter=importlib.import_module("pinny_eval.scan_adapter"),
    )


def adapt_scan(scan: Dict[str, Any]) -> Dict[str, Any]:
    """Contracts section 4 scan result -> ``pinny.detections`` v1."""
    ev = load_evaluator()
    try:
        return ev.scan_adapter.scan_result_to_detections(scan, where=scan.get("scan_id", "scan"))
    except ev.inputs.InputError as exc:
        raise BenchmarkError("evaluator_rejected", str(exc)) from exc


def score_files(detections_path: Path, ground_truth_path: Path, *, document_id: str,
                document_version: str, page_index: int, width: int, height: int,
                tolerance_px: float) -> Dict[str, Any]:
    """Score one page exactly as ``pinny_eval score`` does and return the report."""
    ev = load_evaluator()
    try:
        det = ev.inputs.load_detections(str(detections_path))
        gt = ev.inputs.load_ground_truth(str(ground_truth_path))
        identity = ev.inputs.Identity(document_id, document_version, page_index)
        ev.inputs.check_consistency(identity, width, height, det, gt)
        return ev.report.build_report(det, gt, tolerance_px)
    except ev.inputs.InputError as exc:
        raise BenchmarkError("evaluator_rejected", str(exc)) from exc


def render_page_markdown(report: Dict[str, Any]) -> str:
    return load_evaluator().report.render_markdown(report)
