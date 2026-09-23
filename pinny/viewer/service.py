"""Viewer application service: upload, page frames, scans, review actions,
report export. The HTTP layer (``server.py``) is a thin wrapper around it.

Collaborators (contracts §§4, 5, 9):

* a render service with ``register_upload``, ``list_documents``,
  ``document_info``, ``page_frame``, ``render_page``, ``render_page_png`` and
  ``crop_renderer``. Until the foundation pushes the real one, this is
  :class:`~pinny.viewer.render_stub.StubRenderService`.
* the detector (``pinny.detection``).
* the learning store, which owns scans, pins and review events.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # the learning store moves into pinny/ (contracts §8)
    from pinny.learning import store as _store  # type: ignore
except ImportError:  # pragma: no cover - until the move lands
    from pinny_learning import store as _store  # type: ignore

from pinny.detection import (BoundingBox, DetectionError, OpenCVTemplateDetector,
                             ScanSettings, Template)

from .errors import ViewerError

SOURCE = "viewer"
REPORT_FORMAT = "pinny.viewer.report"
REPORT_FORMAT_VERSION = 1

# Namespace for scan ids derived from the client's scan request id, so a
# retried POST converges on one scan.
_SCAN_NS = uuid.UUID("0b8f7a4e-2d7c-4f7e-9a51-3c9e3f1d6a10")

ACTIONS = ("approve", "reject", "delete_pin", "add_manual", "remove_manual")


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
                 detector=None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else _store.default_data_dir()
        if render is None:
            from .render_stub import StubRenderService
            render = StubRenderService(self.data_dir)
        self.render = render
        self.detector = detector or OpenCVTemplateDetector()
        self.detector_version = _git_version()
        # The learning store holds one SQLite connection, which must stay on
        # the thread that opened it. All store calls go through this single
        # worker; detection runs on the caller's thread.
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pinny-store")
        self.store = self._db(_store.LearningStore, self.data_dir,
                              crop_renderer=render.crop_renderer,
                              default_reviewer=_store.local_reviewer_identity())

    def _db(self, fn, *args, **kwargs):
        return self._exec.submit(fn, *args, **kwargs).result()

    def close(self) -> None:
        self._db(self.store.close)
        self._exec.shutdown()

    # -------------------------------------------------------------- documents
    def upload(self, data: bytes, filename: str, document_id: Optional[str] = None) -> dict:
        return self.render.register_upload(data, filename, document_id=document_id)

    def documents(self) -> List[dict]:
        return self.render.list_documents()

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
        for s in self._db(self.store.list_scans, document_version_id=document_version,
                          canonical_page_id=page_id):
            st = self._db(self.store.load_scan, s.scan_id)
            out.append({"scan_id": s.scan_id, "created_at": st.created_at,
                        "template": s.metadata.get("scan_result", {}).get("template"),
                        "counts": _counts(st.pins)})
        return out

    # ------------------------------------------------------------------ scans
    def scan(self, *, document_version: str, page_index: int, template_box: dict,
             request_id: str, threshold: Optional[float] = None) -> dict:
        """Run one template scan and record it immutably (contracts §4).

        ``request_id`` is the client's id for this scan request. The scan id
        is derived from it, so a retried request returns the first result.
        """
        _require_uuid(request_id, "request_id")
        scan_id = str(uuid.uuid5(_SCAN_NS, request_id))
        try:
            return self.scan_state(scan_id)
        except ViewerError:
            pass

        info = self.render.document_info(document_version)
        frame = self.render.page_frame(document_version, page_index)
        box = _template_box(template_box, frame)
        settings = ScanSettings() if threshold is None else ScanSettings(threshold=float(threshold))
        page = self.render.render_page(document_version, page_index)
        if page.shape[:2] != (frame["height"], frame["width"]):
            raise ViewerError("frame_mismatch",
                              "The rendered page does not match its frame size; cannot scan.",
                              500)
        try:
            template = Template.from_page_crop(page, box)
            result = self.detector.detect(page, template, settings)
        except DetectionError as exc:
            raise ViewerError(exc.code, str(exc)) from exc

        detections = []
        for n, c in enumerate(result.candidates, start=1):
            cx, cy = c.center
            detections.append({"id": f"det-{n}", "box": c.box.to_dict(), "x": cx, "y": cy,
                               "score": float(c.score), "rotation": c.rotation,
                               "source": "detector"})
        document = {"document_id": info["document_id"], "document_version": document_version,
                    "page_index": page_index}
        scan_result = {
            "scan_id": scan_id,
            "document": document,
            "coordinate_frame": dict(frame),
            "template": {"box": box.to_dict(),
                         "sha256": hashlib.sha256(template.image.tobytes()).hexdigest()},
            "detector": {"name": result.detector, "version": self.detector_version,
                         "settings": _settings_dict(settings)},
            "created_at": _now(),
            "truncated": result.truncated,
            "warnings": list(result.warnings),
            "detections": detections,
        }
        scan = _store.Scan(
            scan_id=scan_id, document_version_id=document_version,
            canonical_page_id=f"{document_version}#p{page_index}",
            page_width=frame["width"], page_height=frame["height"],
            detector_version=self.detector_version,
            matching_settings=_settings_dict(settings),
            template_id=scan_result["template"]["sha256"], page_index=page_index,
            metadata={"scan_result": scan_result, "request_id": request_id})
        dets = [_store.Detection(
            detection_id=d["id"],
            box=(d["box"]["x"], d["box"]["y"], d["box"]["x"] + d["box"]["width"],
                 d["box"]["y"] + d["box"]["height"]),
            score=d["score"], x=d["x"], y=d["y"],
            raw={"rotation": d["rotation"], "box": d["box"]}) for d in detections]
        self._db(self.store.record_scan, scan, dets)
        return self.scan_state(scan_id)

    def scan_state(self, scan_id: str) -> dict:
        try:
            st = self._db(self.store.load_scan, scan_id)
        except _store.NotFound:
            raise ViewerError("unknown_scan", "That scan does not exist.", 404) from None
        result = st.scan.metadata.get("scan_result", {})
        scores = {d.detection_id: d.score for d in st.detections}
        rotations = {d.detection_id: d.raw.get("rotation") for d in st.detections}
        return {
            "scan_id": scan_id,
            "document": result.get("document"),
            "coordinate_frame": result.get("coordinate_frame"),
            "template": result.get("template"),
            "detector": result.get("detector"),
            "created_at": result.get("created_at"),
            "truncated": result.get("truncated", False),
            "warnings": result.get("warnings", []),
            "pins": [_pin_dict(p, scores, rotations) for p in st.pins],
            "counts": _counts(st.pins),
        }

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
        scores = {p["pin_id"]: p.get("score") for p in state["pins"]}
        rotations = {p["pin_id"]: p.get("rotation") for p in state["pins"]}
        return {"pin": _pin_dict(res.pin, scores, rotations),
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
        scores = {d.detection_id: d.score for d in st.detections}
        rotations = {d.detection_id: d.raw.get("rotation") for d in st.detections}
        pins = [_pin_dict(p, scores, rotations) for p in st.pins]
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


def _pin_dict(p, scores: dict, rotations: dict) -> dict:
    box = None
    if p.box is not None:
        x0, y0, x1, y1 = p.box
        box = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    return {"pin_id": p.pin_id, "origin": p.origin, "state": p.state, "x": p.x, "y": p.y,
            "box": box, "detection_id": p.detection_id,
            "score": scores.get(p.detection_id) if p.detection_id else None,
            "rotation": rotations.get(p.detection_id) if p.detection_id else None,
            "version": p.version, "updated_at": p.updated_at}


def _counts(pins) -> dict:
    c = {"unreviewed": 0, "approved": 0, "rejected": 0, "added": 0, "removed": 0}
    for p in pins:
        c[p.state] = c.get(p.state, 0) + 1
    c["total"] = len(pins)
    return c
