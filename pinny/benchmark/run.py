"""``python -m pinny.benchmark run``: score every mode on the test split."""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pinny.detection import DetectionError, ScanSettings

from . import evaluator
from .aggregate import aggregate_counts, metrics
from .dataset import SplitData, SplitPage, first_point_template_box, load_test_split
from .errors import BenchmarkError
from .gate import SYNTHETIC_LABEL
from .modes import DETECTOR_NAMES, MODEL, MODES, TEMPLATE_VERIFIER, git_version, run_mode

BENCHMARK_FORMAT = "pinny.benchmark"
TOLERANCE_PX = 12
TEMPLATE_SIZE_PX = 40
TEMPLATE_RULE = (
    "Template mode needs a template per page. Each page uses a 40x40 px box centred on its first "
    "test point (manifest order), shifted to lie inside the page. The template matches itself, so "
    "the baseline gets that hit on nearly every page, which flatters template recall slightly. "
    "Pages with no test points get no "
    "template, and both template modes report zero detections there."
)
REFERENCE_STATUSES = ("unverified", "verified")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reference_status(split: SplitData, requested: Optional[str]) -> str:
    if split.synthetic_only:
        return "synthetic"
    status = requested or "unverified"
    if status not in REFERENCE_STATUSES:
        raise BenchmarkError("invalid_reference_status",
                             f"reference status must be one of {REFERENCE_STATUSES}, got {status!r}.")
    return status


def ground_truth_for(page: SplitPage, split: SplitData, status: str) -> Dict[str, Any]:
    if status == "synthetic":
        labeled_by = "pinny.training.synthetic (generated)"
        method = "points placed by the synthetic page generator (P4)"
    else:
        labeled_by = "Pinny complete-page review (dataset manifest)"
        method = ("approved and manually added pins of a complete page (P3: zero unreviewed pins). "
                  "Approved machine pins were proposed by the template detector and confirmed by a "
                  "person; see docs/phase2-evaluation.md for blind labelling of test pages.")
    return {
        "format": "pinny.ground_truth",
        "format_version": 1,
        "dataset": {
            "dataset_id": split.dataset_id,
            "labeled_by": labeled_by,
            "labeled_at": split.manifest.get("created_at"),
            "verification": {"status": status, "independent_of_detector": True, "method": method,
                             "reviewed_by": None},
            "notes": f"test split page {page.page_key} ({page.canonical_page_id})",
        },
        "document": {"document_id": page.document_id, "document_version": page.document_version,
                     "page_index": page.page_index},
        "coordinate_frame": {"space": "canonical_raster_px", "dpi": 200, "width": page.width,
                             "height": page.height, "origin": "top-left", "y_axis": "down"},
        "receptacles": [{"id": f"r{i:04d}", "x": p["x"], "y": p["y"]}
                        for i, p in enumerate(page.points, start=1)],
    }


def _model_meta(model: Any, meta: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    out = {"model_id": str(model.model_id), "threshold": float(model.threshold)}
    for key in ("kind", "arch", "dataset_id", "synthetic_only", "weights_sha256", "operating_point"):
        if meta and key in meta:
            out[key] = meta[key]
    return out


def run_benchmark(dataset_dir: Path, modes: Sequence[str], out_dir: Path, *,
                  verifier: Any = None, detector: Any = None,
                  verifier_meta: Optional[Mapping[str, Any]] = None,
                  detector_meta: Optional[Mapping[str, Any]] = None,
                  template_threshold: Optional[float] = None,
                  reference_status: Optional[str] = None,
                  overwrite: bool = False) -> Dict[str, Any]:
    modes = list(dict.fromkeys(modes))
    for m in modes:
        if m not in MODES:
            raise BenchmarkError("invalid_mode", f"Unknown mode {m!r}; expected one of {', '.join(MODES)}.")
    if not modes:
        raise BenchmarkError("invalid_mode", "Pass at least one mode.")
    if TEMPLATE_VERIFIER in modes and verifier is None:
        raise BenchmarkError("missing_model", "Mode 'template+verifier' needs --verifier <model_dir>.")
    if MODEL in modes and detector is None:
        raise BenchmarkError("missing_model", "Mode 'model' needs --detector <model_dir>.")

    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise BenchmarkError("output_exists",
                             f"{out_dir} is not empty. Choose a new --out or pass --overwrite.")
    split = load_test_split(Path(dataset_dir))
    status = _reference_status(split, reference_status)
    settings = ScanSettings() if template_threshold is None else ScanSettings(threshold=template_threshold)
    try:
        settings.validate()
    except DetectionError as exc:
        raise BenchmarkError("invalid_settings", str(exc)) from exc
    template_version = git_version()

    template_boxes = {p.page_key: first_point_template_box(p, TEMPLATE_SIZE_PX) for p in split.pages}
    gt_paths: Dict[str, Path] = {}
    for page in split.pages:
        gt_paths[page.page_key] = out_dir / "reference" / f"{page.safe_key}.ground_truth.json"
        _write_json(gt_paths[page.page_key], ground_truth_for(page, split, status))

    per_mode_pages: Dict[str, List[Dict[str, Any]]] = {m: [] for m in modes}
    elapsed: Dict[str, float] = {m: 0.0 for m in modes}
    for page in split.pages:
        rgb = page.load_rgb()
        for mode in modes:
            t0 = time.perf_counter()
            try:
                scan = run_mode(mode, page, rgb, dataset_id=split.dataset_id,
                                template_box=template_boxes[page.page_key], settings=settings,
                                template_version=template_version, verifier=verifier,
                                detector=detector)
            except DetectionError as exc:
                raise BenchmarkError(exc.code, f"{mode} on page {page.page_key}: {exc}") from exc
            elapsed[mode] += time.perf_counter() - t0
            det_path = out_dir / mode / f"{page.safe_key}.detections.json"
            _write_json(det_path, evaluator.adapt_scan(scan))
            report = evaluator.score_files(
                det_path, gt_paths[page.page_key], document_id=page.document_id,
                document_version=page.document_version, page_index=page.page_index,
                width=page.width, height=page.height, tolerance_px=float(TOLERANCE_PX))
            report_path = out_dir / mode / f"{page.safe_key}.report.json"
            _write_json(report_path, report)
            c = report["counts"]
            per_mode_pages[mode].append({
                "page_key": page.page_key,
                "canonical_page_id": page.canonical_page_id,
                "document_id": page.document_id,
                "true_positives": c["true_positives"],
                "false_positives": c["false_positives"],
                "false_negatives": c["false_negatives"],
                "suppressed": len(scan.get("suppressed", [])),
                "detections_file": str(det_path.relative_to(out_dir)),
                "report_file": str(report_path.relative_to(out_dir)),
            })

    mode_results: Dict[str, Any] = {}
    for mode in modes:
        pages = per_mode_pages[mode]
        counts = aggregate_counts(pages)
        model_ids: Dict[str, str] = {}
        models: Dict[str, Any] = {}
        if mode == TEMPLATE_VERIFIER:
            model_ids["verifier"] = str(verifier.model_id)
            models["verifier"] = _model_meta(verifier, verifier_meta)
        if mode == MODEL:
            model_ids["detector"] = str(detector.model_id)
            models["detector"] = _model_meta(detector, detector_meta)
        mode_results[mode] = {
            "mode": mode,
            "detector_name": DETECTOR_NAMES[mode],
            "template_version": template_version if mode != MODEL else None,
            "model_ids": model_ids,
            "models": models,
            "counts": counts,
            "metrics": metrics(counts),
            "suppressed": sum(p["suppressed"] for p in pages),
            "elapsed_seconds": round(elapsed[mode], 3),
            "pages": pages,
        }

    ev = evaluator.load_evaluator()
    summary = {
        "format": BENCHMARK_FORMAT,
        "format_version": 1,
        "created_at": _now(),
        "dataset": {"dataset_id": split.dataset_id, "path": str(Path(dataset_dir)),
                    "synthetic_only": split.synthetic_only,
                    "source": split.manifest.get("source")},
        "split": "test",
        "label": (SYNTHETIC_LABEL if split.synthetic_only
                  else f"real drawings, {status} reference"),
        "reference_status": status,
        "tolerance_px": TOLERANCE_PX,
        "page_count": len(split.pages),
        "document_count": len(split.document_ids),
        "reference_points": split.reference_points,
        "aggregation": "TP/FP/FN summed over pages, then divided (not averaged per page)",
        "template_choice": {
            "rule": TEMPLATE_RULE,
            "size_px": TEMPLATE_SIZE_PX,
            "settings": settings.to_dict(),
            "per_page": template_boxes,
        },
        "evaluator": {"name": "pinny_eval", "version": ev.version},
        "modes": mode_results,
    }
    _write_json(out_dir / "summary.json", summary)
    (out_dir / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
    return summary


def _fmt(m: Mapping[str, Any]) -> str:
    if m["value"] is None:
        return f"undefined ({m['undefined_reason']})"
    return f"{m['value']:.4f} ({m['numerator']}/{m['denominator']})"


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    ds = summary["dataset"]
    lines = ["# Pinny benchmark", ""]
    if ds["synthetic_only"]:
        lines += [f"> **{SYNTHETIC_LABEL}**", ""]
    elif summary["reference_status"] != "verified":
        lines += ["> The reference is a single-person review (`unverified`). Treat these numbers as "
                  "agreement with that review until a second person checks the test pages.", ""]
    lines += [
        f"- Dataset: `{ds['dataset_id']}`",
        f"- Split: `{summary['split']}` ({summary['label']})",
        f"- Pages: {summary['page_count']}, documents: {summary['document_count']}, "
        f"reference points: {summary['reference_points']}",
        f"- Tolerance: {summary['tolerance_px']} px (canonical raster, 200 DPI)",
        f"- Aggregation: {summary['aggregation']}",
        f"- Evaluator: {summary['evaluator']['name']} {summary['evaluator']['version']}",
        "",
        "| Mode | Model ids | TP | FP | FN | Precision | Recall | Suppressed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mode, r in summary["modes"].items():
        ids = ", ".join(f"{k}: `{v}`" for k, v in r["model_ids"].items()) or \
            f"template `{r['template_version']}`"
        c, m = r["counts"], r["metrics"]
        lines.append(f"| `{mode}` | {ids} | {c['true_positives']} | {c['false_positives']} | "
                     f"{c['false_negatives']} | {_fmt(m['precision'])} | {_fmt(m['recall'])} | "
                     f"{r['suppressed']} |")
    lines += ["", "## Template choice", "", summary["template_choice"]["rule"], "",
              f"Template threshold: {summary['template_choice']['settings']['threshold']}", ""]
    return "\n".join(lines)
