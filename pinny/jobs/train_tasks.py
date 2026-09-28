"""Training job kinds (docs/training-site.md section 4): ``build_dataset``,
``train_verifier``, ``train_detector`` and ``benchmark``.

They run in the ``train`` pool, one at a time: in a sandboxed child under a
worker (``python -m pinny.jobs.worker --pool train``), or in a thread of the
development server (``PINNY_JOBS=inprocess``). Payloads carry ids and a few
small numbers, validated here. Every path is derived from the data
directory:

    datasets/<dataset_id>/            pinny.dataset v1 (dataset_id: 64 hex)
    models/<model_id>/                the model registry
    benchmarks/<job_id>/summary.json  pinny.benchmark v1
    benchmarks/<job_id>/promotion.json   pinny.promotion v1 (the promotion evidence)

``benchmark`` takes an optional ``template_threshold``: the template
baseline and the candidate's template stage both use it, as with
``python -m pinny.benchmark run --template-threshold``.

Results are small JSON summaries without server paths.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .progress import Progress, job_dir
from .tasks import TaskError, _VERSION_RE

DATASET_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # pinny.models.registry
EPOCHS_MAX = 200


def benchmark_dir(data_dir: Path, job_id: str) -> Path:
    job_dir(data_dir, job_id)  # validates the id
    return Path(data_dir) / "benchmarks" / job_id


def promotion_report_path(data_dir: Path, job_id: str) -> Path:
    return benchmark_dir(data_dir, job_id) / "promotion.json"


def dataset_dir(data_dir: Path, dataset_id: Any) -> Path:
    if not isinstance(dataset_id, str) or not DATASET_ID_RE.match(dataset_id):
        raise TaskError("invalid_job", "Bad dataset id.")
    d = Path(data_dir) / "datasets" / dataset_id
    if not (d / "manifest.json").is_file():
        raise TaskError("dataset_not_found", "That dataset does not exist.", 404)
    return d


def _int(p: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    v = p.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise TaskError("invalid_job", f"{key} must be a whole number from {lo} to {hi}.")
    return v


def _require_torch() -> None:
    if importlib.util.find_spec("torch") is None:
        raise TaskError("training_unavailable", "PyTorch is not installed on the training worker. "
                        "Install the train extra (pip install -e '.[train]') there.", 422)


def _epoch_logger(progress: Progress, epochs: int, start: float = 0.02, span: float = 0.9):
    pattern = re.compile(r"\bepoch (\d+)")

    def log(msg: str) -> None:
        m = pattern.search(str(msg))
        frac = start + span * min(1.0, int(m.group(1)) / max(epochs, 1)) if m else None
        progress.update(frac, str(msg))
    return log


def _model_summary(meta: Dict[str, Any]) -> Dict[str, Any]:
    metrics = {k: {m: v for m, v in (meta.get("metrics") or {}).get(k, {}).items() if m != "history"}
               for k in ("val", "test")}
    return {"model_id": meta["model_id"], "kind": meta["kind"], "arch": meta.get("arch"),
            "dataset_id": meta.get("dataset_id"), "synthetic_only": bool(meta.get("synthetic_only")),
            "operating_point": meta.get("operating_point"), "metrics": metrics,
            "created_at": meta.get("created_at")}


# ------------------------------------------------------------ build_dataset
class _CountingRender:
    """The render service, reporting a step of progress per page rendered."""

    def __init__(self, render, progress: Progress, total: int) -> None:
        self.render, self.progress, self.total, self.done = render, progress, max(total, 1), 0

    def render_page(self, document_version: str, page_index: int):
        self.progress.check()
        page = self.render.render_page(document_version, page_index)
        self.done += 1
        self.progress.update(0.1 + 0.85 * self.done / self.total,
                             f"page {self.done} of at most {self.total} rendered")
        return page


def build_dataset(p: Dict[str, Any], data_dir: Path, progress: Progress, job_id: str) -> Dict[str, Any]:
    from pinny.learning import LearningStore
    from pinny.render import RenderService
    from pinny.training.dataset import build_dataset as _build, summarize

    version = p.get("document_version")
    if version is not None and (not isinstance(version, str) or not _VERSION_RE.match(version)):
        raise TaskError("invalid_job", "Bad document version.")
    progress.update(0.02, "exporting reviews from the learning store")
    with LearningStore(data_dir) as store:
        export = store.export(document_version=version)
    pages = {s["canonical_page_id"] for s in export.get("scans", [])}
    labelled = sum(1 for ex in export.get("examples", []) if ex.get("label") in ("positive", "negative"))
    progress.update(0.1, f"{len(export.get('scans', []))} scans on {len(pages)} pages, {labelled} labelled pins")
    if not pages:
        raise TaskError("nothing_to_build", "There are no scans to build a dataset from yet. Scan and review "
                        "some pages first.", 422)
    render = _CountingRender(RenderService(data_dir, cache_size=2), progress, len(pages))
    res = _build(export, render, Path(data_dir) / "datasets")
    info = summarize(res.manifest)
    info["reused"] = res.reused
    c = info["counts"]
    progress.update(1.0, f"dataset {info['dataset_id'][:12]}: verifier "
                    + ", ".join(f"{s} {c['verifier'][s]['pos']}+/{c['verifier'][s]['neg']}-" for s in c["verifier"])
                    + "; detector " + ", ".join(f"{s} {c['detector'][s]['pages']} pages" for s in c["detector"])
                    + (" (identical to an existing dataset)" if res.reused else ""))
    return info


# ------------------------------------------------------------ training
def train_verifier(p: Dict[str, Any], data_dir: Path, progress: Progress, job_id: str) -> Dict[str, Any]:
    d = dataset_dir(data_dir, p.get("dataset_id"))
    from pinny.training import verifier_train as vt
    epochs = _int(p, "epochs", vt.TrainConfig.epochs, 1, EPOCHS_MAX)
    seed = _int(p, "seed", 0, 0, 2**31 - 1)
    _require_torch()
    from pinny.models.artifact import read_metadata
    progress.update(0.01, f"training a verifier on dataset {d.name[:12]} for up to {epochs} epochs")
    model_dir = vt.train_verifier(d, Path(data_dir) / "models", vt.TrainConfig(epochs=epochs, seed=seed),
                                  log=_epoch_logger(progress, epochs))
    meta = read_metadata(model_dir)
    progress.update(1.0, f"saved {meta['model_id']}")
    return _model_summary(meta)


def train_detector(p: Dict[str, Any], data_dir: Path, progress: Progress, job_id: str) -> Dict[str, Any]:
    d = dataset_dir(data_dir, p.get("dataset_id"))
    from pinny.training import detector_train as dt
    defaults = dt.TrainConfig()
    epochs = _int(p, "epochs", defaults.epochs, 1, EPOCHS_MAX)
    tiles = _int(p, "tiles_per_epoch", defaults.tiles_per_epoch, 8, 65536)
    seed = _int(p, "seed", 0, 0, 2**31 - 1)
    _require_torch()
    progress.update(0.01, f"training a point detector on dataset {d.name[:12]} for {epochs} epochs")
    out = dt.train_detector(d, Path(data_dir) / "models",
                            dt.TrainConfig(epochs=epochs, tiles_per_epoch=tiles, seed=seed),
                            log=_epoch_logger(progress, epochs))
    meta = out["metadata"]
    progress.update(1.0, f"saved {meta['model_id']}")
    return _model_summary(meta)


# ------------------------------------------------------------ benchmark
def benchmark(p: Dict[str, Any], data_dir: Path, progress: Progress, job_id: str) -> Dict[str, Any]:
    from pinny.benchmark.gate import build_promotion_report
    from pinny.benchmark.run import run_benchmark
    from pinny.models.registry import MODE_FOR_KIND, ModelRegistry

    d = dataset_dir(data_dir, p.get("dataset_id"))
    model_id = p.get("model_id")
    if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
        raise TaskError("invalid_job", "Bad model id.")
    threshold = p.get("template_threshold")
    if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                                  or not -1 <= threshold <= 1):
        raise TaskError("invalid_job", "template_threshold must be a number from -1 to 1.")
    reg = ModelRegistry(data_dir)
    meta = reg.get(model_id)  # 404 unknown_model
    kind = meta["kind"]
    mode = MODE_FOR_KIND[kind]
    _require_torch()
    progress.update(0.02, f"loading {kind} {model_id}")
    model_dir = reg.model_dir(model_id)
    if kind == "verifier":
        from pinny.models.verifier import Verifier
        models = {"verifier": Verifier.load(model_dir), "verifier_meta": meta}
    else:
        from pinny.models.point_detector import PointDetector
        models = {"detector": PointDetector.load(model_dir), "detector_meta": meta}
    progress.update(0.05, f"scoring template and {mode} on the test split of dataset {d.name[:12]}")
    out = benchmark_dir(data_dir, job_id)
    summary = run_benchmark(d, ["template", mode], out, overwrite=True,
                            template_threshold=None if threshold is None else float(threshold), **models)
    raw = (out / "summary.json").read_bytes()
    progress.update(0.9, f"{summary['page_count']} test pages, {summary['document_count']} documents, "
                    f"{summary['reference_points']} reference points ({summary['label']})")
    report = build_promotion_report(summary, mode, benchmark_sha256=hashlib.sha256(raw).hexdigest(),
                                    benchmark_path=f"benchmarks/{job_id}/summary.json")
    tmp = out / "promotion.json.tmp"
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(out / "promotion.json")
    failed = ", ".join(report["failed_conditions"]) or "none"
    progress.update(1.0, f"promote: {str(report['promote']).lower()} (failed conditions: {failed})")
    return benchmark_result(report)


def benchmark_result(report: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a promotion report the training pages show."""
    keys = ("model_id", "kind", "candidate_mode", "baseline_mode", "promote", "dataset_id", "split",
            "synthetic_only", "label", "tolerance_px", "page_count", "document_count", "reference_points",
            "conditions", "failed_conditions", "created_at")
    out = {k: report.get(k) for k in keys}
    out["baseline"] = {"counts": report["baseline"]["counts"], "metrics": _metric_values(report["baseline"])}
    out["candidate"] = {"counts": report["candidate"]["counts"], "metrics": _metric_values(report["candidate"])}
    return out


def _metric_values(side: Dict[str, Any]) -> Dict[str, Optional[float]]:
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (side.get("metrics") or {}).items()}


TRAIN_TASKS = {"build_dataset": build_dataset, "train_verifier": train_verifier,
               "train_detector": train_detector, "benchmark": benchmark}
