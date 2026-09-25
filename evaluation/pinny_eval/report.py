"""Build evaluation reports (JSON-serialisable dict + Markdown rendering)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .curves import CurveInput, pr_curve, sample_points
from .inputs import DETECTOR_ASSISTED, Detections, GroundTruth, dpi_check
from .matching import Pair, Point, eligible_pairs, match
from .stats import localization, prf
from .tolerance import Tolerance

REPORT_FORMAT = "pinny.evaluation_report"
NOT_MEASURED = "Not measured — verified reference pending"
MEASURED = "Measured against verified reference"
MEASURED_ASSISTED = "Measured against detector-assisted reviewed reference — recall may be overstated"
RECALL_CAVEAT = (
    "RECALL MAY BE OVERSTATED. The reference was bootstrapped from detector output and then reviewed "
    "(status 'detector_assisted_reviewed'). Receptacles the detector missed are the ones a reviewer "
    "is most likely to miss too, even with the exhaustive miss check. Treat recall as an upper bound."
)


# --------------------------------------------------------------------------- scoring


@dataclass
class PageScore:
    detections: Detections
    ground_truth: GroundTruth
    tolerance: Tolerance
    tol_by_id: Dict[str, float]
    edges: Dict[Tuple[str, str], float]
    pairs: List[Pair]

    @property
    def tp(self) -> int:
        return len(self.pairs)

    @property
    def fp(self) -> int:
        return len(self.detections.points) - self.tp

    @property
    def fn(self) -> int:
        return len(self.ground_truth.points) - self.tp

    def curve_input(self) -> CurveInput:
        return CurveInput(self.detections.confidences, len(self.ground_truth.points), self.edges)

    def matched_rows(self) -> List[Dict[str, Any]]:
        pred_by_id = {p.id: p for p in self.detections.points}
        ref_by_id = {r.id: r for r in self.ground_truth.points}
        rows = []
        for pr in self.pairs:
            p, r = pred_by_id[pr.prediction_id], ref_by_id[pr.reference_id]
            rows.append({
                "prediction": _pt(p),
                "reference": _pt(r),
                "distance_px": pr.distance,
                "dx_px": p.x - r.x,
                "dy_px": p.y - r.y,
                "tolerance_px": self.tol_by_id[p.id],
            })
        return rows


def score_page(detections: Detections, ground_truth: GroundTruth, tolerance: Tolerance) -> PageScore:
    tol_by_id = tolerance.per_prediction(detections.points, detections.boxes)
    edges = eligible_pairs(detections.points, ground_truth.points, tol_by_id)
    pairs = match(detections.points, ground_truth.points, tol_by_id, edges_by_id=edges)
    return PageScore(detections, ground_truth, tolerance, tol_by_id, edges, pairs)


# --------------------------------------------------------------------------- status


def accuracy_status(statuses: Sequence[str], scope_label: str,
                    extra_caveats: Sequence[str] = ()) -> Dict[str, Any]:
    """Headline status. ``statuses`` holds one verification status per scored page."""
    caveats = list(extra_caveats)
    counts = Counter(statuses)
    if DETECTOR_ASSISTED in counts:
        caveats.insert(0, RECALL_CAVEAT)
    kinds = set(counts)
    if not kinds:
        return {"real_drawing_accuracy": NOT_MEASURED, "scope": "no_reference", "caveats": caveats,
                "note": "No reference file was supplied. Detector output is recorded with its provenance only."}
    if kinds == {"verified"}:
        return {"real_drawing_accuracy": MEASURED, "scope": "verified_reference", "caveats": caveats,
                "note": f"Scores apply only to {scope_label} (these pages, this detector build and "
                        "settings, this tolerance)."}
    if kinds <= {"verified", DETECTOR_ASSISTED}:
        return {"real_drawing_accuracy": MEASURED_ASSISTED, "scope": "detector_assisted_reference",
                "caveats": caveats,
                "note": f"Scores apply only to {scope_label}. At least one reference was bootstrapped from "
                        "detector output; precision is meaningful, recall is an upper bound."}
    if kinds == {"synthetic"}:
        return {"real_drawing_accuracy": NOT_MEASURED, "scope": "synthetic_fixture", "caveats": caveats,
                "note": "Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing "
                        "about how the detector performs on real drawings."}
    if kinds == {"unverified"}:
        return {"real_drawing_accuracy": NOT_MEASURED, "scope": "unverified_reference", "caveats": caveats,
                "note": "The reference is labelled 'unverified'. The counts below show agreement with an "
                        "unverified reference and must not be reported as precision/recall on real drawings."}
    mix = ", ".join(f"{k}: {counts[k]}" for k in sorted(counts))
    return {"real_drawing_accuracy": NOT_MEASURED, "scope": "mixed_reference", "caveats": caveats,
            "note": f"Reference statuses are mixed ({mix}). Only an all-verified set is reported as "
                    "real-drawing accuracy; these counts show agreement only."}


# --------------------------------------------------------------------------- page report


def _pt(p: Point) -> Dict[str, Any]:
    return {"id": p.id, "x": p.x, "y": p.y}


def _runtime(det: Detections) -> Dict[str, Any]:
    return {"elapsed_seconds": det.runtime_seconds, "source_field": det.runtime_source}


def _frame(det: Detections, gt: Optional[GroundTruth]) -> Dict[str, Any]:
    frame = det.frame.as_dict()
    dpi = det.frame.dpi if det.frame.dpi is not None else (gt.frame.dpi if gt is not None else None)
    frame["dpi"] = dpi
    return frame


def gt_dataset(gt: GroundTruth) -> Dict[str, Any]:
    return {
        "dataset_id": gt.dataset["dataset_id"],
        "labeled_by": gt.dataset["labeled_by"],
        "labeled_at": gt.dataset.get("labeled_at"),
        "verification": gt.dataset["verification"],
        "reference_file": gt.path,
        "reference_sha256": gt.sha256,
    }


def matching_block(tolerance: Tolerance) -> Dict[str, Any]:
    return {
        **tolerance.as_dict(),
        "tolerance_units": "canonical raster pixels",
        "distance": "euclidean",
        "inclusive": True,
        "algorithm": "maximum-cardinality one-to-one matching; minimum total distance as tie-breaker",
    }


def same_scan_caveat(det: Detections, gt: Optional[GroundTruth]) -> List[str]:
    if gt is not None and gt.verification_status == DETECTOR_ASSISTED and gt.source_scan_id == det.scan_id:
        return [f"The reference was seeded from this same scan ('{det.scan_id}'), so any receptacle this "
                "scan missed was never proposed to the reviewer. Recall on this page is the most likely "
                "to be overstated."]
    return []


def build_report(
    detections: Detections,
    ground_truth: Optional[GroundTruth],
    tolerance: Optional[Tolerance],
) -> Dict[str, Any]:
    statuses = [] if ground_truth is None else [ground_truth.verification_status]
    label = "no dataset" if ground_truth is None else f"dataset '{ground_truth.dataset['dataset_id']}'"
    report: Dict[str, Any] = {
        "format": REPORT_FORMAT,
        "format_version": 1,
        "evaluator": {"name": "pinny_eval", "version": __version__},
        "status": accuracy_status(statuses, label, same_scan_caveat(detections, ground_truth)),
        "document": detections.identity.as_dict(),
        "coordinate_frame": _frame(detections, ground_truth),
        "dpi_check": dpi_check(detections, ground_truth),
        "detector_provenance": {
            "scan_id": detections.scan_id,
            "detector": detections.detector,
            "results_file": detections.path,
            "results_sha256": detections.sha256,
            "provenance": "original_detector_output",
            **detections.scan_provenance,
        },
        "runtime": _runtime(detections),
        "dataset": None,
        "matching": None,
        "counts": None,
        "metrics": None,
        "localization": None,
        "curve": None,
        "matched": [],
        "false_positives": [],
        "false_negatives": [],
    }
    if ground_truth is None:
        report["counts"] = {"predictions": len(detections.points)}
        return report

    assert tolerance is not None
    ps = score_page(detections, ground_truth, tolerance)
    report["dataset"] = gt_dataset(ground_truth)
    report["matching"] = matching_block(tolerance)

    preds = detections.points
    refs = ground_truth.points
    matched_pred = {pr.prediction_id: pr.reference_id for pr in ps.pairs}
    matched_ref = {pr.reference_id: pr.prediction_id for pr in ps.pairs}
    near_pred: Dict[str, List[Tuple[float, str]]] = {}
    near_ref: Dict[str, List[Tuple[float, str]]] = {}
    for (pid, rid), d in ps.edges.items():
        near_pred.setdefault(pid, []).append((d, rid))
        near_ref.setdefault(rid, []).append((d, pid))

    report["counts"] = {
        "predictions": len(preds),
        "references": len(refs),
        "true_positives": ps.tp,
        "false_positives": ps.fp,
        "false_negatives": ps.fn,
    }
    report["metrics"] = prf(ps.tp, ps.fp, ps.fn)
    report["matched"] = ps.matched_rows()
    report["localization"] = localization(report["matched"])
    report["curve"] = pr_curve([ps.curve_input()])

    fps: List[Dict[str, Any]] = []
    for p in sorted(preds, key=lambda q: q.id):
        if p.id in matched_pred:
            continue
        near = sorted(near_pred.get(p.id, []))
        reason = ("duplicate_or_crowded: every reference within tolerance is matched to another prediction"
                  if near else "no_reference_within_tolerance")
        fps.append({
            **_pt(p),
            "confidence": detections.confidences.get(p.id),
            "reason": reason,
            "references_within_tolerance": [
                {"id": rid, "distance_px": d, "matched_to": matched_ref.get(rid)} for d, rid in near
            ],
        })
    report["false_positives"] = fps

    fns: List[Dict[str, Any]] = []
    for r in sorted(refs, key=lambda q: q.id):
        if r.id in matched_ref:
            continue
        near = sorted(near_ref.get(r.id, []))
        fns.append({
            **_pt(r),
            "reason": ("no_prediction_within_tolerance" if not near
                       else "crowded: every prediction within tolerance is matched to another reference"),
            "predictions_within_tolerance": [
                {"id": pid, "distance_px": d, "matched_to": matched_pred.get(pid)} for d, pid in near
            ],
        })
    report["false_negatives"] = fns
    return report


# --------------------------------------------------------------------------- markdown helpers


def fmt_metric(m: Dict[str, Any]) -> str:
    if m["value"] is None:
        return f"undefined ({m['undefined_reason']})"
    return f"{m['value']:.4f} ({m['numerator']}/{m['denominator']})"


def fmt_num(v: Optional[float], spec: str = ".4f") -> str:
    return "—" if v is None else format(v, spec)


def _fmt_xy(p: Dict[str, Any]) -> str:
    return f"({p['x']:g}, {p['y']:g})"


def status_lines(st: Dict[str, Any]) -> List[str]:
    lines = [f"**Real-drawing accuracy:** {st['real_drawing_accuracy']}", ""]
    for c in st.get("caveats") or []:
        lines += [f"> **Caveat:** {c}", ">"]
    lines += [f"> {st['note']}", ""]
    return lines


def localization_lines(loc: Optional[Dict[str, Any]]) -> List[str]:
    lines = ["## Localization (matched pairs)", ""]
    if not loc or not loc.get("matched_pairs"):
        return lines + ["No matched pairs.", ""]
    return lines + [
        "| Measure | Value |",
        "|---|---|",
        f"| Matched pairs | {loc['matched_pairs']} |",
        f"| Mean distance px | {loc['mean_distance_px']:.3f} |",
        f"| Median distance px | {loc['median_distance_px']:.3f} |",
        f"| p95 distance px | {loc['p95_distance_px']:.3f} |",
        f"| Max distance px | {loc['max_distance_px']:.3f} |",
        f"| Mean dx px (prediction − reference) | {loc['mean_dx_px']:+.3f} |",
        f"| Mean dy px (prediction − reference, y down) | {loc['mean_dy_px']:+.3f} |",
        "",
    ]


def _op(pt: Optional[Dict[str, Any]]) -> str:
    if pt is None:
        return "not reached"
    return (f"threshold {pt['threshold']:g}: P {pt['precision']:.4f}, R {pt['recall']:.4f}, "
            f"F1 {pt['f1']:.4f} (TP {pt['tp']}, FP {pt['fp']}, FN {pt['fn']})")


def curve_lines(curve: Optional[Dict[str, Any]], title: str = "Score curve") -> List[str]:
    lines = [f"## {title}", ""]
    if not curve or not curve.get("available"):
        return lines + [(curve or {}).get("note") or "Not computed.", ""]
    lines += [
        f"- AP: **{curve['ap']:.4f}** ({curve['ap_method']})",
        f"- Best F1: {_op(curve['best_f1'])}",
        f"- Precision at recall ≥ 0.95: {_op(curve['precision_at_recall_0_95'])}",
        f"- Recall at precision ≥ 0.95: {_op(curve['recall_at_precision_0_95'])}",
        f"- {curve['thresholds']} distinct thresholds over {curve['detections']} detections "
        f"and {curve['references']} references (all points in the JSON report)",
        "",
        "| Threshold | Detections | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in sample_points(curve["points"]):
        lines.append(f"| {p['threshold']:g} | {p['detections']} | {p['tp']} | {p['fp']} | {p['fn']} "
                     f"| {p['precision']:.4f} | {p['recall']:.4f} | {p['f1']:.4f} |")
    return lines + [""]


def baseline_lines(check: Optional[Dict[str, Any]]) -> List[str]:
    if not check:
        return []
    verdict = "PASSED" if check["passed"] else "FAILED (regression)"
    lines = ["## Baseline gate", "",
             f"**{verdict}** against `{check['baseline_file']}` (max drop {check['max_drop']:g})", "",
             "| Metric | Baseline | Current | Drop | Result |", "|---|---|---|---|---|"]
    for c in check["checks"]:
        lines.append(f"| {c['metric']} | {fmt_num(c['baseline'])} | {fmt_num(c['current'])} "
                     f"| {fmt_num(c['drop'], '+.4f')} | {c['result']} |")
    return lines + [""]


def render_markdown(report: Dict[str, Any]) -> str:
    st = report["status"]
    doc = report["document"]
    fr = report["coordinate_frame"]
    prov = report["detector_provenance"]
    rt = report.get("runtime") or {}
    lines = ["# Pinny detector evaluation report", ""] + status_lines(st) + [
        "## Identity",
        "",
        f"- Document: `{doc['document_id']}` version `{doc['document_version']}`, page index {doc['page_index']}",
        f"- Coordinate frame: {fr['space']} at {fr['dpi']:g} DPI, {fr['width']}x{fr['height']}, "
        f"origin {fr['origin']}, y {fr['y_axis']}" if fr.get("dpi") is not None else
        f"- Coordinate frame: {fr['space']}, {fr['width']}x{fr['height']}, origin {fr['origin']}, "
        f"y {fr['y_axis']}, dpi not declared",
        f"- DPI check: {report['dpi_check']['note']}",
        f"- Scan: `{prov['scan_id']}`",
        f"- Detector: `{prov['detector']['name']}` version `{prov['detector']['version']}`",
        f"- Detector settings: `{_compact(prov['detector']['settings'])}`",
        "- Detector runtime: " + (f"{rt['elapsed_seconds']:g} s (`{rt['source_field']}`)"
                                   if rt.get("elapsed_seconds") is not None else "not recorded"),
    ]
    if "template" in prov:
        tpl = prov["template"]
        lines.append(f"- Template: box `{_compact(tpl.get('box'))}`, sha256 `{str(tpl.get('sha256'))[:16]}…`")
    if "created_at" in prov:
        lines.append(f"- Scan created: {prov['created_at']}")
    lines += [
        f"- Detector results: `{prov['results_file']}` (sha256 `{prov['results_sha256'][:16]}…`), "
        "original detector output only (manual corrections are not scored)",
    ]
    ds = report["dataset"]
    if ds is None:
        lines += ["", f"Predictions recorded: {report['counts']['predictions']}. No scoring performed.", ""]
        return "\n".join(lines)

    m = report["matching"]
    c = report["counts"]
    mt = report["metrics"]
    tol = Tolerance(m["tolerance_px"], m["tolerance_rel"]).describe()
    lines += [
        f"- Reference dataset: `{ds['dataset_id']}` labelled by {ds['labeled_by']}"
        + (f" on {ds['labeled_at']}" if ds.get("labeled_at") else "")
        + f"; verification status **{ds['verification']['status']}**",
        f"- Reference file: `{ds['reference_file']}` (sha256 `{ds['reference_sha256'][:16]}…`)",
        "",
        "## Matching",
        "",
        f"- Tolerance: {tol} (Euclidean, inclusive)",
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
        f"| Precision | {fmt_metric(mt['precision'])} |",
        f"| Recall | {fmt_metric(mt['recall'])} |",
        f"| F1 | {fmt_metric(mt['f1'])} |",
        "",
    ]
    lines += localization_lines(report.get("localization"))
    lines += curve_lines(report.get("curve"))
    lines += baseline_lines(report.get("baseline_check"))
    lines += ["### Matched", ""]
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
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
