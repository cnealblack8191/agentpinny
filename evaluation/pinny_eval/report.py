"""Build evaluation reports (JSON-serialisable dict + Markdown rendering)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import __version__
from .inputs import Detections, GroundTruth
from .matching import Point, distance, match

REPORT_FORMAT = "pinny.evaluation_report"
NOT_MEASURED = "Not measured — verified reference pending"


def _ratio(num: int, den: int, undefined_reason: str) -> Dict[str, Any]:
    """Zero-denominator rule: value is null (never 0 or 1) and a reason is given."""
    if den == 0:
        return {"value": None, "numerator": num, "denominator": den, "undefined_reason": undefined_reason}
    return {"value": num / den, "numerator": num, "denominator": den, "undefined_reason": None}


def _accuracy_status(gt: Optional[GroundTruth]) -> Dict[str, Any]:
    if gt is None:
        return {
            "real_drawing_accuracy": NOT_MEASURED,
            "scope": "no_reference",
            "note": "No reference file was supplied. Detector output is recorded with its provenance only.",
        }
    status = gt.verification_status
    if status == "verified":
        return {
            "real_drawing_accuracy": "Measured against verified reference",
            "scope": "verified_reference",
            "note": (
                f"Scores apply only to dataset '{gt.dataset['dataset_id']}' "
                "(this page, this detector build and settings, this tolerance)."
            ),
        }
    if status == "synthetic":
        return {
            "real_drawing_accuracy": NOT_MEASURED,
            "scope": "synthetic_fixture",
            "note": (
                "Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing "
                "about how the detector performs on real drawings."
            ),
        }
    return {
        "real_drawing_accuracy": NOT_MEASURED,
        "scope": "unverified_reference",
        "note": (
            "The reference is labelled 'unverified'. The counts below show agreement with an unverified "
            "reference and must not be reported as precision/recall on real drawings."
        ),
    }


def _pt(p: Point) -> Dict[str, Any]:
    return {"id": p.id, "x": p.x, "y": p.y}


def build_report(
    detections: Detections,
    ground_truth: Optional[GroundTruth],
    tolerance_px: Optional[float],
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "format": REPORT_FORMAT,
        "format_version": 1,
        "evaluator": {"name": "pinny_eval", "version": __version__},
        "status": _accuracy_status(ground_truth),
        "document": detections.identity.as_dict(),
        "coordinate_frame": detections.frame.as_dict(),
        "detector_provenance": {
            "scan_id": detections.scan_id,
            "detector": detections.detector,
            "results_file": detections.path,
            "results_sha256": detections.sha256,
            "provenance": "original_detector_output",
        },
        "dataset": None,
        "matching": None,
        "counts": None,
        "metrics": None,
        "matched": [],
        "false_positives": [],
        "false_negatives": [],
    }
    if ground_truth is None:
        report["counts"] = {"predictions": len(detections.points)}
        return report

    assert tolerance_px is not None
    report["dataset"] = {
        "dataset_id": ground_truth.dataset["dataset_id"],
        "labeled_by": ground_truth.dataset["labeled_by"],
        "labeled_at": ground_truth.dataset.get("labeled_at"),
        "verification": ground_truth.dataset["verification"],
        "reference_file": ground_truth.path,
        "reference_sha256": ground_truth.sha256,
    }
    report["matching"] = {
        "tolerance_px": tolerance_px,
        "tolerance_units": "canonical raster pixels",
        "distance": "euclidean",
        "inclusive": True,
        "algorithm": "maximum-cardinality one-to-one matching; minimum total distance as tie-breaker",
    }

    preds = detections.points
    refs = ground_truth.points
    pairs = match(preds, refs, tolerance_px)
    pred_by_id = {p.id: p for p in preds}
    ref_by_id = {r.id: r for r in refs}
    matched_pred = {pr.prediction_id: pr.reference_id for pr in pairs}
    matched_ref = {pr.reference_id: pr.prediction_id for pr in pairs}

    tp = len(pairs)
    fp = len(preds) - tp
    fn = len(refs) - tp
    report["counts"] = {
        "predictions": len(preds),
        "references": len(refs),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }
    report["metrics"] = {
        "precision": _ratio(tp, len(preds), "no predictions (TP + FP = 0)"),
        "recall": _ratio(tp, len(refs), "no reference receptacles (TP + FN = 0)"),
    }

    report["matched"] = [
        {
            "prediction": _pt(pred_by_id[pr.prediction_id]),
            "reference": _pt(ref_by_id[pr.reference_id]),
            "distance_px": pr.distance,
        }
        for pr in pairs
    ]

    fps: List[Dict[str, Any]] = []
    for p in sorted(preds, key=lambda q: q.id):
        if p.id in matched_pred:
            continue
        near = sorted(
            ((distance(p, r), r.id) for r in refs if distance(p, r) <= tolerance_px),
            key=lambda t: (t[0], t[1]),
        )
        if near:
            reason = "duplicate_or_crowded: every reference within tolerance is matched to another prediction"
        else:
            reason = "no_reference_within_tolerance"
        fps.append(
            {
                **_pt(p),
                "reason": reason,
                "references_within_tolerance": [
                    {"id": rid, "distance_px": d, "matched_to": matched_ref.get(rid)} for d, rid in near
                ],
            }
        )
    report["false_positives"] = fps

    fns: List[Dict[str, Any]] = []
    for r in sorted(refs, key=lambda q: q.id):
        if r.id in matched_ref:
            continue
        near = sorted(
            ((distance(p, r), p.id) for p in preds if distance(p, r) <= tolerance_px),
            key=lambda t: (t[0], t[1]),
        )
        fns.append(
            {
                **_pt(r),
                "reason": (
                    "no_prediction_within_tolerance"
                    if not near
                    else "crowded: every prediction within tolerance is matched to another reference"
                ),
                "predictions_within_tolerance": [
                    {"id": pid, "distance_px": d, "matched_to": matched_pred.get(pid)} for d, pid in near
                ],
            }
        )
    report["false_negatives"] = fns
    return report


def _fmt_metric(m: Dict[str, Any]) -> str:
    if m["value"] is None:
        return f"undefined ({m['undefined_reason']})"
    return f"{m['value']:.4f} ({m['numerator']}/{m['denominator']})"


def _fmt_xy(p: Dict[str, Any]) -> str:
    return f"({p['x']:g}, {p['y']:g})"


def render_markdown(report: Dict[str, Any]) -> str:
    st = report["status"]
    doc = report["document"]
    fr = report["coordinate_frame"]
    prov = report["detector_provenance"]
    lines = [
        "# Pinny detector evaluation report",
        "",
        f"**Real-drawing accuracy:** {st['real_drawing_accuracy']}",
        "",
        f"> {st['note']}",
        "",
        "## Identity",
        "",
        f"- Document: `{doc['document_id']}` version `{doc['document_version']}`, page index {doc['page_index']}",
        f"- Coordinate frame: {fr['space']}, {fr['width']}x{fr['height']}, origin {fr['origin']}, y {fr['y_axis']}",
        f"- Scan: `{prov['scan_id']}`",
        f"- Detector: `{prov['detector']['name']}` version `{prov['detector']['version']}`",
        f"- Detector settings: `{_compact(prov['detector']['settings'])}`",
        f"- Detector results: `{prov['results_file']}` (sha256 `{prov['results_sha256'][:16]}…`), "
        "original detector output only (manual corrections are not scored)",
    ]
    ds = report["dataset"]
    if ds is None:
        lines += ["", f"Predictions recorded: {report['counts']['predictions']}. No scoring performed.", ""]
        return "\n".join(lines)

    m = report["matching"]
    c = report["counts"]
    lines += [
        f"- Reference dataset: `{ds['dataset_id']}` labelled by {ds['labeled_by']}"
        + (f" on {ds['labeled_at']}" if ds.get("labeled_at") else "")
        + f"; verification status **{ds['verification']['status']}**",
        f"- Reference file: `{ds['reference_file']}` (sha256 `{ds['reference_sha256'][:16]}…`)",
        "",
        "## Matching",
        "",
        f"- Tolerance: {m['tolerance_px']:g} {m['tolerance_units']} (Euclidean, inclusive)",
        f"- Method: {m['algorithm']}",
        "",
        "## Results",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Predictions | {c['predictions']} |",
        f"| Reference receptacles | {c['references']} |",
        f"| True positives | {c['true_positives']} |",
        f"| False positives | {c['false_positives']} |",
        f"| False negatives (misses) | {c['false_negatives']} |",
        f"| Precision | {_fmt_metric(report['metrics']['precision'])} |",
        f"| Recall | {_fmt_metric(report['metrics']['recall'])} |",
        "",
        "### Matched",
        "",
    ]
    if report["matched"]:
        lines += ["| Prediction | At | Reference | At | Distance px |", "|---|---|---|---|---|"]
        for row in report["matched"]:
            lines.append(
                f"| {row['prediction']['id']} | {_fmt_xy(row['prediction'])} | {row['reference']['id']} "
                f"| {_fmt_xy(row['reference'])} | {row['distance_px']:.3f} |"
            )
    else:
        lines.append("None.")
    lines += ["", "### False positives (unmatched predictions)", ""]
    if report["false_positives"]:
        lines += ["| Prediction | At | Reason |", "|---|---|---|"]
        for row in report["false_positives"]:
            lines.append(f"| {row['id']} | {_fmt_xy(row)} | {_near(row['reason'], row['references_within_tolerance'])} |")
    else:
        lines.append("None.")
    lines += ["", "### False negatives (missed reference receptacles)", ""]
    if report["false_negatives"]:
        lines += ["| Reference | At | Reason |", "|---|---|---|"]
        for row in report["false_negatives"]:
            lines.append(f"| {row['id']} | {_fmt_xy(row)} | {_near(row['reason'], row['predictions_within_tolerance'])} |")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def _near(reason: str, near: List[Dict[str, Any]]) -> str:
    if not near:
        return reason
    items = ", ".join(f"{n['id']} @ {n['distance_px']:.3f}px → {n['matched_to']}" for n in near)
    return f"{reason} [{items}]"


def _compact(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
