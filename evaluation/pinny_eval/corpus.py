"""Score many pages at once from a manifest (``score-corpus``).

Manifest (``pinny.eval_manifest`` v1); paths are relative to the manifest file::

    {
      "format": "pinny.eval_manifest", "format_version": 1,
      "corpus_id": "mini-v1",
      "tolerance_px": 10,            # optional; see below
      "tolerance_rel": null,         # optional
      "pages": [
        {"detections": "a/p0.det.json", "ground_truth": "a/p0.gt.json",
         "document": "site-a", "tags": ["dense", "scanned"]}
      ]
    }

Each detections/reference pair must describe the same document version, page
and frame (the same checks as ``score``, with the reference as the expected
identity). The same page may not appear twice. ``document`` groups pages for
the per-document table and defaults to the page's ``document_id``.

Tolerance: ``--tolerance-px`` / ``--tolerance-rel`` on the command line, else
the manifest's values. If both give a value for the same field and they
differ, the run is rejected so a gate can't silently change radius.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .curves import pr_curve
from .inputs import InputError, check_pair, load_detections, load_ground_truth
from .report import (
    PageScore, accuracy_status, baseline_lines, curve_lines, fmt_metric,
    fmt_num, localization_lines, matching_block, same_scan_caveat, score_page, status_lines,
)
from .stats import f1_value, localization, prf, runtime_summary
from .tolerance import Tolerance

MANIFEST_FORMAT = "pinny.eval_manifest"
CORPUS_FORMAT = "pinny.corpus_report"

def load_manifest(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise InputError(f"{path}: cannot read manifest ({exc})") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: manifest must be a JSON object")
    if data.get("format") != MANIFEST_FORMAT or data.get("format_version") != 1:
        raise InputError(f"{path}: expected format '{MANIFEST_FORMAT}' with format_version 1")
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        raise InputError(f"{path}: 'pages' must be a non-empty list")
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for i, entry in enumerate(pages):
        where = f"{path}: pages[{i}]"
        if not isinstance(entry, dict):
            raise InputError(f"{where}: must be an object")
        for key in ("detections", "ground_truth"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise InputError(f"{where}: '{key}' must be a path")
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) and t for t in tags):
            raise InputError(f"{where}: 'tags' must be a list of non-empty strings")
        doc = entry.get("document")
        if doc is not None and (not isinstance(doc, str) or not doc):
            raise InputError(f"{where}: 'document' must be a non-empty string")
        out.append({
            "detections": os.path.join(base, entry["detections"]),
            "ground_truth": os.path.join(base, entry["ground_truth"]),
            "detections_rel": entry["detections"],
            "ground_truth_rel": entry["ground_truth"],
            "document": doc,
            "tags": list(tags),
        })
    return {"path": path, "corpus_id": data.get("corpus_id") or os.path.basename(path),
            "description": data.get("description"),
            "tolerance_px": data.get("tolerance_px"), "tolerance_rel": data.get("tolerance_rel"),
            "pages": out}


def resolve_tolerance(manifest: Dict[str, Any], cli_px: Optional[float], cli_rel: Optional[float]) -> Tolerance:
    vals = {}
    for key, cli in (("tolerance_px", cli_px), ("tolerance_rel", cli_rel)):
        man = manifest.get(key)
        if cli is not None and man is not None and float(cli) != float(man):
            raise InputError(f"--{key.replace('_', '-')} {cli:g} differs from the manifest's {key} {man:g}")
        vals[key] = cli if cli is not None else man
    try:
        return Tolerance(vals["tolerance_px"], vals["tolerance_rel"])
    except ValueError as exc:
        raise InputError(f"{manifest['path']}: {exc} (none given on the command line or in the manifest)")


def _group_rows(pages: List[Dict[str, Any]], key_fn) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in pages:
        for k in key_fn(p):
            groups.setdefault(k, []).append(p)
    rows = []
    for k in sorted(groups):
        ps = groups[k]
        tp = sum(p["counts"]["true_positives"] for p in ps)
        fp = sum(p["counts"]["false_positives"] for p in ps)
        fn = sum(p["counts"]["false_negatives"] for p in ps)
        rts = [p["runtime_seconds"] for p in ps if p["runtime_seconds"] is not None]
        rows.append({"key": k, "pages": len(ps), "true_positives": tp, "false_positives": fp,
                     "false_negatives": fn, "metrics": prf(tp, fp, fn),
                     "runtime_total_seconds": sum(rts) if rts else None})
    return rows


def build_corpus_report(manifest: Dict[str, Any], tolerance: Tolerance, worst: int = 5) -> Dict[str, Any]:
    scores: List[Tuple[Dict[str, Any], PageScore]] = []
    seen: Dict[Tuple[str, int], int] = {}
    for i, entry in enumerate(manifest["pages"]):
        det = load_detections(entry["detections"])
        gt = load_ground_truth(entry["ground_truth"])
        try:
            check_pair(det, gt)
        except InputError as exc:
            raise InputError(f"manifest pages[{i}]: {exc}") from exc
        key = (det.identity.document_version, det.identity.page_index)
        if key in seen:
            raise InputError(f"manifest pages[{i}] repeats pages[{seen[key]}] "
                             f"(document_version {key[0]!r}, page {key[1]})")
        seen[key] = i
        scores.append((entry, score_page(det, gt, tolerance)))

    pages: List[Dict[str, Any]] = []
    all_matched: List[Dict[str, Any]] = []
    extra_caveats: List[str] = []
    for entry, ps in scores:
        det, gt = ps.detections, ps.ground_truth
        doc_label = entry["document"] or det.identity.document_id
        rows = ps.matched_rows()
        all_matched += rows
        for c in same_scan_caveat(det, gt):
            extra_caveats.append(f"[{doc_label} p{det.identity.page_index}] {c}")
        curve = pr_curve([ps.curve_input()])
        pages.append({
            "page_label": f"{doc_label} p{det.identity.page_index}",
            "document": doc_label,
            "tags": entry["tags"],
            "identity": det.identity.as_dict(),
            "scan_id": det.scan_id,
            "detector": {"name": det.detector["name"], "version": det.detector["version"]},
            "dataset_id": gt.dataset["dataset_id"],
            "verification_status": gt.verification_status,
            "detections_file": entry["detections_rel"],
            "detections_sha256": det.sha256,
            "ground_truth_file": entry["ground_truth_rel"],
            "ground_truth_sha256": gt.sha256,
            "counts": {"predictions": len(det.points), "references": len(gt.points),
                       "true_positives": ps.tp, "false_positives": ps.fp, "false_negatives": ps.fn},
            "metrics": prf(ps.tp, ps.fp, ps.fn),
            "ap": curve["ap"],
            "runtime_seconds": det.runtime_seconds,
            "localization": localization(rows),
        })

    tp = sum(p["counts"]["true_positives"] for p in pages)
    fp = sum(p["counts"]["false_positives"] for p in pages)
    fn = sum(p["counts"]["false_negatives"] for p in pages)

    macro: Dict[str, Any] = {}
    for key in ("precision", "recall", "f1"):
        vals = [p["metrics"][key]["value"] for p in pages if p["metrics"][key]["value"] is not None]
        macro[key] = {"value": sum(vals) / len(vals) if vals else None, "pages_included": len(vals),
                      "pages_excluded_undefined": len(pages) - len(vals),
                      "undefined_reason": None if vals else "undefined on every page"}

    ranked = [p for p in pages if f1_value(p["counts"]["true_positives"], p["counts"]["false_positives"],
                                           p["counts"]["false_negatives"]) is not None]
    ranked.sort(key=lambda p: (p["metrics"]["f1"]["value"],
                               -(p["counts"]["false_positives"] + p["counts"]["false_negatives"]),
                               p["page_label"]))
    worst_pages = [{"page_label": p["page_label"], "f1": p["metrics"]["f1"]["value"],
                    "false_positives": p["counts"]["false_positives"],
                    "false_negatives": p["counts"]["false_negatives"],
                    "detections_file": p["detections_file"]} for p in ranked[:worst]]

    runtimes = [p["runtime_seconds"] for p in pages if p["runtime_seconds"] is not None]
    statuses = [p["verification_status"] for p in pages]
    return {
        "format": CORPUS_FORMAT,
        "format_version": 1,
        "evaluator": {"name": "pinny_eval", "version": __version__},
        "corpus_id": manifest["corpus_id"],
        "manifest_file": manifest["path"],
        "description": manifest.get("description"),
        "status": accuracy_status(statuses, f"corpus '{manifest['corpus_id']}'", extra_caveats),
        "verification_status_counts": {s: statuses.count(s) for s in sorted(set(statuses))},
        "matching": matching_block(tolerance),
        "detectors": sorted({f"{p['detector']['name']} {p['detector']['version']}" for p in pages}),
        "counts": {"pages": len(pages), "documents": len({p["document"] for p in pages}),
                   "predictions": sum(p["counts"]["predictions"] for p in pages),
                   "references": sum(p["counts"]["references"] for p in pages),
                   "true_positives": tp, "false_positives": fp, "false_negatives": fn},
        "micro": prf(tp, fp, fn),
        "macro": macro,
        "aggregation_note": "micro = pooled TP/FP/FN then divided (the headline); macro = unweighted mean "
                            "of per-page values over pages where the value is defined",
        "curve": pr_curve([ps.curve_input() for _, ps in scores]),
        "localization": localization(all_matched),
        "runtime": runtime_summary(runtimes, len(pages) - len(runtimes)),
        "per_page": pages,
        "per_document": _group_rows(pages, lambda p: [p["document"]]),
        "per_tag": _group_rows(pages, lambda p: p["tags"]),
        "worst_pages": worst_pages,
    }


def _group_table(rows: List[Dict[str, Any]], title: str, label: str) -> List[str]:
    lines = [f"## {title}", ""]
    if not rows:
        return lines + ["None.", ""]
    lines += [f"| {label} | Pages | TP | FP | FN | Precision | Recall | F1 | Runtime s |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        m = r["metrics"]
        lines.append(f"| {r['key']} | {r['pages']} | {r['true_positives']} | {r['false_positives']} "
                     f"| {r['false_negatives']} | {fmt_num(m['precision']['value'])} "
                     f"| {fmt_num(m['recall']['value'])} | {fmt_num(m['f1']['value'])} "
                     f"| {fmt_num(r['runtime_total_seconds'], '.3g')} |")
    return lines + [""]


def render_corpus_markdown(report: Dict[str, Any]) -> str:
    c = report["counts"]
    mi, ma = report["micro"], report["macro"]
    m = report["matching"]
    rt = report["runtime"]
    lines = [f"# Pinny corpus evaluation: {report['corpus_id']}", ""] + status_lines(report["status"])
    lines += [
        f"- Manifest: `{report['manifest_file']}`",
        f"- Pages: {c['pages']} across {c['documents']} documents; verification statuses: "
        + ", ".join(f"{k} {v}" for k, v in report["verification_status_counts"].items()),
        "- Detectors: " + ", ".join(f"`{d}`" for d in report["detectors"]),
        f"- Tolerance: {Tolerance(m['tolerance_px'], m['tolerance_rel']).describe()} (Euclidean, inclusive)",
        "",
        "## Pooled results",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Predictions | {c['predictions']} |",
        f"| Reference receptacles | {c['references']} |",
        f"| True positives | {c['true_positives']} |",
        f"| False positives | {c['false_positives']} |",
        f"| False negatives (misses) | {c['false_negatives']} |",
        f"| Micro precision | {fmt_metric(mi['precision'])} |",
        f"| Micro recall | {fmt_metric(mi['recall'])} |",
        f"| Micro F1 | {fmt_metric(mi['f1'])} |",
    ]
    for key in ("precision", "recall", "f1"):
        v = ma[key]
        lines.append(f"| Macro {key} | {fmt_num(v['value'])} (over {v['pages_included']} pages"
                     + (f"; {v['pages_excluded_undefined']} undefined" if v["pages_excluded_undefined"] else "")
                     + ") |")
    lines += ["", f"_{report['aggregation_note']}._", ""]
    lines += curve_lines(report["curve"], "Pooled score curve")
    lines += baseline_lines(report.get("baseline_check"))
    lines += localization_lines(report["localization"])
    lines += ["## Runtime", ""]
    if rt.get("pages_with_runtime"):
        lines += [f"- p50 {rt['p50_seconds']:.3f} s, p95 {rt['p95_seconds']:.3f} s, mean {rt['mean_seconds']:.3f} s, "
                  f"total {rt['total_seconds']:.3f} s over {rt['pages_with_runtime']} pages"
                  + (f" ({rt['pages_without_runtime']} pages without runtime)" if rt["pages_without_runtime"] else ""),
                  ""]
    else:
        lines += ["No runtime recorded.", ""]
    lines += ["## Worst pages", ""]
    if report["worst_pages"]:
        lines += ["| Page | F1 | FP | FN | Detections file |", "|---|---|---|---|---|"]
        for w in report["worst_pages"]:
            lines.append(f"| {w['page_label']} | {w['f1']:.4f} | {w['false_positives']} | {w['false_negatives']} "
                         f"| `{w['detections_file']}` |")
    else:
        lines.append("None.")
    lines += ["", "## Per page", "",
              "| Page | Status | Tags | Pred | Ref | TP | FP | FN | Precision | Recall | F1 | AP | Runtime s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in report["per_page"]:
        pc, pm = p["counts"], p["metrics"]
        lines.append(
            f"| {p['page_label']} | {p['verification_status']} | {', '.join(p['tags']) or '—'} "
            f"| {pc['predictions']} | {pc['references']} | {pc['true_positives']} | {pc['false_positives']} "
            f"| {pc['false_negatives']} | {fmt_num(pm['precision']['value'])} | {fmt_num(pm['recall']['value'])} "
            f"| {fmt_num(pm['f1']['value'])} | {fmt_num(p['ap'])} | {fmt_num(p['runtime_seconds'], '.3g')} |")
    lines.append("")
    lines += _group_table(report["per_document"], "Per document", "Document")
    if report["per_tag"]:
        lines += _group_table(report["per_tag"], "Per tag", "Tag")
    return "\n".join(lines)
