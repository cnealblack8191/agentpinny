"""Legend workflow in the viewer: read a drawing set's legend, let a
reviewer check it, scan every sheet with it, and count what was found
(docs/legend-reader.md, docs/set-scanning.md, docs/training-site.md).

Storage, per document version (removed with the document)::

    documents/<sha256>/legend.json                        pinny.legend v1, with its edit log
    documents/<sha256>/set_scans/<run_id>/run.json        one whole-set scan: state, job, scan ids
    documents/<sha256>/set_scans/<run_id>/legend.json     the confirmed legend it scans with
    documents/<sha256>/set_scans/<run_id>/result.json     pinny.set_scan v1, written by the job

and one saved legend per engineer in ``legend_library/<name>.json``
(``pinny.legend_library`` v1).

Reading the legend (``read_legend``) and scanning the set (``scan_set``)
are sandboxed jobs; this process never opens the PDF. When a ``scan_set``
job finishes, this process (which owns the learning store) records its
result as one ordinary scan per sheet, each pin carrying its legend tag as
``class_label``. Recording is idempotent: scan ids and contents derive
from the run, so a restart part-way records the rest and repeats nothing.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pinny.jobs.legend_tasks import write_json_atomic
from pinny.jobs.queue import CANCELLED, DONE, FAILED, PRIORITY_BATCH, JobQueue
from pinny.learning import contract as _contract
from pinny.learning import store as _store
from pinny.legend import EngineerLibrary, Legend, LegendError, compare
from pinny.legend.reader import signature_similarity

from .errors import ViewerError

_log = logging.getLogger("pinny.viewer")

DETECTOR_NAME = "pinny-set-scan"
SCAN_MODE = "legend"
# Scan ids of a whole-set scan's sheets derive from its run id and page.
_RUN_NS = uuid.UUID("5e7d0c3a-9b1f-4c2e-8d6a-2f4b7a9c1e03")
_ENGINEER_RE = re.compile(r"^[\w .,&'()\-/]{1,80}$")

RUN_QUEUED, RUN_RUNNING, RUN_RECORDING, RUN_DONE, RUN_FAILED = "queued", "running", "recording", "done", "failed"
RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING, RUN_RECORDING)
POLL_S = 0.5

TEMPLATE_HINT = ("You can still count with a template: open a sheet, choose Template box, draw a box around "
                 "one symbol and press Scan.")

# What each legend flag means, for the reviewer. Blocking flags must be
# fixed or confirmed before the legend can be confirmed.
_FLAG_NOTES = {
    "combined": ("Two drawings in one row. Split it into one symbol per drawing, or press Looks right if "
                 "it is one symbol.", True),
    "wrapped": ("The description ran onto a second line. Check the name, then press Looks right.", True),
    "duplicate_tag": ("Another symbol has the same tag. Change one of the tags.", True),
    "generated_tag": ("The legend shows no tag for this symbol, so Pinny made one up. Change it if the "
                      "engineer uses another.", False),
}

_EDIT_OPS = ("rename", "set_tag", "set_group", "set_count", "confirm", "delete", "split", "merge", "add",
             "set_engineer")

_LEGEND_ERRORS = {
    "row_already_read": "That row was already read as a symbol. Edit that symbol instead, or press Add anyway.",
    "invalid_box": "Draw the box around the whole symbol drawing.",
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _hex(version: str) -> str:
    return version.split(":", 1)[1]


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _require_uuid(value: Any, name: str) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise ViewerError("invalid_request_id", f"{name} must be a UUID.") from None
    if not isinstance(value, str):
        raise ViewerError("invalid_request_id", f"{name} must be a UUID string.")
    return value


def _csv_cell(v: Any) -> Any:
    """Text from the drawing could start a spreadsheet formula; keep it text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _tally() -> Dict[str, int]:
    return {"found": 0, "approved": 0, "rejected": 0, "added": 0, "confirmed": 0, "unreviewed": 0}


def _add_pin(t: Dict[str, int], pin, found_here: bool = True) -> None:
    """Count one pin under its current tag. ``found`` counts Pinny's own
    finding, under the tag Pinny gave it (``found_here``), so correcting a
    tag moves the approval, not what Pinny found."""
    if pin.origin == _store.MACHINE:
        if found_here:
            t["found"] += 1
        if pin.state == _store.APPROVED:
            t["approved"] += 1
        elif pin.state == _store.REJECTED:
            t["rejected"] += 1
        elif pin.state == _store.UNREVIEWED:
            t["unreviewed"] += 1
    elif pin.state == _store.ADDED:
        t["added"] += 1
    t["confirmed"] = t["approved"] + t["added"]


def _sum(into: Dict[str, int], t: Dict[str, int]) -> None:
    for k, n in t.items():
        into[k] = into.get(k, 0) + n


class LegendService:
    def __init__(self, viewer) -> None:
        """``viewer`` is the ``ViewerService``: its render client (for page
        frames), learning store, job queue and version."""
        self.viewer = viewer
        self.data_dir = Path(viewer.data_dir)
        self._jobs: Optional[JobQueue] = viewer.jobs
        self._runner = None
        self._lock = threading.RLock()
        self._watchers: Dict[str, threading.Thread] = {}
        self._closing = threading.Event()

    # ------------------------------------------------------------- plumbing
    @property
    def jobs(self) -> JobQueue:
        """The viewer's queue, or in development (``PINNY_JOBS=inprocess``)
        a queue whose ``scan`` pool runs in a thread of this process."""
        with self._lock:
            if self._jobs is None:
                self._jobs = JobQueue(self.data_dir)
            if self.viewer.jobs is None and self._runner is None:
                from pinny.jobs.inprocess import InProcessRunner
                self._runner = InProcessRunner(self._jobs, "scan", self.data_dir)
            return self._jobs

    def close(self) -> None:
        self._closing.set()
        for t in list(self._watchers.values()):
            t.join(5)
        if self._runner is not None:
            self._runner.stop()

    def _doc_dir(self, version: str) -> Path:
        return self.data_dir / "documents" / _hex(version)

    def _legend_path(self, version: str) -> Path:
        return self._doc_dir(version) / "legend.json"

    def _run_dir(self, version: str, run_id: str) -> Path:
        return self._doc_dir(version) / "set_scans" / run_id

    def _info(self, version: str) -> dict:
        return self.viewer.document_info(version)  # 404s for an unknown version

    def _load(self, version: str) -> Optional[Legend]:
        path = self._legend_path(version)
        if not path.is_file():
            return None
        try:
            legend = Legend.load(path)
        except LegendError:
            _log.exception("unreadable legend for %s", version)
            raise ViewerError("legend_unreadable", "The saved legend could not be read. Find the legend again.",
                              500) from None
        if legend.pdf_sha256 != _hex(version):
            return None
        return legend

    def _legend(self, version: str) -> Legend:
        legend = self._load(version)
        if legend is None:
            raise ViewerError("legend_not_read", "Find the legend first.", 409)
        return legend

    def _save(self, version: str, legend: Legend) -> None:
        if not (self._doc_dir(version) / "version.json").is_file():
            raise ViewerError("document_version_not_found", "That document does not exist.", 404)
        legend.save(self._legend_path(version))

    @staticmethod
    def _stamp(legend: Legend, since: int, by: Optional[str]) -> None:
        for e in legend.edits[since:]:
            if by:
                e.setdefault("by", by)

    @staticmethod
    def _confirmation(legend: Legend) -> Optional[dict]:
        """The confirmation still in force: the last edit, if it confirmed."""
        if legend.edits and legend.edits[-1].get("op") == "confirm_legend":
            return legend.edits[-1]
        return None

    def tags(self, version: str) -> List[str]:
        legend = self._load(version)
        return [e.tag for e in legend.entries] if legend else []

    # ----------------------------------------------------------------- view
    @staticmethod
    def _lookalikes(legend: Legend) -> List[tuple]:
        out = []
        counted = [e for e in legend.entries if e.count and e.signatures and not e.labels]
        for i, a in enumerate(counted):
            for b in counted[i + 1:]:
                sim = max(signature_similarity(x, y) for x in a.signatures for y in b.signatures)
                if sim >= 0.95:
                    out.append((a, b))
        return out

    def _library_path(self, engineer: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", engineer.lower()).strip("-")[:60] or "engineer"
        return self.data_dir / "legend_library" / f"{slug}.json"

    def _library(self, engineer: Optional[str]) -> Optional[EngineerLibrary]:
        if not engineer:
            return None
        path = self._library_path(engineer)
        if not path.is_file():
            return None
        try:
            return EngineerLibrary.load(path)
        except LegendError:
            _log.exception("unreadable legend library %s", path.name)
            return None

    def _view(self, version: str, legend: Legend) -> Dict[str, Any]:
        looks: Dict[str, List[str]] = {}
        for a, b in self._lookalikes(legend):
            looks.setdefault(a.id, []).append(
                f"Drawn the same as {b.tag} ({b.name}) with no label, so Pinny can't tell them apart: matches "
                f"count as {a.tag}. Merge them, or untick Count on one.")
            looks.setdefault(b.id, []).append(
                f"Drawn the same as {a.tag} ({a.name}) with no label, so Pinny can't tell them apart: matches "
                f"count as {a.tag}. Merge them, or untick Count on one.")
        entries = []
        for e in legend.entries:
            d = e.to_dict()
            d.pop("signatures", None)
            notes = []
            for f in e.flags:
                text, blocking = _FLAG_NOTES.get(f, (None, False))
                if text:
                    notes.append({"flag": f, "text": text, "blocking": blocking and e.status == "check"})
            for text in looks.get(e.id, []):
                notes.append({"flag": "lookalike", "text": text, "blocking": False})
            d["notes"] = notes
            entries.append(d)
        problems = []
        review = legend.needs_review()
        if review:
            problems.append(f"{len(review)} symbol(s) need a look: " + ", ".join(e.tag for e in review) + ".")
        if not any(e.count and e.symbol_boxes for e in legend.entries):
            problems.append("No symbol is set to be counted. Tick Count for at least one.")
        conf = self._confirmation(legend)
        comparison = None
        lib = self._library(legend.engineer)
        if lib is not None:
            diffs = compare(legend, lib)
            comparison = {"engineer": lib.engineer,
                          "match": len(diffs["match"]),
                          "new": [d.to_dict() for d in diffs["new"]],
                          "changed": [d.to_dict() for d in diffs["changed"]],
                          "missing": [d.to_dict() for d in diffs["missing"]]}
        return {
            "document_version": version,
            "page_index": legend.page_index,
            "heading": legend.heading,
            "engineer": legend.engineer,
            "coordinate_frame": legend.coordinate_frame,
            "entries": entries,
            "warnings": list(legend.warnings),
            "problems": problems,
            "ready_to_confirm": not problems,
            "confirmed": conf is not None,
            "confirmed_by": conf.get("by") if conf else None,
            "confirmed_at": conf.get("at") if conf else None,
            "version": len(legend.edits),
            "comparison": comparison,
        }

    def state(self, version: str) -> Dict[str, Any]:
        """The legend (or ``None``) and the latest whole-set scan."""
        self._info(version)
        legend = self._load(version)
        runs = self.runs(version)
        return {"document_version": version, "legend": self._view(version, legend) if legend else None,
                "set_scan": runs[-1] if runs else None}

    # --------------------------------------------------------------- reading
    def read(self, version: str, page_index: Any = None, requested_by: Optional[str] = None) -> Dict[str, Any]:
        """Find the legend (or read it from ``page_index``) and store it,
        replacing any legend read before."""
        info = self._info(version)
        if page_index is not None:
            if isinstance(page_index, bool) or not isinstance(page_index, int) or not 0 <= page_index < info["page_count"]:
                raise ViewerError("invalid_page", "page_index must be a page of this document.")
        payload = {"document_version": version, "page_index": page_index}
        if self.viewer.jobs is not None:
            raw = self.viewer.jobs.run("read_legend", payload, requested_by=requested_by)
        else:  # development: in this process, like the other job kinds
            from pinny.jobs import legend_tasks
            from pinny.jobs.tasks import TaskError
            try:
                raw = legend_tasks.read_legend(payload, self.data_dir)
            except TaskError as exc:
                raise ViewerError(exc.code, str(exc), exc.http_status) from None
        legend = Legend.from_dict(raw)
        if legend.pdf_sha256 != _hex(version):
            raise ViewerError("legend_mismatch", "The legend was read from a different file.", 500)
        with self._lock:
            old = self._load(version)
            if old is not None and old.engineer:
                legend.engineer = old.engineer
            legend.edits.append({"op": "read", "entry": None, "at": _now(), "page_index": legend.page_index,
                                 **({"by": requested_by} if requested_by else {})})
            self._save(version, legend)
        return self.state(version)

    # --------------------------------------------------------------- editing
    def edit(self, version: str, body: Dict[str, Any], reviewer: Optional[str] = None) -> Dict[str, Any]:
        """One edit of the legend (``op`` and its fields); returns the state."""
        self._info(version)
        op = body.get("op")
        if op not in _EDIT_OPS:
            raise ViewerError("unknown_edit", f"Unknown legend edit {op!r}.")
        with self._lock:
            legend = self._legend(version)
            expected = body.get("expected_version")
            if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
                raise ViewerError("invalid_version", "expected_version must be an integer.")
            if expected is not None and expected != len(legend.edits):
                raise ViewerError("stale_legend", "Someone else changed the legend. It has been reloaded; "
                                  "make your change again.", 409)
            since = len(legend.edits)
            entry_id = body.get("entry_id")
            if op not in ("add", "set_engineer") and not isinstance(entry_id, str):
                raise ViewerError("missing_entry", "entry_id is required for this edit.")
            try:
                self._apply(legend, op, entry_id, body)
            except LegendError as exc:
                raise ViewerError(exc.code, _LEGEND_ERRORS.get(exc.code, str(exc)),
                                  409 if exc.code == "row_already_read" else 400) from None
            self._stamp(legend, since, reviewer)
            self._save(version, legend)
        return self.state(version)

    def _apply(self, legend: Legend, op: str, entry_id: Optional[str], b: Dict[str, Any]) -> None:
        def text(key: str, most: int = 200) -> str:
            v = b.get(key)
            if not isinstance(v, str) or len(v) > most:
                raise ViewerError("invalid_" + key, f"{key} must be text of at most {most} characters.")
            return v

        if op == "rename":
            legend.rename(entry_id, text("name"))
        elif op == "set_tag":
            legend.set_tag(entry_id, text("tag", 20))
        elif op == "set_group":
            legend.set_group(entry_id, text("group", 80))
        elif op == "set_count":
            if not isinstance(b.get("count"), bool):
                raise ViewerError("invalid_count", "count must be true or false.")
            legend.set_count(entry_id, b["count"])
        elif op == "confirm":
            legend.confirm(entry_id)
        elif op == "delete":
            legend.delete(entry_id)
        elif op == "split":
            legend.split(entry_id)
        elif op == "merge":
            into = b.get("into")
            if not isinstance(into, str):
                raise ViewerError("invalid_into", "Choose the symbol to merge into.")
            legend.merge(entry_id, into)
        elif op == "add":
            box = b.get("box")
            if not isinstance(box, dict) or not all(_num(box.get(k)) for k in ("x", "y", "width", "height")):
                raise ViewerError("invalid_box", "box needs numeric x, y, width and height in canonical pixels.")
            w, h = legend.coordinate_frame.get("width"), legend.coordinate_frame.get("height")
            x0, y0 = float(box["x"]), float(box["y"])
            x1, y1 = x0 + float(box["width"]), y0 + float(box["height"])
            if box["width"] <= 0 or box["height"] <= 0 or x0 < 0 or y0 < 0 or x1 > w or y1 > h:
                raise ViewerError("invalid_box", "The box must be inside the legend page.")
            force = b.get("force", False)
            if not isinstance(force, bool):
                raise ViewerError("invalid_force", "force must be true or false.")
            legend.add((x0, y0, x1, y1), force=force)
        elif op == "set_engineer":
            name = b.get("engineer")
            if name is not None:
                name = text("engineer", 80).strip()
                if not _ENGINEER_RE.match(name):
                    raise ViewerError("invalid_engineer", "Use letters, digits, spaces and . , & ' ( ) - / only.")
            legend.edits.append({"op": "set_engineer", "entry": None, "at": _now(), "old": legend.engineer,
                                 "new": name or None})
            legend.engineer = name or None

    def confirm(self, version: str, body: Dict[str, Any], reviewer: Optional[str] = None) -> Dict[str, Any]:
        """The reviewer's word that the legend is right: needed to scan."""
        self._info(version)
        with self._lock:
            legend = self._legend(version)
            expected = body.get("expected_version")
            if expected is not None and expected != len(legend.edits):
                raise ViewerError("stale_legend", "Someone else changed the legend. It has been reloaded; "
                                  "check it and confirm again.", 409)
            view = self._view(version, legend)
            if view["problems"]:
                raise ViewerError("legend_needs_review", " ".join(view["problems"]), 409)
            if not view["confirmed"]:
                legend.edits.append({"op": "confirm_legend", "entry": None, "at": _now(),
                                     **({"by": reviewer} if reviewer else {})})
                self._save(version, legend)
        return self.state(version)

    # --------------------------------------------------------------- library
    def library(self) -> Dict[str, Any]:
        out = []
        d = self.data_dir / "legend_library"
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                lib = EngineerLibrary.load(p)
            except LegendError:
                continue
            out.append({"engineer": lib.engineer, "symbols": len(lib.entries), "updated_at": lib.updated_at})
        return {"engineers": out}

    def save_standard(self, version: str, body: Dict[str, Any], reviewer: Optional[str] = None) -> Dict[str, Any]:
        """Save this (confirmed) legend as the engineer's standard legend."""
        info = self._info(version)
        name = body.get("engineer")
        if not isinstance(name, str) or not _ENGINEER_RE.match(name.strip()):
            raise ViewerError("invalid_engineer", "Enter the engineer's name (letters, digits, spaces and "
                              ". , & ' ( ) - / only).")
        name = name.strip()
        with self._lock:
            legend = self._legend(version)
            if self._confirmation(legend) is None:
                raise ViewerError("legend_not_confirmed", "Confirm the legend before saving it as a standard.", 409)
            path = self._library_path(name)
            lib = self._library(name) or EngineerLibrary(name)
            lib.update_from(legend, project=info.get("filename"))
            path.parent.mkdir(parents=True, exist_ok=True)
            lib.save(path)
            if legend.engineer != name:
                legend.edits.append({"op": "set_engineer", "entry": None, "at": _now(), "old": legend.engineer,
                                     "new": name, **({"by": reviewer} if reviewer else {})})
                legend.engineer = name
                # Naming the engineer is not a change to what is counted.
                legend.edits.append({"op": "confirm_legend", "entry": None, "at": _now(),
                                     **({"by": reviewer} if reviewer else {})})
                self._save(version, legend)
        return self.state(version)

    # ------------------------------------------------------------ set scans
    def _read_run(self, version: str, run_id: str) -> Optional[dict]:
        try:
            return json.loads((self._run_dir(version, run_id) / "run.json").read_text())
        except (OSError, ValueError):
            return None

    def _write_run(self, run: dict) -> None:
        rdir = self._run_dir(run["document_version"], run["run_id"])
        if not rdir.is_dir():  # the document was deleted meanwhile
            return
        write_json_atomic(rdir / "run.json", run)

    def _run_view(self, run: dict) -> Dict[str, Any]:
        out = {k: run.get(k) for k in ("run_id", "document_version", "status", "requested_by", "created_at",
                                       "finished_at", "legend_sha256", "legend_page_index", "error",
                                       "warnings", "elapsed_seconds")}
        total = len(run.get("pages") or [])
        done = total if run.get("status") in (RUN_RECORDING, RUN_DONE) else 0
        fraction = 1.0 if run.get("status") == RUN_DONE else 0.0
        if run.get("status") in (RUN_QUEUED, RUN_RUNNING) and run.get("job_id"):
            try:
                job = self.jobs.get(run["job_id"])
                fraction = float(job.progress or 0.0)
                done = min(total, int(fraction / 0.9 * total + 1e-9))
                if job.status == "running":
                    out["status"] = RUN_RUNNING
            except KeyError:
                pass
        out.update(sheets_total=total, sheets_done=done, progress=round(fraction, 4),
                   sheets=[dict(s) for s in run.get("sheets", [])])
        return out

    def runs(self, version: str) -> List[Dict[str, Any]]:
        d = self._doc_dir(version) / "set_scans"
        runs = []
        for p in d.iterdir() if d.is_dir() else []:
            run = self._read_run(version, p.name)
            if run is not None:
                runs.append(run)
        runs.sort(key=lambda r: (r.get("created_at") or "", r["run_id"]))
        return [self._run_view(r) for r in runs]

    def start_scan(self, version: str, request_id: Any, requested_by: Optional[str] = None) -> Dict[str, Any]:
        """Scan every sheet with the confirmed legend, in the background."""
        _require_uuid(request_id, "request_id")
        info = self._info(version)
        run_id = str(uuid.uuid5(_RUN_NS, request_id))
        with self._lock:
            run = self._read_run(version, run_id)
            if run is None:
                legend = self._legend(version)
                if self._confirmation(legend) is None:
                    raise ViewerError("legend_not_confirmed", "Confirm the legend before scanning the set.", 409)
                for r in self.runs(version):
                    if r["status"] in RUN_ACTIVE:
                        raise ViewerError("set_scan_running", "A whole-set scan of this drawing is already "
                                          "running. Wait for it to finish.", 409)
                pages = [i for i in range(int(info["page_count"])) if i != legend.page_index]
                if not pages:
                    raise ViewerError("nothing_to_scan", "This drawing has no sheets besides the legend page.", 409)
                raw = json.dumps(legend.to_dict(), ensure_ascii=False).encode()
                rdir = self._run_dir(version, run_id)
                rdir.mkdir(parents=True, exist_ok=True)
                (rdir / "legend.json").write_bytes(raw)
                run = {"run_id": run_id, "request_id": request_id, "document_version": version,
                       "document_id": info["document_id"], "status": RUN_QUEUED, "job_id": None,
                       "legend_sha256": hashlib.sha256(raw).hexdigest(), "legend_page_index": legend.page_index,
                       "pages": pages, "detector_version": self.viewer.detector_version,
                       "requested_by": requested_by, "created_at": _now(), "finished_at": None,
                       "error": None, "warnings": [], "sheets": []}
                self._write_run(run)
                self._submit(run)
        self._watch(version, run_id)
        return self._run_view(self._read_run(version, run_id) or run)

    def _submit(self, run: dict) -> None:
        run["job_id"] = self.jobs.submit(
            "scan_set", {"document_version": run["document_version"], "run_id": run["run_id"],
                         "legend_sha256": run["legend_sha256"]},
            priority=PRIORITY_BATCH, requested_by=run.get("requested_by"))
        self._write_run(run)

    def _watch(self, version: str, run_id: str) -> None:
        with self._lock:
            t = self._watchers.get(run_id)
            if t is not None and t.is_alive():
                return
            if self.viewer.jobs is None:
                _ = self.jobs  # creates the in-process runner
                self._runner.wake()
            t = threading.Thread(target=self._follow, args=(version, run_id), daemon=True,
                                 name=f"pinny-setscan-{run_id[:8]}")
            self._watchers[run_id] = t
            t.start()

    def wait(self, version: str, run_id: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Block until the run is recorded or failed (tests and scripts)."""
        t = self._watchers.get(run_id)
        if t is not None:
            t.join(timeout)
        run = self._read_run(version, run_id)
        if run is None:
            raise ViewerError("unknown_set_scan", "That whole-set scan does not exist.", 404)
        return self._run_view(run)

    def resume_interrupted(self) -> List[str]:
        """On startup: follow every whole-set scan a restart cut off, and
        record the ones whose job finished meanwhile."""
        out = []
        root = self.data_dir / "documents"
        for d in root.iterdir() if root.is_dir() else []:
            sdir = d / "set_scans"
            if len(d.name) != 64 or not sdir.is_dir():
                continue
            version = "sha256:" + d.name
            for r in sdir.iterdir():
                run = self._read_run(version, r.name)
                if run is not None and run.get("status") in RUN_ACTIVE:
                    try:
                        self._watch(version, r.name)
                        out.append(r.name)
                    except Exception:  # noqa: BLE001 - one bad run must not stop the site starting
                        _log.exception("could not resume set scan %s", r.name)
        return out

    def cancel_document(self, version: str) -> None:
        """Before a document is deleted: stop its running whole-set scans."""
        for r in self.runs(version):
            run = self._read_run(version, r["run_id"])
            if run and run.get("status") in RUN_ACTIVE and run.get("job_id"):
                try:
                    self.jobs.cancel(run["job_id"])
                except KeyError:
                    pass

    def _follow(self, version: str, run_id: str) -> None:
        try:
            while not self._closing.is_set():
                run = self._read_run(version, run_id)
                if run is None or run.get("status") not in RUN_ACTIVE:
                    return
                if not run.get("job_id"):
                    with self._lock:
                        self._submit(run)
                    continue
                try:
                    job = self.jobs.get(run["job_id"])
                except KeyError:
                    job = None
                if job is None:
                    if (self._run_dir(version, run_id) / "result.json").is_file():
                        self._record(run)
                    else:
                        self._fail(run, "interrupted", "The scan stopped unexpectedly. Start it again.")
                    return
                if job.status == DONE:
                    self._record(run)
                    return
                if job.status in (FAILED, CANCELLED):
                    status = job.error_status or 500
                    msg = job.error_message if 400 <= status < 500 and job.error_message else \
                        "The whole-set scan failed because of an internal error. Start it again."
                    if job.error_code == "worker_lost" or job.error_code == "interrupted":
                        msg = job.error_message
                    self._fail(run, job.error_code or "set_scan_failed", msg)
                    return
                if job.status != run["status"] and job.status in (RUN_QUEUED, RUN_RUNNING):
                    run["status"] = job.status
                    self._write_run(run)
                self._closing.wait(POLL_S)
        except Exception:  # noqa: BLE001 - the run stays active and resumes at the next start
            _log.exception("following set scan %s failed", run_id)

    def _fail(self, run: dict, code: str, message: str) -> None:
        run.update(status=RUN_FAILED, error={"code": code, "message": message}, finished_at=_now())
        self._write_run(run)

    def _record(self, run: dict) -> None:
        """Record the job's result: one ordinary scan per sheet."""
        version, run_id = run["document_version"], run["run_id"]
        try:
            self._info(version)
        except ViewerError:
            return  # deleted meanwhile
        run["status"] = RUN_RECORDING
        self._write_run(run)
        try:
            result = json.loads((self._run_dir(version, run_id) / "result.json").read_text())
        except (OSError, ValueError):
            self._fail(run, "result_missing", "The scan's result could not be read. Start it again.")
            return
        entries = {str(e["entry_id"]): e for e in result.get("entries", [])}
        scales = {entries[k]["tag"]: v for k, v in (result.get("scales") or {}).items() if k in entries}
        sheets = []
        for sheet in result.get("sheets", []):
            p = int(sheet["page_index"])
            row = {"page_index": p, "method": sheet.get("method"), "scan_id": None, "error": None,
                   "counts": sheet.get("counts") or {}}
            try:
                row["scan_id"] = self._record_sheet(run, result, sheet, scales)
            except Exception as exc:  # noqa: BLE001 - one sheet must not stop the others
                code = getattr(exc, "code", None)
                if isinstance(code, str) and getattr(exc, "status", 400) < 500:
                    row["error"] = {"code": code, "message": str(exc)}
                else:
                    _log.exception("recording set scan %s page %s failed", run_id, p)
                    row["error"] = {"code": "record_failed",
                                    "message": "This sheet's result could not be saved."}
            sheets.append(row)
        self._keep_signatures(version, result)
        run.update(status=RUN_DONE, finished_at=_now(), sheets=sheets, warnings=list(result.get("warnings", [])),
                   elapsed_seconds=result.get("elapsed_seconds"))
        self._write_run(run)

    def _record_sheet(self, run: dict, result: dict, sheet: dict, scales: dict) -> str:
        version, p = run["document_version"], int(sheet["page_index"])
        frame = dict(self.viewer.render.page_frame(version, p))
        got = sheet.get("coordinate_frame") or {}
        if (got.get("width"), got.get("height")) != (frame["width"], frame["height"]):
            raise ViewerError("frame_mismatch", "This sheet was scanned at a different size than the viewer "
                              "shows it, so its pins were not saved.", 409)
        scan_id = str(uuid.uuid5(_RUN_NS, f"{run['run_id']}#p{p}"))
        detections = []
        for n, d in enumerate(sheet.get("detections", []), start=1):
            rot = int(round(float(d.get("rotation", 0)) / 90.0)) * 90 % 360
            det = {"id": f"det-{n}", "box": d["box"], "x": d["x"], "y": d["y"], "score": d["score"],
                   "rotation": rot, "mirrored": bool(d.get("mirrored", False)), "source": d.get("source", "vector"),
                   "class_label": d["tag"], "legend_entry": d.get("entry_id")}
            for k in ("label", "scale"):
                if k in d:
                    det[k] = d[k]
            detections.append(det)
        settings = {"legend_sha256": run["legend_sha256"], "legend_page_index": run["legend_page_index"],
                    "set_scan_id": run["run_id"], "method": sheet.get("method"), "scales": scales,
                    "warnings": list(result.get("warnings", [])) + list(sheet.get("warnings", [])),
                    "entries": [{"entry_id": e.get("entry_id"), "tag": e.get("tag"), "name": e.get("name")}
                                for e in result.get("entries", [])],
                    **(result.get("settings") or {})}
        scan_result = {
            "scan_id": scan_id, "mode": SCAN_MODE,
            "document": {"document_id": run["document_id"], "document_version": version, "page_index": p},
            "coordinate_frame": {k: frame[k] for k in ("space", "dpi", "width", "height", "origin", "y_axis")
                                 if k in frame},
            "template": None,
            "detector": {"name": DETECTOR_NAME, "version": run["detector_version"], "settings": settings},
            "set_scan": {"run_id": run["run_id"], "page_index": p},
            "truncated": False, "warnings": list(sheet.get("warnings", [])),
            "detections": detections, "created_at": result.get("created_at") or run["created_at"],
        }
        scan, dets = _store.Scan.from_scan_result(scan_result)
        metadata = {"scan_result": scan_result, "request_id": run["request_id"]}
        if run.get("requested_by"):
            metadata["requested_by"] = run["requested_by"]
        import dataclasses
        scan = dataclasses.replace(scan, metadata=metadata)
        self.viewer._db(self.viewer.store.record_scan, scan, dets)  # idempotent on scan id and content
        return scan_id

    def _keep_signatures(self, version: str, result: dict) -> None:
        """Keep the drawing signatures the scan computed for symbols added by
        hand (not a change a person made, so no edit is logged)."""
        sigs = result.get("legend_signatures") or {}
        with self._lock:
            legend = self._load(version)
            if legend is None:
                return
            changed = False
            for e in legend.entries:
                s = sigs.get(e.id)
                if s and len(e.signatures) < len(e.symbol_boxes) and len(s) == len(e.symbol_boxes):
                    e.signatures = list(s)
                    changed = True
            if changed:
                try:
                    self._save(version, legend)
                except ViewerError:
                    pass

    # ---------------------------------------------------------------- counts
    def counts(self, version: str) -> Dict[str, Any]:
        """Per tag, per sheet and for the whole set, from the latest
        whole-set scan: found by Pinny, approved, rejected, added by hand,
        confirmed (approved + added) and still unreviewed."""
        info = self._info(version)
        legend = self._load(version)
        runs = [r for r in self.runs(version) if r["status"] == RUN_DONE]
        names = {e.tag: e.name for e in legend.entries} if legend else {}
        order = [e.tag for e in legend.entries if e.count] if legend else []
        out: Dict[str, Any] = {"document_version": version, "filename": info["filename"], "run_id": None,
                               "tags": [], "sheets": [], "totals": {"by_tag": {}, "all": _tally()}}
        if not runs:
            return out
        run = runs[-1]
        out["run_id"] = run["run_id"]
        seen: List[str] = list(order)
        totals: Dict[str, Dict[str, int]] = {}
        for s in run["sheets"]:
            row = {"page_index": s["page_index"], "scan_id": s.get("scan_id"), "error": s.get("error"),
                   "review": None, "by_tag": {}, "all": _tally()}
            if s.get("scan_id"):
                try:
                    st = self.viewer._db(self.viewer.store.load_scan, s["scan_id"])
                except _store.NotFound:
                    st = None
                found_as = {d.detection_id: d.raw.get("class_label") for d in st.detections} if st else {}
                for pin in st.pins if st else []:
                    tag = pin.class_label or ""
                    orig = (found_as.get(pin.detection_id) or tag) if pin.origin == _store.MACHINE else tag
                    for t in (tag, orig):
                        if t not in seen:
                            seen.append(t)
                    _add_pin(row["by_tag"].setdefault(tag, _tally()), pin, found_here=orig == tag)
                    if orig != tag:
                        row["by_tag"].setdefault(orig, _tally())["found"] += 1
                for t in row["by_tag"].values():
                    _sum(row["all"], t)
                pr = self.viewer._db(self.viewer.store.page_review_status,
                                     _contract.canonical_page_id(version, s["page_index"]))
                complete = pr is not None and pr.status == _store.PAGE_COMPLETE
                row["review"] = ("reviewed" if complete else
                                 "to_review" if row["all"]["unreviewed"] else "ready_to_mark")
                for tag, t in row["by_tag"].items():
                    _sum(totals.setdefault(tag, _tally()), t)
            out["sheets"].append(row)
        for t in totals.values():
            _sum(out["totals"]["all"], t)
        out["totals"]["by_tag"] = totals
        out["tags"] = [{"tag": t, "name": names.get(t, "No tag" if t == "" else ""), "counted": t in order}
                       for t in seen]
        return out

    def counts_csv(self, version: str) -> tuple:
        c = self.counts(version)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Sheet", "Sheet status", "Tag", "Symbol", "Found by Pinny", "Approved", "Rejected",
                    "Added by hand", "Confirmed total", "Unreviewed"])
        status = {"reviewed": "Reviewed", "to_review": "To review", "ready_to_mark": "Ready to mark reviewed"}
        keys = ("found", "approved", "rejected", "added", "confirmed", "unreviewed")
        for s in c["sheets"]:
            st = status.get(s["review"], "Not saved" if s.get("error") else "")
            for t in c["tags"]:
                n = s["by_tag"].get(t["tag"])
                if n is None:
                    continue
                w.writerow([s["page_index"] + 1, st, _csv_cell(t["tag"]), _csv_cell(t["name"])]
                           + [n[k] for k in keys])
        for t in c["tags"]:
            n = c["totals"]["by_tag"].get(t["tag"], _tally())
            w.writerow(["All sheets", "", _csv_cell(t["tag"]), _csv_cell(t["name"])] + [n[k] for k in keys])
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (c["filename"] or "drawing").rsplit(".", 1)[0])[:80] or "drawing"
        return buf.getvalue().encode("utf-8"), f"{stem}-counts.csv"
