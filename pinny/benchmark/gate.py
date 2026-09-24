"""The promotion gate (``pinny.promotion`` v1, docs/phase2-contracts.md P9).

A candidate mode is compared with the ``template`` baseline on the same test
pages of one benchmark run. It is recommended for promotion only when every
condition holds:

* recall >= baseline recall;
* precision >= baseline precision - 0.02;
* recall or precision improves by 0.02 or more;
* the test split has >= 3 documents and >= 100 reference points;
* the dataset is not synthetic_only.

Precision and recall are computed from the summed TP/FP/FN as exact fractions,
so a difference of exactly 0.02 passes and 0.0199... does not.
"""

from __future__ import annotations

import datetime as _dt
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional

from .aggregate import exact_ratio, metrics
from .errors import BenchmarkError

PROMOTION_FORMAT = "pinny.promotion"
PROMOTION_FORMAT_VERSION = 1
BASELINE_MODE = "template"
CANDIDATE_KINDS = {"template+verifier": "verifier", "model": "detector"}

MARGIN = Fraction(2, 100)
MIN_DOCUMENTS = 3
MIN_REFERENCE_POINTS = 100
REQUIRED_TOLERANCE_PX = 12
SYNTHETIC_LABEL = "synthetic — not real-drawing accuracy"


def _f(x: Optional[Fraction]) -> Optional[float]:
    return None if x is None else float(x)


def _condition(name: str, passed: bool, value: Any, requirement: str, detail: str) -> Dict[str, Any]:
    return {"name": name, "passed": bool(passed), "value": value, "requirement": requirement,
            "detail": detail}


def _mode(summary: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    modes = summary.get("modes") or {}
    if mode not in modes:
        raise BenchmarkError(
            "mode_not_benchmarked",
            f"The benchmark summary has no results for mode '{mode}' "
            f"(it has: {', '.join(sorted(modes)) or 'none'}). Re-run the benchmark with that mode.",
        )
    return modes[mode]


def build_promotion_report(summary: Mapping[str, Any], candidate_mode: str, *,
                           benchmark_sha256: Optional[str] = None,
                           benchmark_path: Optional[str] = None) -> Dict[str, Any]:
    if summary.get("format") != "pinny.benchmark" or summary.get("format_version") != 1:
        raise BenchmarkError("invalid_benchmark",
                             "The input is not a pinny.benchmark v1 summary (summary.json).")
    if candidate_mode not in CANDIDATE_KINDS:
        raise BenchmarkError(
            "invalid_candidate",
            f"Candidate mode must be one of {sorted(CANDIDATE_KINDS)}; '{candidate_mode}' "
            f"cannot be promoted (the '{BASELINE_MODE}' mode is the baseline).",
        )
    tolerance = summary.get("tolerance_px")
    if tolerance != REQUIRED_TOLERANCE_PX:
        raise BenchmarkError(
            "invalid_tolerance",
            f"The benchmark was scored at {tolerance!r} px; P9 fixes the tolerance at "
            f"{REQUIRED_TOLERANCE_PX} px. Re-run the benchmark.",
        )
    base = _mode(summary, BASELINE_MODE)
    cand = _mode(summary, candidate_mode)
    base_pages = [p["page_key"] for p in base["pages"]]
    cand_pages = [p["page_key"] for p in cand["pages"]]
    if base_pages != cand_pages:
        raise BenchmarkError(
            "page_mismatch",
            "The candidate and the baseline were not scored on the same pages; they cannot be compared.",
        )

    kind = CANDIDATE_KINDS[candidate_mode]
    model_id = (cand.get("model_ids") or {}).get(kind)
    if not model_id:
        raise BenchmarkError("missing_model_id",
                             f"The '{candidate_mode}' results do not name a {kind} model_id.")

    def pr(counts: Mapping[str, int]):
        tp, fp, fn = counts["true_positives"], counts["false_positives"], counts["false_negatives"]
        return exact_ratio(tp, tp + fp), exact_ratio(tp, tp + fn)

    bp, br = pr(base["counts"])
    cp, cr = pr(cand["counts"])
    dataset = summary["dataset"]
    synthetic = bool(dataset.get("synthetic_only"))
    docs = int(summary["document_count"])
    refs = int(summary["reference_points"])

    conditions: List[Dict[str, Any]] = []
    defined = None not in (bp, br, cp, cr)

    recall_ok = defined and cr >= br
    conditions.append(_condition(
        "recall_not_below_baseline", recall_ok,
        {"candidate": _f(cr), "baseline": _f(br), "delta": _f(cr - br) if defined else None},
        "candidate recall >= baseline recall",
        "ok" if recall_ok else ("undefined recall or precision" if not defined else "recall dropped"),
    ))
    precision_ok = defined and cp >= bp - MARGIN
    conditions.append(_condition(
        "precision_within_margin", precision_ok,
        {"candidate": _f(cp), "baseline": _f(bp), "delta": _f(cp - bp) if defined else None},
        "candidate precision >= baseline precision - 0.02",
        "ok" if precision_ok else ("undefined recall or precision" if not defined
                                   else "precision dropped by more than 0.02"),
    ))
    improved = defined and (cr - br >= MARGIN or cp - bp >= MARGIN)
    conditions.append(_condition(
        "meaningful_improvement", improved,
        {"recall_delta": _f(cr - br) if defined else None,
         "precision_delta": _f(cp - bp) if defined else None},
        "recall or precision improves by >= 0.02",
        "ok" if improved else ("undefined recall or precision" if not defined
                               else "neither metric improved by 0.02"),
    ))
    conditions.append(_condition(
        "min_test_documents", docs >= MIN_DOCUMENTS, docs, f">= {MIN_DOCUMENTS} test documents",
        "ok" if docs >= MIN_DOCUMENTS else "too few test documents",
    ))
    conditions.append(_condition(
        "min_reference_points", refs >= MIN_REFERENCE_POINTS, refs,
        f">= {MIN_REFERENCE_POINTS} reference points in the test split",
        "ok" if refs >= MIN_REFERENCE_POINTS else "too few reference points",
    ))
    conditions.append(_condition(
        "not_synthetic_only", not synthetic, synthetic, "dataset is not synthetic_only",
        "ok" if not synthetic else SYNTHETIC_LABEL,
    ))

    promote = all(c["passed"] for c in conditions)
    return {
        "format": PROMOTION_FORMAT,
        "format_version": PROMOTION_FORMAT_VERSION,
        "model_id": model_id,
        "kind": kind,
        "candidate_mode": candidate_mode,
        "baseline_mode": BASELINE_MODE,
        "promote": promote,
        "dataset_id": dataset["dataset_id"],
        "split": summary["split"],
        "synthetic_only": synthetic,
        "label": SYNTHETIC_LABEL if synthetic else summary.get("label"),
        "tolerance_px": tolerance,
        "page_count": summary["page_count"],
        "document_count": docs,
        "reference_points": refs,
        "model_ids": {"baseline": dict(base.get("model_ids") or {}),
                      "candidate": dict(cand.get("model_ids") or {})},
        "baseline": {"counts": dict(base["counts"]), "metrics": metrics(base["counts"])},
        "candidate": {"counts": dict(cand["counts"]), "metrics": metrics(cand["counts"])},
        "conditions": conditions,
        "failed_conditions": [c["name"] for c in conditions if not c["passed"]],
        "benchmark": {"path": benchmark_path, "sha256": benchmark_sha256,
                      "created_at": summary.get("created_at")},
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"),
    }


def render_promotion_markdown(report: Mapping[str, Any]) -> str:
    verdict = "PROMOTE" if report["promote"] else "DO NOT PROMOTE"
    lines = [
        f"# Promotion report: {report['model_id']}",
        "",
        f"**{verdict}** — `{report['candidate_mode']}` vs `{report['baseline_mode']}` baseline",
        "",
    ]
    if report["synthetic_only"]:
        lines += [f"> {SYNTHETIC_LABEL}", ""]
    lines += [
        f"- Dataset: `{report['dataset_id']}`, split `{report['split']}`",
        f"- Tolerance: {report['tolerance_px']} px",
        f"- Pages: {report['page_count']}, documents: {report['document_count']}, "
        f"reference points: {report['reference_points']}",
        "",
        "| Mode | TP | FP | FN | Precision | Recall |",
        "|---|---|---|---|---|---|",
    ]
    for label in ("baseline", "candidate"):
        c, m = report[label]["counts"], report[label]["metrics"]
        mode = report["baseline_mode"] if label == "baseline" else report["candidate_mode"]
        lines.append(f"| `{mode}` | {c['true_positives']} | {c['false_positives']} | "
                     f"{c['false_negatives']} | {_fmt(m['precision'])} | {_fmt(m['recall'])} |")
    lines += ["", "| Condition | Passed | Value | Requirement |", "|---|---|---|---|"]
    for c in report["conditions"]:
        lines.append(f"| {c['name']} | {'yes' if c['passed'] else 'no'} | `{_compact(c['value'])}` | "
                     f"{c['requirement']} |")
    return "\n".join(lines) + "\n"


def _fmt(m: Mapping[str, Any]) -> str:
    if m["value"] is None:
        return f"undefined ({m['undefined_reason']})"
    return f"{m['value']:.4f} ({m['numerator']}/{m['denominator']})"


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}={_compact(v)}" for k, v in value.items())
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
