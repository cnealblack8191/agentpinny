"""Training site service (docs/training-site.md sections 3 and 4): labelling
progress, the review queue, page completion, datasets, training runs,
benchmarks and model promotion.

Heavy work runs as jobs in the ``train`` pool: in sandboxed children under
``python -m pinny.jobs.worker --pool train`` (or the web process's own
worker threads with ``PINNY_JOBS=sandbox``), or, in development
(``PINNY_JOBS=inprocess``), in a background thread of this process. The
web tier only queues jobs by id, reads their records and the small JSON
files they write, and calls ``pinny.models.registry`` to promote.

Clients never send paths. Every id is validated before use; paths are
derived from the data directory.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from pinny.jobs.limits import TRAIN_KINDS
from pinny.jobs.queue import DONE, Job, JobQueue
from pinny.jobs.train_tasks import DATASET_ID_RE, EPOCHS_MAX, MODEL_ID_RE, promotion_report_path
from pinny.learning import contract as _contract
from pinny.learning import store as _store
from pinny.models.registry import KINDS, MODE_FOR_KIND, RegistryError

from .errors import ViewerError

_VERSION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_THRESHOLD_KEYS = ("threshold", "score_threshold", "min_score")  # as the learning store reads them

#: Readiness thresholds shown on the dashboard (docs/training-site.md section 3).
#: Guidance, not a lock: an admin may train earlier.
READINESS = {
    "verifier": {"positive": 100, "negative": 100, "documents": 10},
    "detector": {"complete_pages": 30, "points": 300, "documents": 10},
}
#: The promotion gate's minimum on a dataset's test split (docs/phase2-contracts.md P9).
GATE_TEST_DOCUMENTS = 3
GATE_TEST_POINTS = 100


def _job_dict(j: Job, *, detail: bool = False) -> Dict[str, Any]:
    out = {"job_id": j.job_id, "kind": j.kind, "status": j.status, "requested_by": j.requested_by,
           "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at,
           "progress": 1.0 if j.status == DONE else (j.progress or 0.0),
           "error": {"code": j.error_code, "message": j.error_message} if j.error_code else None,
           "payload": j.payload, "result": j.result if j.status == DONE else None,
           "cancel_requested": j.cancel_requested}
    if detail:
        out["log_tail"] = j.progress_log or ""
    return out


def _check(value: Any, pattern: re.Pattern, code: str, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ViewerError(code, f"{what} is missing or malformed.")
    return value


class TrainingService:
    def __init__(self, viewer, jobs: Optional[JobQueue] = None) -> None:
        """``viewer`` is the ``ViewerService`` (its learning store, render
        client and registry). With the viewer's job queue (sandbox or
        external workers) training jobs go to the ``train`` pool; without
        one (development) they run in a thread of this process."""
        self.viewer = viewer
        self.data_dir = Path(viewer.data_dir)
        self.registry = viewer.registry
        self.jobs = jobs or viewer.jobs or JobQueue(self.data_dir)
        self._runner = None
        if viewer.jobs is None and jobs is None:
            from pinny.jobs.inprocess import InProcessRunner
            self._runner = InProcessRunner(self.jobs, "train", self.data_dir)
            # Jobs a stopped development server left behind: the runner
            # recovers stale ones and runs what is queued.
            if self.jobs.list("queued", 1, pool="train") or self.jobs.list("running", 1, pool="train"):
                self._runner.start()
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._runner is not None:
            self._runner.stop()

    def _db(self, fn, *a, **kw):
        return self.viewer._db(fn, *a, **kw)

    # -------------------------------------------------------------- stats
    def _documents(self) -> Dict[str, dict]:
        return {d["document_version"]: d for d in self.viewer.documents()}

    def dashboard(self) -> Dict[str, Any]:
        summary = self._db(self.viewer.store.pin_summary)
        docs = self._documents()
        live = [sc for sc in summary["scans"] if sc["document_version"] in docs]  # deleted drawings don't train
        labels = {"positive": 0, "negative": 0, "unlabeled": 0, "withdrawn": 0}
        reviewed = total_pins = 0
        pos_docs, neg_docs, labelled_docs = set(), set(), set()
        for sc in live:
            n = sc["pins"].get
            pos = n("machine:approved", 0) + n("manual:added", 0)
            neg = n("machine:rejected", 0)
            labels["positive"] += pos
            labels["negative"] += neg
            labels["unlabeled"] += n("machine:unreviewed", 0)
            labels["withdrawn"] += n("manual:removed", 0)
            total_pins += sum(sc["pins"].values())
            reviewed += sum(v for k, v in sc["pins"].items() if not k.endswith(":unreviewed"))
            if pos:
                pos_docs.add(sc["document_id"])
            if neg:
                neg_docs.add(sc["document_id"])
            if pos or neg:
                labelled_docs.add(sc["document_id"])
        # Detector pages (P3): marked fully reviewed, and the latest scan has zero unreviewed pins.
        latest: Dict[str, dict] = {}
        for sc in live:  # record order
            latest[sc["canonical_page_id"]] = sc
        reviews = summary["page_reviews"]
        complete_pages, complete_points, complete_docs = 0, 0, set()
        for page_id, sc in latest.items():
            if reviews.get(page_id) != "complete" or sc["pins"].get("machine:unreviewed", 0):
                continue  # detector pages must be marked fully reviewed (see pinny.training.dataset)
            complete_pages += 1
            complete_points += sc["pins"].get("machine:approved", 0) + sc["pins"].get("manual:added", 0)
            complete_docs.add(sc["document_id"])
        marked = sum(1 for page_id in latest if reviews.get(page_id) == "complete")

        def check(name, have, need):
            return {"name": name, "have": have, "need": need, "ok": have >= need}
        rv, rd = READINESS["verifier"], READINESS["detector"]
        verifier = [check("positive labels", labels["positive"], rv["positive"]),
                    check("negative labels", labels["negative"], rv["negative"]),
                    check("documents with labels", len(labelled_docs), rv["documents"])]
        detector = [check("complete pages", complete_pages, rd["complete_pages"]),
                    check("points on complete pages", complete_points, rd["points"]),
                    check("documents with complete pages", len(complete_docs), rd["documents"])]
        return {
            "labels": labels,
            "pins_reviewed": reviewed,
            "pins_total": total_pins,
            "pages": {"scanned": len(latest), "fully_reviewed": marked, "detector_eligible": complete_pages},
            "documents": {"uploaded": len(docs), "with_labels": len(labelled_docs),
                          "scanned": len({sc["document_id"] for sc in live})},
            "readiness": {"verifier": {"ready": all(c["ok"] for c in verifier), "checks": verifier},
                          "detector": {"ready": all(c["ok"] for c in detector), "checks": detector},
                          "promotion_gate": {"test_documents": GATE_TEST_DOCUMENTS,
                                             "test_points": GATE_TEST_POINTS}},
            "thresholds": READINESS,
            "active": {k: self.registry.active(k) for k in KINDS},
        }

    # --------------------------------------------------------- review queue
    def review_queue(self, limit: int = 50) -> Dict[str, Any]:
        """Unreviewed machine pins across every scanned page, most uncertain
        first (the learning store's ``review_queue`` order within a scan,
        ``|score - threshold|`` across scans), plus the pages still to mark
        fully reviewed."""
        docs = self._documents()
        items: List[Dict[str, Any]] = []
        pages: Dict[str, Dict[str, Any]] = {}
        all_scans = [s for s in self._db(self.viewer.store.list_scans) if s.document_version in docs]
        latest: Dict[str, str] = {}
        counts: Dict[str, int] = {}
        for scan in all_scans:  # list_scans is oldest first
            latest[scan.canonical_page_id] = scan.scan_id
            counts[scan.canonical_page_id] = counts.get(scan.canonical_page_id, 0) + 1
        for scan in all_scans:
            page_id = scan.canonical_page_id
            if latest[page_id] != scan.scan_id:
                continue  # superseded by a rescan: its pins are not the page's to review
            st = self._db(self.viewer.store.load_scan, scan.scan_id)
            page = pages.setdefault(page_id, {
                "canonical_page_id": page_id, "document_version": scan.document_version,
                "page_index": scan.page_index, "filename": docs[scan.document_version]["filename"],
                "unreviewed": 0, "scans": counts[page_id], "latest_scan_id": scan.scan_id,
                "best_margin": None, "first_pin": None})
            unreviewed = [p for p in st.pins if p.origin == "machine" and p.state == "unreviewed"]
            if not unreviewed:
                continue
            page["unreviewed"] += len(unreviewed)
            ordered = self._db(self.viewer.store.review_queue, scan.scan_id, limit=None)
            scores = {d.detection_id: d.score for d in st.detections}
            by_id = {p.pin_id: p for p in unreviewed}
            settings = scan.detector_settings or {}
            vt = (settings.get("verifier") or {}).get("threshold") if isinstance(settings.get("verifier"), dict) else None
            t = vt if isinstance(vt, (int, float)) else next(
                (float(settings[k]) for k in _THRESHOLD_KEYS if isinstance(settings.get(k), (int, float))), None)
            if t is None:
                t = min(scores.get(by_id[i].detection_id, 0.0) for i in ordered) if ordered else 0.0
            for pin_id in ordered:
                p = by_id.get(pin_id)
                if p is None:
                    continue
                score = float(scores.get(p.detection_id, 0.0))
                item = {"scan_id": scan.scan_id, "pin_id": pin_id, "document_version": scan.document_version,
                        "page_index": scan.page_index, "canonical_page_id": page_id,
                        "filename": page["filename"], "score": score, "threshold": float(t),
                        "margin": abs(score - float(t)), "x": p.x, "y": p.y}
                items.append(item)
                if page["best_margin"] is None or item["margin"] < page["best_margin"]:
                    page["best_margin"], page["first_pin"] = item["margin"], {"scan_id": scan.scan_id,
                                                                             "pin_id": pin_id}
        items.sort(key=lambda i: (i["margin"], i["document_version"], i["page_index"]))
        reviews = self._db(self.viewer.store.export, include_heldout=True)["page_reviews"] if pages else {}
        out_pages = []
        for page_id, page in pages.items():
            status = (reviews.get(page_id) or {}).get("status")
            if status == "complete":
                continue
            page["status"] = "needs_review" if page["unreviewed"] else "ready_to_mark"
            out_pages.append(page)
        out_pages.sort(key=lambda p: (p["status"] != "needs_review",
                                      p["best_margin"] if p["best_margin"] is not None else 0.0,
                                      p["filename"] or "", p["page_index"]))
        return {"items": items[:max(0, int(limit))], "total": len(items), "pages": out_pages}

    def mark_page_reviewed(self, document_version: Any, page_index: Any, reviewer: str) -> Dict[str, Any]:
        v = _check(document_version, _VERSION_RE, "invalid_document", "document_version")
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
            raise ViewerError("invalid_page", "page_index must be a non-negative integer.")
        self.viewer.document_info(v)  # 404 for a deleted or unknown drawing
        page_id = _contract.canonical_page_id(v, page_index)
        try:
            r = self._db(self.viewer.store.mark_page_complete, page_id, reviewer=reviewer)
        except _store.NotFound:
            raise ViewerError("page_not_scanned", "That page has not been scanned yet.", 404) from None
        except _store.InvalidTransition:
            scans = self._db(self.viewer.store.list_scans, document_version=v, page_index=page_index)
            n = sum(1 for p in self._db(self.viewer.store.load_scan, scans[-1].scan_id).pins
                    if p.origin == "machine" and p.state == "unreviewed")  # the latest scan only
            raise ViewerError("page_has_unreviewed_pins",
                              f"This page still has {n} unreviewed pin(s). Approve or reject every pin "
                              "before marking the page fully reviewed.", 409) from None
        return {"canonical_page_id": page_id, "document_version": v, "page_index": page_index,
                "status": r.status, "completed_at": r.completed_at, "reviewer": r.reviewer}

    # ------------------------------------------------------------- datasets
    def datasets(self) -> Dict[str, Any]:
        from pinny.training.dataset import load_manifest, summarize
        root = self.data_dir / "datasets"
        out = []
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not d.is_dir() or not DATASET_ID_RE.match(d.name):
                    continue
                try:
                    info = summarize(load_manifest(d))
                except Exception:  # noqa: BLE001 - half-written or foreign directory
                    continue
                test = info["counts"]["detector"]["test"]
                info["gate"] = {"test_documents": info["documents"]["test"], "test_points": test["points"],
                                "ok": (info["documents"]["test"] >= GATE_TEST_DOCUMENTS
                                       and test["points"] >= GATE_TEST_POINTS and not info["synthetic"])}
                out.append(info)
        out.sort(key=lambda i: (i["created_at"], i["dataset_id"]), reverse=True)
        return {"datasets": out, "gate": {"test_documents": GATE_TEST_DOCUMENTS, "test_points": GATE_TEST_POINTS}}

    def _dataset(self, dataset_id: Any) -> str:
        d = _check(dataset_id, DATASET_ID_RE, "invalid_dataset", "dataset_id")
        if not (self.data_dir / "datasets" / d / "manifest.json").is_file():
            raise ViewerError("dataset_not_found", "That dataset does not exist.", 404)
        return d

    def _model(self, model_id: Any) -> Dict[str, Any]:
        m = _check(model_id, MODEL_ID_RE, "invalid_model_id", "model_id")
        try:
            return self.registry.get(m)
        except RegistryError as exc:
            if exc.code == "unknown_model":
                raise ViewerError("unknown_model", "That model does not exist.", 404) from None
            raise ViewerError("invalid_model", "That model's files are unreadable.", 409) from None

    # ----------------------------------------------------------------- jobs
    def _submit(self, kind: str, payload: Dict[str, Any], requested_by: str, dedupe: str) -> Dict[str, Any]:
        job_id = self.jobs.submit(kind, payload, dedupe_key=f"train:{dedupe}", requested_by=requested_by)
        if self._runner is not None:
            self._runner.wake()
        return self.job(job_id)

    def build_dataset(self, requested_by: str) -> Dict[str, Any]:
        return self._submit("build_dataset", {}, requested_by, "build_dataset")

    def train(self, body: Dict[str, Any], requested_by: str) -> Dict[str, Any]:
        kind = body.get("kind")
        if kind not in KINDS:
            raise ViewerError("invalid_model_kind", "kind must be verifier or detector.")
        payload = {"dataset_id": self._dataset(body.get("dataset_id"))}
        for key, lo, hi in (("epochs", 1, EPOCHS_MAX), ("tiles_per_epoch", 8, 65536)):
            v = body.get(key)
            if v is None:
                continue
            if key == "tiles_per_epoch" and kind != "detector":
                raise ViewerError("invalid_setting", "tiles_per_epoch applies to the detector only.")
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ViewerError("invalid_setting", f"{key} must be a whole number from {lo} to {hi}.")
            payload[key] = v
        job_kind = f"train_{kind}"
        return self._submit(job_kind, payload, requested_by,
                            f"{job_kind}:{json.dumps(payload, sort_keys=True)}")

    def benchmark(self, body: Dict[str, Any], requested_by: str) -> Dict[str, Any]:
        meta = self._model(body.get("model_id"))
        dataset_id = body.get("dataset_id")
        if dataset_id is None:
            dataset_id = meta.get("dataset_id")  # the model's own dataset: its test split was never tuned on
        payload = {"model_id": meta["model_id"], "dataset_id": self._dataset(dataset_id)}
        t = body.get("template_threshold")
        if t is not None:
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not -1 <= t <= 1:
                raise ViewerError("invalid_setting", "template_threshold must be a number from -1 to 1.")
            payload["template_threshold"] = t
        return self._submit("benchmark", payload, requested_by, f"benchmark:{json.dumps(payload, sort_keys=True)}")

    def _train_job(self, job_id: str) -> Job:
        if not isinstance(job_id, str) or not _UUID_RE.match(job_id):
            raise ViewerError("unknown_job", "That job does not exist.", 404)
        try:
            j = self.jobs.get(job_id)
        except KeyError:
            raise ViewerError("unknown_job", "That job does not exist.", 404) from None
        if j.kind not in TRAIN_KINDS:
            raise ViewerError("unknown_job", "That job does not exist.", 404)
        return j

    def job(self, job_id: str) -> Dict[str, Any]:
        return _job_dict(self._train_job(job_id), detail=True)

    def list_jobs(self, limit: int = 50) -> Dict[str, Any]:
        return {"jobs": [_job_dict(j) for j in self.jobs.list(limit=limit, pool="train")]}

    def cancel(self, job_id: str) -> Dict[str, Any]:
        self._train_job(job_id)
        self.jobs.cancel(job_id)
        if self._runner is not None:
            self._runner.wake()
        return self.job(job_id)

    # --------------------------------------------------------------- models
    def _benchmarks(self) -> Dict[str, List[Dict[str, Any]]]:
        """Finished benchmark jobs per model id, newest first."""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for j in self.jobs.list(DONE, limit=1000, pool="train", kind="benchmark"):
            r = j.result or {}
            if isinstance(r.get("model_id"), str):
                out.setdefault(r["model_id"], []).append(dict(r, job_id=j.job_id, finished_at=j.finished_at))
        return out

    def models(self) -> Dict[str, Any]:
        benches = self._benchmarks()
        active = {}
        for k in KINDS:
            e = self.registry.active_entry(k)
            active[k] = None if e is None else {key: e.get(key) for key in (
                "model_id", "kind", "promoted_at", "evidence_sha256", "dataset_id")}
        models = []
        for m in reversed(self.registry.list_models()):  # newest first
            models.append({
                "model_id": m["model_id"], "kind": m["kind"], "arch": m.get("arch"),
                "dataset_id": m.get("dataset_id"), "synthetic_only": bool(m.get("synthetic_only")),
                "created_at": m.get("created_at"), "operating_point": m.get("operating_point"),
                "metrics": {k: {n: v for n, v in (m.get("metrics") or {}).get(k, {}).items() if n != "history"}
                            for k in ("val", "test")},
                "active": (active.get(m["kind"]) or {}).get("model_id") == m["model_id"],
                "benchmarks": benches.get(m["model_id"], []),
            })
        return {"active": active, "models": models, "modes": MODE_FOR_KIND}

    def promote(self, body: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Promote the model a finished benchmark job scored, with that job's
        promotion report as the evidence. The registry refuses unless the
        report recommends it."""
        job_id = body.get("benchmark_job_id")
        if not isinstance(job_id, str) or not _UUID_RE.match(job_id):
            raise ViewerError("invalid_job", "benchmark_job_id must be the id of a benchmark job.")
        j = self._train_job(job_id)
        if j.kind != "benchmark":
            raise ViewerError("not_a_benchmark", "That job is not a benchmark.", 409)
        if j.status != DONE:
            raise ViewerError("benchmark_not_finished", "That benchmark has not finished.", 409)
        path = promotion_report_path(self.data_dir, job_id)
        model_id = (j.result or {}).get("model_id") or j.payload.get("model_id")
        meta = self._model(model_id)
        if not path.is_file():
            raise ViewerError("evidence_missing", "That benchmark's promotion report is missing; run the "
                              "benchmark again.", 409)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ViewerError("evidence_invalid", "That benchmark's promotion report is unreadable.", 409) from None
        if report.get("promote") is not True:
            failed = ", ".join(report.get("failed_conditions") or []) or "not recommended"
            raise ViewerError("promotion_not_recommended",
                              f"The benchmark does not recommend promoting {model_id} ({failed}).", 409)
        with self._lock:
            try:
                entry = self.registry.promote(meta["model_id"], path)
            except RegistryError as exc:
                raise ViewerError(exc.code, _plain(exc.message), 409) from None
        return {"model_id": entry["model_id"], "kind": entry["kind"], "promoted_at": entry["promoted_at"],
                "evidence_sha256": entry["evidence_sha256"], "dataset_id": entry.get("dataset_id"),
                "benchmark_job_id": job_id}

    def deactivate(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind = body.get("kind")
        if kind not in KINDS:
            raise ViewerError("invalid_model_kind", "kind must be verifier or detector.")
        with self._lock:
            previous = self.registry.deactivate(kind)
        return {"kind": kind, "deactivated": previous}


def _plain(message: str) -> str:
    """A registry message without server paths."""
    return re.sub(r"(/[^\s'\"]+)+", "<file>", message)
