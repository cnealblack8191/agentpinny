"""Viewer application service: upload, page frames, scans, review actions,
report export. The HTTP layer (``server.py``) is a thin wrapper around it.

Collaborators (contracts §§4, 5, 9):

* the render service, :class:`pinny.render.RenderService` (contracts §9):
  ``ingest_pdf``, ``get_version``, ``list_versions``, ``page_frame``,
  ``render_page``, ``render_page_png`` and ``crop_renderer``.
* the detector (``pinny.detection``).
* the learning store, which owns scans, pins and review events.
* Phase 2 learned models (docs/phase2-contracts.md P6-P8), through
  ``pinny.models.registry``. The model classes, and so torch, are imported
  only when a scan needs them, so ``template`` mode runs without torch.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import importlib
import importlib.util
import math
import os
import subprocess
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from pinny.learning import store as _store
from pinny.models.registry import ModelRegistry
from pinny.render import RenderService

from pinny.detection import (BoundingBox, DetectionError, OpenCVTemplateDetector,
                             ScanSettings, Template)

from .errors import ViewerError

SOURCE = "viewer"
REPORT_FORMAT = "pinny.viewer.report"
REPORT_FORMAT_VERSION = 1

# Namespace for scan ids derived from the client's scan request id, so a
# retried POST converges on one scan.
_SCAN_NS = uuid.UUID("0b8f7a4e-2d7c-4f7e-9a51-3c9e3f1d6a10")
# Namespace for batch ids (from the client's request id) and for each batch
# page's scan request id (from batch id and page), so retries converge.
_BATCH_NS = uuid.UUID("7c1d2e9a-4b6f-4a38-8e0d-5f2a9b3c7e41")

ACTIONS = ("approve", "reject", "delete_pin", "add_manual", "remove_manual")

# Scan modes (docs/phase2-contracts.md P7) and the model kind each one needs.
MODES = ("template", "template+verifier", "model")
MODE_MODEL_KIND = {"template": None, "template+verifier": "verifier", "model": "detector"}
DETECTOR_NAMES = {"template": "opencv-template", "template+verifier": "opencv-template+verifier",
                  "model": "pinny-point-detector"}
MODEL_BOX_PX = 40  # P7: a model-mode detection gets a 40 x 40 box centred on its point
MODEL_MAX_POINTS = 500

# The P6 inference classes, imported only when a scan needs one.
_MODEL_CLASSES = {"verifier": ("pinny.models.verifier", "Verifier"),
                  "detector": ("pinny.models.point_detector", "PointDetector")}


def _git_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        sha = ""
    return f"git:{sha}" if sha else "git:unknown"


def _settings_dict(settings: ScanSettings) -> Dict[str, Any]:
    d = dataclasses.asdict(settings)
    d["rotations"] = list(settings.rotations)
    return d


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


class ViewerService:
    def __init__(self, data_dir: Optional[os.PathLike] = None, *, render=None,
                 detector=None, model_classes: Optional[Dict[str, Any]] = None) -> None:
        """``model_classes`` maps a model kind (``verifier``, ``detector``) to a
        class with the P6 ``load(model_dir)`` classmethod. Kinds left out use
        the real classes, imported lazily. Tests pass fakes here."""
        self.data_dir = Path(data_dir) if data_dir is not None else _store.default_data_dir()
        self.render = render if render is not None else RenderService(self.data_dir)
        self.detector = detector or OpenCVTemplateDetector()
        self.detector_version = _git_version()
        self.registry = ModelRegistry(self.data_dir)
        self._model_classes = dict(model_classes or {})
        self._loaded: Dict[tuple, Any] = {}  # (kind, model_id) -> loaded model
        self._model_lock = threading.Lock()
        # The learning store holds one SQLite connection, which must stay on
        # the thread that opened it. All store calls go through this single
        # worker; detection runs on the caller's thread.
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pinny-store")
        self.store = self._db(_store.LearningStore, self.data_dir,
                              crop_renderer=self.render.crop_renderer,
                              default_reviewer=_store.local_reviewer_identity())
        # Batch scans run one at a time, one page at a time, on this worker,
        # which bounds memory to one page raster plus the detector's work.
        self._batch_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pinny-batch")
        self._batch_jobs: Dict[str, Future] = {}
        self._batch_lock = threading.Lock()
        self._closing = threading.Event()

    def _db(self, fn, *args, **kwargs):
        return self._exec.submit(fn, *args, **kwargs).result()

    def close(self) -> None:
        # Stop between pages: the page being scanned finishes, later pages stay
        # pending and resume when the batch is started or resumed again.
        self._closing.set()
        self._batch_exec.shutdown(wait=True, cancel_futures=True)
        self._db(self.store.close)
        self._exec.shutdown()

    # -------------------------------------------------------------- documents
    def upload(self, data: bytes, filename: str, document_id: Optional[str] = None) -> dict:
        return self._doc(self.render.ingest_pdf(data, original_filename=filename,
                                                document_id=document_id))

    def documents(self) -> List[dict]:
        return [self._doc(v) for v in self.render.list_versions()]

    def document_info(self, document_version: str) -> dict:
        return self._doc(self.render.get_version(document_version))

    def _doc(self, v) -> dict:
        return {"document_id": v.document_id, "document_version": v.document_version,
                "filename": v.original_filename, "page_count": v.page_count,
                "render_service": getattr(self.render, "render_service", "foundation")}

    def frame(self, document_version: str, page_index: int) -> dict:
        frame = dict(self.render.page_frame(document_version, page_index))
        frame["render_service"] = getattr(self.render, "render_service", "foundation")
        return frame

    def raster_png(self, document_version: str, page_index: int) -> bytes:
        return self.render.render_page_png(document_version, page_index)

    def page_scans(self, document_version: str, page_index: int) -> List[dict]:
        """Scans of one page, oldest first, with review counts."""
        page_id = f"{document_version}#p{page_index}"
        out = []
        for s in self._db(self.store.list_scans, document_version=document_version,
                          page_index=page_index):
            st = self._db(self.store.load_scan, s.scan_id)
            result = s.metadata.get("scan_result", {})
            out.append({"scan_id": s.scan_id, "created_at": s.created_at,
                        "mode": result.get("mode", "template"),
                        "template": result.get("template"),
                        "counts": _counts(st.pins)})
        return out

    # ----------------------------------------------------------------- models
    def _model_class(self, kind: str):
        cls = self._model_classes.get(kind)
        if cls is not None:
            return cls
        module, name = _MODEL_CLASSES[kind]
        try:
            # The model modules import torch lazily, so check for it here.
            if importlib.util.find_spec("torch") is None:
                raise ImportError("No module named 'torch'")
            return getattr(importlib.import_module(module), name)
        except ImportError as exc:
            raise ViewerError(
                "models_unavailable",
                f"The {kind} model cannot be loaded here ({exc}). Install the train extra "
                "(pip install -e '.[train]'), or scan in 'template' mode.", 503) from None

    def _models_importable(self, kind: str) -> bool:
        """Whether a model of this kind could be loaded, without importing torch."""
        if kind in self._model_classes:
            return True
        try:
            return (importlib.util.find_spec("torch") is not None
                    and importlib.util.find_spec(_MODEL_CLASSES[kind][0]) is not None)
        except (ImportError, ValueError):
            return False

    def models(self) -> dict:
        """The scan modes, whether each one can run, and the active models."""
        active = {k: self.registry.active(k) for k in ("verifier", "detector")}
        modes = []
        for mode in MODES:
            kind = MODE_MODEL_KIND[mode]
            entry = {"mode": mode, "available": True, "kind": kind, "model_id": None,
                     "reason": None, "needs_template": kind != "detector"}
            if kind is not None:
                mid = active[kind]
                entry["model_id"] = mid
                if mid is None:
                    entry.update(available=False, reason=f"No {kind} model is active. "
                                 "Promote one with python -m pinny.models.registry promote.")
                elif not self._models_importable(kind):
                    entry.update(available=False, reason="torch or the model code is not "
                                 "installed; install the train extra.")
                else:
                    try:
                        meta = self.registry.get(mid)
                        entry["threshold"] = (meta.get("operating_point") or {}).get("threshold")
                        entry["synthetic_only"] = bool(meta.get("synthetic_only"))
                    except Exception as exc:  # noqa: BLE001 - report, don't fail the listing
                        entry.update(available=False, reason=str(exc))
            modes.append(entry)
        return {"modes": modes, "active": active}

    def _active_model(self, mode: str):
        """The loaded, active model for ``mode``, or a clear error."""
        kind = MODE_MODEL_KIND[mode]
        model_id = self.registry.active(kind)
        if model_id is None:
            raise ViewerError("model_not_active",
                              f"Scan mode {mode!r} needs an active {kind} model, and none is "
                              "promoted. Promote one with 'python -m pinny.models.registry "
                              "promote <model_id> --evidence <report>', or use 'template'.",
                              409)
        key = (kind, model_id)
        with self._model_lock:
            model = self._loaded.get(key)
            if model is None:
                cls = self._model_class(kind)
                model_dir = self.registry.model_dir(model_id)
                try:
                    model = cls.load(model_dir)
                except ViewerError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "code", None)
                    raise ViewerError(code if isinstance(code, str) else "model_load_failed",
                                      f"Could not load {kind} {model_id}: {exc}", 500) from None
                if getattr(model, "model_id", model_id) != model_id:
                    raise ViewerError("model_mismatch",
                                      f"{model_dir} loaded as {model.model_id!r}, but the "
                                      f"registry says {model_id!r}.", 500)
                self._loaded[key] = model
        return model_id, model

    # ------------------------------------------------------------------ scans
    def scan(self, *, document_version: str, page_index: int,
             template_box: Optional[dict] = None, request_id: str,
             threshold: Optional[float] = None, mode: Optional[str] = None,
             model_threshold: Optional[float] = None,
             template_page_index: Optional[int] = None) -> dict:
        """Run one scan and record it immutably (contracts §4, P7).

        ``mode`` is ``template`` (the default, Phase 1), ``template+verifier``
        or ``model``. ``threshold`` is the template-matching threshold and
        applies to the two template modes. ``model_threshold`` overrides the
        active model's operating point. ``template_page_index`` is the page
        the template box was drawn on (default: the scanned page).

        ``request_id`` is the client's id for this scan request. The scan id
        is derived from it, so a retried request returns the first result.
        """
        _require_uuid(request_id, "request_id")
        mode = self._check_scan_args(mode, template_box, threshold, model_threshold)
        scan_id = str(uuid.uuid5(_SCAN_NS, request_id))
        try:
            return self.scan_state(scan_id)
        except ViewerError:
            pass
        info = self.document_info(document_version)
        self.render.page_frame(document_version, page_index)  # 404s for an unknown page
        template = None
        if mode != "model":
            template = self._template(document_version,
                                      page_index if template_page_index is None else template_page_index,
                                      template_box, record_page=template_page_index is not None)
        model = self._active_model(mode) if mode != "template" else (None, None)
        return self._scan_page(info, page_index, scan_id, request_id, mode, template, model,
                               threshold, model_threshold)

    @staticmethod
    def _check_scan_args(mode: Optional[str], template_box: Any, threshold: Any,
                         model_threshold: Any) -> str:
        """Validate the mode and thresholds of a scan or batch; returns the mode."""
        mode = "template" if mode is None else mode
        if mode not in MODES:
            raise ViewerError("invalid_mode",
                              f"Unknown scan mode {mode!r}; use one of {', '.join(MODES)}.")
        if threshold is not None and (not _num(threshold) or not -1 <= threshold <= 1):
            raise ViewerError("invalid_threshold", "threshold must be a number in [-1, 1].")
        if model_threshold is not None:
            if mode == "template":
                raise ViewerError("invalid_threshold",
                                  "model_threshold applies only to the model scan modes.")
            if not _num(model_threshold) or not 0 <= model_threshold <= 1:
                raise ViewerError("invalid_threshold", "model_threshold must be in [0, 1].")
        if mode == "model":
            if template_box is not None:
                raise ViewerError("template_not_used",
                                  "Scan mode 'model' needs no template box; leave it out.")
            if threshold is not None:
                raise ViewerError("invalid_threshold",
                                  "threshold is the template-matching threshold; in 'model' "
                                  "mode use model_threshold.")
        return mode

    def _template(self, document_version: str, page_index: int, template_box: Any,
                  record_page: bool) -> tuple:
        """Crop the template from ``page_index``. Returns ``(Template, meta)``,
        where ``meta`` is the scan result's ``template`` object (contracts §4;
        ``page_index`` is the v1.2 additive field, set when ``record_page``)."""
        frame = self.render.page_frame(document_version, page_index)
        box = _template_box(template_box, frame)
        page = self._page(document_version, page_index, frame)
        try:
            template = Template.from_page_crop(page, box)
        except DetectionError as exc:
            raise ViewerError(exc.code, str(exc)) from exc
        meta = {"box": box.to_dict(), "sha256": hashlib.sha256(template.image.tobytes()).hexdigest()}
        if record_page:
            meta["page_index"] = page_index
        return template, meta

    def _page(self, document_version: str, page_index: int, frame: dict):
        page = self.render.render_page(document_version, page_index)
        if page.shape[:2] != (frame["height"], frame["width"]):
            raise ViewerError("frame_mismatch",
                              "The rendered page does not match its frame size; cannot scan.",
                              500)
        return page

    def _scan_page(self, info: dict, page_index: int, scan_id: str, request_id: str, mode: str,
                   template: Optional[tuple], model: tuple, threshold: Optional[float],
                   model_threshold: Optional[float], extra: Optional[dict] = None) -> dict:
        """Detect on one page and record the scan. ``template`` is
        ``(Template, meta)`` from ``_template`` (``None`` in model mode) and
        ``model`` is ``(model_id, model)`` (``(None, None)`` in template mode).
        ``extra`` is merged into the scan result (e.g. its batch)."""
        document_version = info["document_version"]
        model_id, model = model
        frame = self.render.page_frame(document_version, page_index)
        page = self._page(document_version, page_index, frame)

        document = {"document_id": info["document_id"], "document_version": document_version,
                    "page_index": page_index}
        scan_result = {"scan_id": scan_id, "mode": mode, "document": document,
                       "coordinate_frame": dict(frame)}
        if mode == "model":
            scan_result.update(self._model_scan(page, frame, model_id, model, model_threshold))
        else:
            tpl, tpl_meta = template
            settings = (ScanSettings() if threshold is None
                        else ScanSettings(threshold=float(threshold)))
            try:
                result = self.detector.detect(page, tpl, settings)
            except DetectionError as exc:
                raise ViewerError(exc.code, str(exc)) from exc
            scan_result["template"] = dict(tpl_meta)
            candidates = []
            for c in result.candidates:
                cx, cy = c.center
                candidates.append({"box": c.box.to_dict(), "x": cx, "y": cy,
                                   "score": float(c.score), "rotation": c.rotation,
                                   "source": "detector"})
            if mode == "template":
                scan_result["detector"] = {"name": result.detector,
                                           "version": self.detector_version,
                                           "settings": _settings_dict(settings)}
                detections = candidates
            else:
                detections, suppressed, vsettings = self._verify(
                    page, candidates, model_id, model, model_threshold)
                scan_result["detector"] = {
                    "name": DETECTOR_NAMES[mode], "version": model_id,
                    "settings": dict(_settings_dict(settings), verifier=vsettings,
                                     code_version=self.detector_version)}
                scan_result["suppressed"] = suppressed
            scan_result["truncated"] = result.truncated
            scan_result["warnings"] = list(result.warnings)
            scan_result["detections"] = detections
        for n, d in enumerate(scan_result["detections"], start=1):
            d["id"] = f"det-{n}"
        scan_result.update(extra or {})
        scan_result["created_at"] = _now()

        scan, dets = _store.Scan.from_scan_result(scan_result)
        scan = dataclasses.replace(scan, metadata={"scan_result": scan_result,
                                                   "request_id": request_id})
        self._db(self.store.record_scan, scan, dets)
        return self.scan_state(scan_id)

    def _verify(self, page, candidates: List[dict], model_id: str, model,
                model_threshold: Optional[float]):
        """Rescore template candidates with the verifier (P7). Returns the kept
        detections (``score`` = verifier score), the suppressed candidates and
        the verifier settings."""
        threshold = float(model.threshold if model_threshold is None else model_threshold)
        points = [(c["x"], c["y"]) for c in candidates]
        scores = self._call_model(model, "score", page, points) if points else []
        scores = list(scores) if hasattr(scores, "__len__") else scores
        if not isinstance(scores, list) or len(scores) != len(points):
            raise ViewerError("model_output_invalid",
                              f"Verifier {model_id} did not return one score per candidate "
                              f"({len(points)} candidates).", 500)
        kept, suppressed = [], []
        for c, v in zip(candidates, scores):
            v = _probability(v, model_id)
            d = dict(c, template_score=c["score"], verifier_score=v, score=v)
            (kept if v >= threshold else suppressed).append(d)
        # Kept detections are ranked by verifier score; ties keep template order.
        kept.sort(key=lambda d: -d["verifier_score"])
        for n, d in enumerate(suppressed, start=1):
            d["id"] = f"sup-{n}"
            d["suppressed_by"] = "verifier"
        return kept, suppressed, {"model_id": model_id, "threshold": threshold,
                                  "threshold_source": "model" if model_threshold is None
                                  else "request"}

    def _model_scan(self, page, frame: dict, model_id: str, model,
                    model_threshold: Optional[float]) -> dict:
        threshold = float(model.threshold if model_threshold is None else model_threshold)
        points = self._call_model(model, "detect_points", page, threshold=threshold,
                                  max_points=MODEL_MAX_POINTS)
        if not isinstance(points, (list, tuple)):
            raise ViewerError("model_output_invalid",
                              f"Point detector {model_id} did not return a list.", 500)
        half = MODEL_BOX_PX / 2
        detections = []
        for p in points:
            try:
                x, y = float(p["x"]), float(p["y"])
            except (KeyError, TypeError, ValueError):
                raise ViewerError("model_output_invalid",
                                  f"Point detector {model_id} returned {p!r}; expected "
                                  "{x, y, score}.", 500) from None
            if not (math.isfinite(x) and math.isfinite(y)
                    and 0 <= x <= frame["width"] and 0 <= y <= frame["height"]):
                raise ViewerError("model_output_invalid",
                                  f"Point detector {model_id} returned ({x}, {y}), outside the "
                                  f"{frame['width']}x{frame['height']} page.", 500)
            score = _probability(p.get("score"), model_id)
            detections.append({"box": {"x": int(round(x - half)), "y": int(round(y - half)),
                                       "width": MODEL_BOX_PX, "height": MODEL_BOX_PX},
                               "x": x, "y": y, "score": score, "rotation": 0,
                               "source": "detector"})
        detections.sort(key=lambda d: (-d["score"], d["y"], d["x"]))
        return {
            "template": None,
            "detector": {"name": DETECTOR_NAMES["model"], "version": model_id,
                         "settings": {"model_id": model_id, "threshold": threshold,
                                      "threshold_source": "model" if model_threshold is None
                                      else "request",
                                      "max_points": MODEL_MAX_POINTS, "box_px": MODEL_BOX_PX,
                                      "code_version": self.detector_version}},
            "truncated": len(detections) >= MODEL_MAX_POINTS,
            "warnings": [],
            "detections": detections,
        }

    @staticmethod
    def _call_model(model, method: str, *args, **kwargs):
        try:
            return getattr(model, method)(*args, **kwargs)
        except ViewerError:
            raise
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            raise ViewerError(code if isinstance(code, str) else "model_failed",
                              f"{type(model).__name__}.{method} failed: {exc}", 500) from None

    def scan_state(self, scan_id: str) -> dict:
        try:
            st = self._db(self.store.load_scan, scan_id)
        except _store.NotFound:
            raise ViewerError("unknown_scan", "That scan does not exist.", 404) from None
        result = st.scan.metadata.get("scan_result", {})
        scores = {d.detection_id: d.score for d in st.detections}
        rotations = {d.detection_id: d.rotation for d in st.detections}
        extras = _extras(st.detections)
        return {
            "scan_id": scan_id,
            "mode": result.get("mode", "template"),
            "document": result.get("document"),
            "coordinate_frame": result.get("coordinate_frame"),
            "template": result.get("template"),
            "detector": result.get("detector"),
            "created_at": result.get("created_at"),
            "truncated": result.get("truncated", False),
            "warnings": result.get("warnings", []),
            "pins": [_pin_dict(p, scores, rotations, extras) for p in st.pins],
            "suppressed": result.get("suppressed", []),
            "counts": _counts(st.pins),
            "batch": result.get("batch"),
        }

    # ---------------------------------------------------------------- batches
    def start_batch(self, *, document_version: str, request_id: str,
                    template_box: Optional[dict] = None,
                    template_page_index: Optional[int] = None,
                    page_indexes: Optional[List[int]] = None, mode: Optional[str] = None,
                    threshold: Optional[float] = None,
                    model_threshold: Optional[float] = None) -> dict:
        """Scan many pages with one template, in the background (contracts §5a).

        The template box is drawn on ``template_page_index`` and matched on
        every page in ``page_indexes`` (default: all pages, in order). Each
        page gets its own ordinary scan, recorded as soon as it finishes, so
        review can start on the first pages while later ones are scanned. A
        page that fails is recorded as failed and the batch moves on.

        The batch id is derived from ``request_id``. Repeating the request
        returns the batch as it stands and resumes pages a shutdown or crash
        interrupted; the same ``request_id`` with other arguments is a
        ``request_conflict``.
        """
        _require_uuid(request_id, "request_id")
        mode = self._check_scan_args(mode, template_box, threshold, model_threshold)
        info = self.document_info(document_version)
        count = int(info["page_count"])
        if page_indexes is None:
            pages = list(range(count))
        else:
            if not isinstance(page_indexes, list) or not page_indexes:
                raise ViewerError("invalid_pages", "page_indexes must be a non-empty list.")
            if any(isinstance(i, bool) or not isinstance(i, int) for i in page_indexes):
                raise ViewerError("invalid_pages", "page_indexes must be integers.")
            if len(set(page_indexes)) != len(page_indexes):
                raise ViewerError("invalid_pages", "page_indexes lists a page twice.")
            bad = [i for i in page_indexes if not 0 <= i < count]
            if bad:
                raise ViewerError("page_not_found",
                                  f"Page(s) {bad} are not in this {count}-page document "
                                  f"(pages are numbered from 0).", 404)
            pages = list(page_indexes)
        if not pages:
            raise ViewerError("invalid_pages", "The document has no pages to scan.")
        tpl_meta: Dict[str, Any] = {}
        if mode != "model":
            if template_page_index is None:
                template_page_index = pages[0]
            if (isinstance(template_page_index, bool) or not isinstance(template_page_index, int)
                    or not 0 <= template_page_index < count):
                raise ViewerError("invalid_page", "template_page_index must be a page of this "
                                  "document.")
            _, tpl_meta = self._template(document_version, template_page_index, template_box,
                                         record_page=True)
        elif template_page_index is not None:
            raise ViewerError("template_not_used",
                              "Scan mode 'model' needs no template; leave template_page_index out.")
        if mode != "template":
            self._active_model(mode)  # fail now, not once per page

        batch_id = str(uuid.uuid5(_BATCH_NS, request_id))
        settings = {"threshold": threshold, "model_threshold": model_threshold}
        try:
            self._db(self.store.create_batch, batch_id, document_id=info["document_id"],
                     document_version=document_version, page_indexes=pages, mode=mode,
                     settings=settings, template_page_index=template_page_index if tpl_meta else None,
                     template_box=tpl_meta.get("box"), template_sha256=tpl_meta.get("sha256"),
                     metadata={"request_id": request_id})
        except _store.IdempotencyConflict:
            raise ViewerError("request_conflict",
                              "This request_id already started a different batch; use a new "
                              "request_id.", 409) from None
        return self.resume_batch(batch_id)

    def resume_batch(self, batch_id: str, retry_failed: bool = False) -> dict:
        """Continue a batch: requeue interrupted (and, with ``retry_failed``,
        failed) pages and make sure its job is running. A no-op for a batch
        that is complete or cancelled."""
        self._batch(batch_id)  # 404s
        with self._batch_lock:
            job = self._batch_jobs.get(batch_id)
            if job is None or job.done():
                if self._closing.is_set():
                    raise ViewerError("shutting_down", "The viewer is shutting down.", 503)
                # Nothing of this batch is running, so a "running" page was
                # interrupted and is safe to requeue.
                self._db(self.store.requeue_batch, batch_id, retry_failed=bool(retry_failed))
                self._batch_jobs[batch_id] = self._batch_exec.submit(self._run_batch, batch_id)
            elif retry_failed:
                # A job is mid-batch: leave its running page alone, and queue a
                # follow-up job (it starts after this one) for the retried pages.
                self._db(self.store.requeue_batch, batch_id, retry_failed=True,
                         interrupted=False)
                self._batch_jobs[batch_id] = self._batch_exec.submit(self._run_batch, batch_id)
        return self.batch_state(batch_id)

    def cancel_batch(self, batch_id: str) -> dict:
        """Skip every page not yet started. The page being scanned finishes."""
        self._batch(batch_id)
        self._db(self.store.cancel_batch, batch_id)
        return self.batch_state(batch_id)

    def wait_batch(self, batch_id: str, timeout: Optional[float] = None) -> dict:
        """Block until the batch's current job ends (for tests and scripts)."""
        with self._batch_lock:
            job = self._batch_jobs.get(batch_id)
        if job is not None:
            job.result(timeout=timeout)
        return self.batch_state(batch_id)

    def _run_batch(self, batch_id: str) -> None:
        """Scan a batch's pending pages in order, one at a time."""
        b = self._db(self.store.get_batch, batch_id)
        threshold = b.settings.get("threshold")
        model_threshold = b.settings.get("model_threshold")
        info, template, model, setup_error = None, None, (None, None), None
        try:
            info = self.document_info(b.document_version)
            if b.mode != "model":
                template = self._template(b.document_version, b.template_page_index,
                                          b.template_box, record_page=True)
                if template[1]["sha256"] != b.template_sha256:
                    raise ViewerError("template_changed",
                                      "The template page no longer renders the same pixels; "
                                      "start a new batch.", 409)
            if b.mode != "template":
                model = self._active_model(b.mode)
        except Exception as exc:  # noqa: BLE001 - recorded on each page below
            setup_error = exc
        while not self._closing.is_set():
            page_index = self._db(self.store.next_batch_page, batch_id)
            if page_index is None:
                return
            try:
                if setup_error is not None:
                    raise setup_error
                request_id = str(uuid.uuid5(_BATCH_NS, f"{batch_id}#p{page_index}"))
                scan_id = str(uuid.uuid5(_SCAN_NS, request_id))
                try:  # a scan recorded before an interruption is reused
                    self._db(self.store.load_scan, scan_id)
                except _store.NotFound:
                    self._scan_page(info, page_index, scan_id, request_id, b.mode, template, model,
                                    threshold, model_threshold,
                                    extra={"batch": {"batch_id": batch_id,
                                                     "page_index": page_index}})
                self._db(self.store.finish_batch_page, batch_id, page_index, scan_id)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the batch
                code = getattr(exc, "code", None)
                self._db(self.store.fail_batch_page, batch_id, page_index,
                         code if isinstance(code, str) else "scan_failed",
                         str(exc) or type(exc).__name__)

    def _batch(self, batch_id: str):
        try:
            return self._db(self.store.get_batch, batch_id)
        except _store.NotFound:
            raise ViewerError("unknown_batch", "That batch does not exist.", 404) from None

    def batch_state(self, batch_id: str) -> dict:
        return _batch_dict(self._batch(batch_id))

    def document_batches(self, document_version: str) -> List[dict]:
        """Batches of one document version, oldest first."""
        return [_batch_dict(b) for b in self._db(self.store.list_batches, document_version)]

    def batch_queue(self, batch_id: str, strategy: Optional[str] = None,
                    limit: Optional[int] = None) -> dict:
        """Unreviewed pins across every finished page, most uncertain first.
        Each item names its ``scan_id`` and ``pin_id``; review it with the
        ordinary ``act(scan_id, ...)``."""
        self._batch(batch_id)
        strategy = strategy or "margin"
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ViewerError("invalid_limit", "limit must be a non-negative integer.")
        try:
            items = self._db(self.store.batch_review_queue, batch_id, strategy, limit=limit)
        except _store.InvalidArgument as exc:
            raise ViewerError("invalid_strategy", f"{exc}; use margin or lowest_score.") from None
        return {"batch_id": batch_id, "strategy": strategy,
                "items": [{"scan_id": i.scan_id, "page_index": i.page_index, "pin_id": i.pin_id,
                           "score": i.score, "x": i.x, "y": i.y,
                           "box": i.box.to_dict() if i.box is not None else None,
                           "rotation": i.rotation, "version": i.version} for i in items]}

    # ---------------------------------------------------------------- reviews
    def act(self, scan_id: str, body: dict) -> dict:
        """Apply one review action (contracts §5)."""
        action = body.get("action")
        if action not in ACTIONS:
            raise ViewerError("unknown_action", f"Unknown action {action!r}.")
        request_id = body.get("request_id")
        _require_uuid(request_id, "request_id")
        expected = body.get("expected_version")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            raise ViewerError("invalid_version", "expected_version must be an integer.")
        state = self.scan_state(scan_id)  # 404s for an unknown scan
        kw = {"request_id": request_id, "source": SOURCE}
        try:
            if action == "add_manual":
                frame = state["coordinate_frame"]
                x, y = _point(body, frame)
                res = self._db(self.store.add_manual, scan_id, x, y, **kw)
            else:
                pin_id = body.get("pin_id")
                if not isinstance(pin_id, str) or not pin_id:
                    raise ViewerError("missing_pin", "pin_id is required for this action.")
                method = getattr(self.store, action)
                res = self._db(method, scan_id, pin_id, expected_version=expected, **kw)
        except _store.NotFound:
            raise ViewerError("unknown_pin", "That pin does not exist in this scan.", 404) from None
        except _store.StaleVersion as exc:
            raise ViewerError("stale_version",
                              f"The pin was changed elsewhere ({exc}). Reload to see it.",
                              409) from None
        except _store.InvalidTransition as exc:
            raise ViewerError("invalid_transition", str(exc), 409) from None
        except _store.IdempotencyConflict as exc:
            raise ViewerError("request_conflict", str(exc), 409) from None
        by_det = {p["detection_id"]: p for p in state["pins"] if p["detection_id"]}
        scores = {k: p.get("score") for k, p in by_det.items()}
        rotations = {k: p.get("rotation") for k, p in by_det.items()}
        extras = {k: {e: p[e] for e in _EXTRA_KEYS if p.get(e) is not None}
                  for k, p in by_det.items()}
        return {"pin": _pin_dict(res.pin, scores, rotations, extras),
                "event_id": res.event.event_id, "action": res.event.action,
                "replayed": res.replayed}

    # ----------------------------------------------------------------- export
    def report(self, scan_id: str) -> dict:
        """Report with the original detector output and the corrected pins
        kept apart (integration check I7)."""
        try:
            st = self._db(self.store.load_scan, scan_id)
        except _store.NotFound:
            raise ViewerError("unknown_scan", "That scan does not exist.", 404) from None
        result = st.scan.metadata["scan_result"]
        original = {
            "format": "pinny.detections",
            "format_version": 1,
            "provenance": "original_detector_output",
            "scan_id": result["scan_id"],
            "document": result["document"],
            "coordinate_frame": result["coordinate_frame"],
            "template": result["template"],
            "detector": result["detector"],
            "created_at": result["created_at"],
            "detections": [dict(d, confidence=d["score"]) for d in result["detections"]],
        }
        if original["template"] is None:
            # Model-mode scans have no template. Leave the key out rather than
            # sending null, which the Phase 1 evaluator does not accept.
            del original["template"]
        if "mode" in result:  # Phase 1 scans keep their exact Phase 1 export
            original["mode"] = result["mode"]
        if "suppressed" in result:
            original["suppressed"] = result["suppressed"]
        scores = {d.detection_id: d.score for d in st.detections}
        rotations = {d.detection_id: d.rotation for d in st.detections}
        pins = [_pin_dict(p, scores, rotations, _extras(st.detections)) for p in st.pins]
        corrected = {
            "format": "pinny.corrected_pins",
            "format_version": 1,
            "provenance": "reviewed_corrections",
            "scan_id": scan_id,
            "document": result["document"],
            "coordinate_frame": result["coordinate_frame"],
            "pins": pins,
            "final_pins": [{"pin_id": p["pin_id"], "x": p["x"], "y": p["y"],
                            "origin": p["origin"], "state": p["state"]}
                           for p in pins if p["state"] in ("approved", "added")],
            "counts": _counts(st.pins),
        }
        return {
            "format": REPORT_FORMAT,
            "format_version": REPORT_FORMAT_VERSION,
            "exported_at": _now(),
            "document": result["document"],
            "coordinate_frame": result["coordinate_frame"],
            "original_detections": original,
            "corrected_pins": corrected,
            "review_events": [{"event_id": e.event_id, "request_id": e.request_id,
                               "pin_id": e.pin_id, "action": e.action, "source": e.source,
                               "reviewer": e.reviewer, "created_at": e.created_at}
                              for e in st.events],
        }


# ------------------------------------------------------------------ helpers
def _require_uuid(value: Any, name: str) -> None:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise ViewerError("invalid_request_id", f"{name} must be a UUID.") from None
    if not isinstance(value, str):
        raise ViewerError("invalid_request_id", f"{name} must be a UUID string.")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _template_box(b: Any, frame: dict) -> BoundingBox:
    if not isinstance(b, dict) or not all(_num(b.get(k)) for k in ("x", "y", "width", "height")):
        raise ViewerError("invalid_template_box",
                          "template_box needs numeric x, y, width and height.")
    vals = [b["x"], b["y"], b["width"], b["height"]]
    if any(int(v) != v for v in vals):
        raise ViewerError("invalid_template_box", "template_box must be whole canonical pixels.")
    x, y, w, h = (int(v) for v in vals)
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > frame["width"] or y + h > frame["height"]:
        raise ViewerError("invalid_template_box",
                          "The selection must be a non-empty rectangle inside the page.")
    return BoundingBox(x, y, w, h)


def _point(body: dict, frame: dict):
    x, y = body.get("x"), body.get("y")
    if not (_num(x) and _num(y)):
        raise ViewerError("invalid_point", "x and y must be numbers in canonical pixels.")
    if not (0 <= x <= frame["width"] and 0 <= y <= frame["height"]):
        raise ViewerError("point_outside_page",
                          f"({x}, {y}) is outside the {frame['width']}x{frame['height']} page.")
    return float(x), float(y)


def _probability(v: Any, model_id: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = float("nan")
    if not (math.isfinite(f) and 0.0 <= f <= 1.0):
        raise ViewerError("model_output_invalid",
                          f"Model {model_id} returned score {v!r}; scores must be in [0, 1].",
                          500)
    return f


# Per-detection extras (P7) shown on pins when present.
_EXTRA_KEYS = ("template_score", "verifier_score")


def _extras(detections) -> dict:
    return {d.detection_id: {k: d.raw[k] for k in _EXTRA_KEYS if k in d.raw}
            for d in detections}


def _pin_dict(p, scores: dict, rotations: dict, extras: Optional[dict] = None) -> dict:
    box = p.box.to_dict() if p.box is not None else None
    out = {"pin_id": p.pin_id, "origin": p.origin, "state": p.state, "x": p.x, "y": p.y,
           "box": box, "detection_id": p.detection_id,
           "score": scores.get(p.detection_id) if p.detection_id else None,
           "rotation": rotations.get(p.detection_id) if p.detection_id else None,
           "version": p.version, "updated_at": p.updated_at}
    if extras and p.detection_id:
        out.update(extras.get(p.detection_id) or {})
    return out


def _batch_dict(b) -> dict:
    return {"batch_id": b.batch_id, "status": b.status, "mode": b.mode,
            "document": {"document_id": b.document_id, "document_version": b.document_version},
            "template": ({"page_index": b.template_page_index, "box": b.template_box,
                          "sha256": b.template_sha256} if b.template_box else None),
            "settings": b.settings, "created_at": b.created_at, "updated_at": b.updated_at,
            "cancelled_at": b.cancelled_at, "page_counts": b.page_counts,
            "pin_counts": b.pin_counts,
            "pages": [{"page_index": p.page_index, "status": p.status, "scan_id": p.scan_id,
                       "error": ({"code": p.error_code, "message": p.error_message}
                                 if p.error_code else None),
                       "attempts": p.attempts, "started_at": p.started_at,
                       "finished_at": p.finished_at, "counts": p.counts,
                       "review_complete": p.review_complete} for p in b.pages]}


def _counts(pins) -> dict:
    c = {"unreviewed": 0, "approved": 0, "rejected": 0, "added": 0, "removed": 0}
    for p in pins:
        c[p.state] = c.get(p.state, 0) + 1
    c["total"] = len(pins)
    return c
