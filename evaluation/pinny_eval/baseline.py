"""Regression gate: compare a report's AP / recall / F1 with a stored baseline.

A baseline file is either a ``pinny.eval_baseline`` v1 file (written with
``--write-baseline``) or any page/corpus report JSON from this evaluator.
The gated metrics are:

* page report (``score``): curve AP, recall, F1;
* corpus report (``score-corpus``): pooled-curve AP, micro recall, micro F1.

A metric fails when ``baseline - current > max_drop`` (absolute, on the 0..1
scale). A metric that is null in the baseline is skipped; a metric that the
baseline has but the current run cannot compute (e.g. confidences were
dropped so AP is unavailable) fails. The tolerance must be identical, since
scores at different radii are not comparable; a mismatch rejects the run.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .inputs import InputError

BASELINE_FORMAT = "pinny.eval_baseline"
GATED = ("ap", "recall", "f1")
EPS = 1e-12


def gate_metrics(report: Dict[str, Any]) -> Dict[str, Optional[float]]:
    curve = report.get("curve") or {}
    if report.get("format") == "pinny.corpus_report":
        m = report["micro"]
    else:
        m = report.get("metrics") or {}
    return {
        "ap": curve.get("ap") if curve.get("available") else None,
        "recall": (m.get("recall") or {}).get("value"),
        "f1": (m.get("f1") or {}).get("value"),
    }


def _tol(report_or_baseline: Dict[str, Any]) -> Dict[str, Any]:
    m = report_or_baseline.get("matching") or {}
    return {"tolerance_px": m.get("tolerance_px"), "tolerance_rel": m.get("tolerance_rel")}


def make_baseline(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "format": BASELINE_FORMAT,
        "format_version": 1,
        "source_format": report.get("format"),
        "corpus_id": report.get("corpus_id"),
        "matching": _tol(report),
        "metrics": gate_metrics(report),
    }


def load_baseline(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise InputError(f"{path}: cannot read baseline ({exc})") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: baseline must be a JSON object")
    fmt = data.get("format")
    if fmt == BASELINE_FORMAT:
        if data.get("format_version") != 1 or not isinstance(data.get("metrics"), dict):
            raise InputError(f"{path}: unsupported or malformed {BASELINE_FORMAT} file")
        return data
    if fmt in ("pinny.evaluation_report", "pinny.corpus_report"):
        return make_baseline(data)
    raise InputError(f"{path}: format is {fmt!r}; expected {BASELINE_FORMAT} or an evaluator report")


def compare(baseline: Dict[str, Any], report: Dict[str, Any], max_drop: float, path: str) -> Dict[str, Any]:
    if _tol(baseline) != _tol(report):
        raise InputError(
            f"{path}: baseline tolerance {_tol(baseline)} differs from this run's {_tol(report)}; "
            "scores at different tolerances cannot be compared"
        )
    current = gate_metrics(report)
    checks = []
    for key in GATED:
        b = baseline["metrics"].get(key)
        c = current.get(key)
        if b is None:
            checks.append({"metric": key, "baseline": None, "current": c, "drop": None,
                           "passed": True, "result": "skipped (not in baseline)"})
        elif c is None:
            checks.append({"metric": key, "baseline": b, "current": None, "drop": None,
                           "passed": False, "result": "FAIL (not computable in this run)"})
        else:
            drop = b - c
            ok = drop <= max_drop + EPS
            checks.append({"metric": key, "baseline": b, "current": c, "drop": drop,
                           "passed": ok, "result": "pass" if ok else f"FAIL (drop > {max_drop:g})"})
    return {
        "baseline_file": path,
        "max_drop": max_drop,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }
