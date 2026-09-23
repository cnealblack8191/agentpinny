"""SQLite persistence for scans, pin state, review events and training examples.

Implements docs/contracts.md sections 4-6. The store captures examples only;
it never trains or adjusts the detector.

Invariants:
  * Every review action updates pin state and appends an immutable event in a
    single ``BEGIN IMMEDIATE`` transaction.
  * Every action carries a client-generated ``request_id`` (uuid4). Replaying
    the same request returns the original result without writing anything;
    reusing a ``request_id`` for a different request raises
    ``IdempotencyConflict``.
  * Crop specs are committed with the event, before any image is written.
    Image writing happens after commit; a failure leaves the crop ``pending``
    or ``failed`` and ``process_pending_crops()`` regenerates it later, so a
    failed file write never loses a training example.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from . import contract
from .contract import Box, CropSpec

try:  # docs/contracts.md section 7; pinny/errors.py is owned by the foundation.
    from pinny.errors import PinnyError as _ErrorBase
except ImportError:  # pragma: no cover - until the foundation lands
    class _ErrorBase(Exception):  # type: ignore[no-redef]
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code
            self.message = message

DB_FILENAME = "pinny.sqlite3"
CROPS_DIRNAME = "crops"
EXPORTS_DIRNAME = "exports"

# Deterministic ids derived from request ids so retries converge.
_ID_NAMESPACE = uuid.UUID("5d0f6c1e-7a51-4b8e-9f0e-6b1f3c0a9e21")

# Pin origins / states / actions (contracts section 5).
MACHINE = "machine"
MANUAL = "manual"

UNREVIEWED = "unreviewed"
APPROVED = "approved"
REJECTED = "rejected"
ADDED = "added"
REMOVED = "removed"

APPROVE = "approve"
REJECT = "reject"
ADD_MANUAL = "add_manual"
REMOVE_MANUAL = "remove_manual"

SOURCES = frozenset({"viewer", "cli", "test"})

# Crop file states.
CROP_PENDING = "pending"
CROP_WRITTEN = "written"
CROP_FAILED = "failed"


class LearningStoreError(_ErrorBase):
    code = "learning_store_error"

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(code or type(self).code, message)


class InvalidArgument(LearningStoreError):
    code = "invalid_argument"


class IdempotencyConflict(LearningStoreError):
    """A request/event/scan id was reused for a different payload."""

    code = "idempotency_conflict"


class NotFound(LearningStoreError):
    code = "not_found"


class InvalidTransition(LearningStoreError):
    code = "invalid_transition"


class StaleVersion(LearningStoreError):
    """``expected_version`` did not match the pin's current version."""

    code = "stale_version"


class SchemaMismatch(LearningStoreError):
    code = "schema_mismatch"


class CropRenderer(Protocol):
    """Cuts ``spec.box`` from the canonical raster and returns PNG bytes.

    Supplied by the foundation's render service (contracts sections 6, 9).
    The store never reads drawings itself and never sends them anywhere.
    """

    def __call__(self, spec: CropSpec) -> bytes: ...


def local_reviewer_identity() -> Optional[str]:
    """Contracts section 5: ``$PINNY_REVIEWER``, then the OS user, then ``None``."""
    env = os.environ.get("PINNY_REVIEWER")
    if env:
        return env
    try:
        return getpass.getuser() or None
    except Exception:
        return None


def default_data_dir() -> Path:
    """``$PINNY_DATA_DIR`` or ``~/.local/share/pinny`` (outside the repo)."""
    env = os.environ.get("PINNY_DATA_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "pinny"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(obj: Any) -> str:
    data = obj if isinstance(obj, (bytes, bytearray)) else _canon(obj).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _derive_id(kind: str, request_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"{kind}:{request_id}"))


def _pt(x: float, y: float) -> Dict[str, float]:
    return {"x": x, "y": y}


# --------------------------------------------------------------------------
# Public records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    """One entry of a section-4 scan result's ``detections``."""

    detection_id: str
    box: Any  # Box, {x, y, width, height} mapping, or BoundingBox-like
    score: float
    rotation: int = 0
    source: str = "detector"
    # Pin point; defaults to the detection centre (section 3).
    x: Optional[float] = None
    y: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)  # any extra keys, kept verbatim


@dataclass(frozen=True)
class Scan:
    """Section-4 scan metadata (everything except ``detections``)."""

    scan_id: str
    document_id: str
    document_version: str
    page_index: int
    frame_width: int
    frame_height: int
    detector_name: str
    detector_version: str
    detector_settings: Dict[str, Any]
    template_box: Optional[Any] = None
    template_sha256: Optional[str] = None
    created_at: Optional[str] = None  # RFC3339 UTC; defaults to record time
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_page_id(self) -> str:
        return contract.canonical_page_id(self.document_version, self.page_index)

    @property
    def coordinate_frame(self) -> Dict[str, Any]:
        return contract.frame_descriptor(self.frame_width, self.frame_height)

    @classmethod
    def from_scan_result(cls, d: Mapping[str, Any]) -> Tuple["Scan", List[Detection]]:
        """Parse a section-4 scan result dict."""
        try:
            w, h = contract.validate_frame(d["coordinate_frame"])
            doc, det, tpl = d["document"], d["detector"], d.get("template") or {}
            scan = cls(scan_id=d["scan_id"], document_id=doc["document_id"],
                       document_version=doc["document_version"], page_index=int(doc["page_index"]),
                       frame_width=w, frame_height=h, detector_name=det["name"],
                       detector_version=det["version"], detector_settings=dict(det.get("settings") or {}),
                       template_box=tpl.get("box"), template_sha256=tpl.get("sha256"),
                       created_at=d.get("created_at"))
            known = {"id", "box", "x", "y", "score", "rotation", "source"}
            dets = [Detection(detection_id=e["id"], box=e["box"], score=e["score"],
                              rotation=int(e.get("rotation", 0)), source=e.get("source", "detector"),
                              x=e.get("x"), y=e.get("y"),
                              raw={k: v for k, v in e.items() if k not in known})
                    for e in d.get("detections", [])]
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidArgument(f"malformed scan result: {e!r}") from e
        return scan, dets


@dataclass(frozen=True)
class Pin:
    pin_id: str
    scan_id: str
    origin: str
    state: str
    x: float
    y: float
    box: Optional[Box]
    detection_id: Optional[str]
    version: int
    created_at: str
    updated_at: str

    def snapshot(self) -> Dict[str, Any]:
        return {
            "pin_id": self.pin_id, "origin": self.origin, "state": self.state,
            "point": _pt(self.x, self.y), "box": self.box.to_dict() if self.box else None,
            "detection_id": self.detection_id, "version": self.version,
        }


@dataclass(frozen=True)
class ReviewEvent:
    event_id: str
    request_id: str
    scan_id: str
    pin_id: str
    action: str
    prior_state: Optional[Dict[str, Any]]
    new_state: Dict[str, Any]
    source: str
    reviewer: Optional[str]
    created_at: str
    crop_key: Optional[str]


@dataclass(frozen=True)
class ReviewResult:
    event: ReviewEvent
    pin: Pin
    replayed: bool  # True when the request_id had already been applied


@dataclass(frozen=True)
class ScanState:
    scan: Scan
    recorded_at: str
    detections: List[Detection]
    pins: List[Pin]
    events: List[ReviewEvent]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id               TEXT PRIMARY KEY,
    document_id           TEXT NOT NULL,
    document_version      TEXT NOT NULL,
    page_index            INTEGER NOT NULL,
    canonical_page_id     TEXT NOT NULL,
    frame_width           INTEGER NOT NULL,
    frame_height          INTEGER NOT NULL,
    dpi                   INTEGER NOT NULL,
    template_box          TEXT,
    template_sha256       TEXT,
    detector_name         TEXT NOT NULL,
    detector_version      TEXT NOT NULL,
    detector_settings     TEXT NOT NULL,
    detector_settings_sha TEXT NOT NULL,
    metadata              TEXT NOT NULL,
    fingerprint           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    recorded_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scans_by_page ON scans(document_version, page_index);

-- Original detector output; never mutated after insert.
CREATE TABLE IF NOT EXISTS detections (
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    detection_id TEXT NOT NULL,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    x  REAL NOT NULL, y  REAL NOT NULL,
    score        REAL NOT NULL,
    rotation     INTEGER NOT NULL,
    source       TEXT NOT NULL,
    raw          TEXT NOT NULL,
    PRIMARY KEY (scan_id, detection_id)
);

-- Current pin state (mutable, versioned).
CREATE TABLE IF NOT EXISTS pins (
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    pin_id       TEXT NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('machine','manual')),
    state        TEXT NOT NULL CHECK (state IN ('unreviewed','approved','rejected','added','removed')),
    detection_id TEXT,
    x REAL NOT NULL, y REAL NOT NULL,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    version      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (scan_id, pin_id),
    FOREIGN KEY (scan_id, detection_id) REFERENCES detections(scan_id, detection_id)
);

-- Deterministic crop definitions, keyed by hash of the spec.
CREATE TABLE IF NOT EXISTS crops (
    crop_key    TEXT PRIMARY KEY,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending','written','failed')),
    rel_path    TEXT,
    sha256      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Append-only review log.
CREATE TABLE IF NOT EXISTS review_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    request_id   TEXT NOT NULL UNIQUE,
    request_sha  TEXT NOT NULL,
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    pin_id       TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('approve','reject','add_manual','remove_manual')),
    prior_state  TEXT,
    new_state    TEXT NOT NULL,
    source       TEXT NOT NULL,
    reviewer     TEXT,
    crop_key     TEXT REFERENCES crops(crop_key),
    created_at   TEXT NOT NULL,
    FOREIGN KEY (scan_id, pin_id) REFERENCES pins(scan_id, pin_id)
);
CREATE INDEX IF NOT EXISTS events_by_pin ON review_events(scan_id, pin_id, seq);

CREATE TRIGGER IF NOT EXISTS review_events_no_update
BEFORE UPDATE ON review_events BEGIN
    SELECT RAISE(ABORT, 'review_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS review_events_no_delete
BEFORE DELETE ON review_events BEGIN
    SELECT RAISE(ABORT, 'review_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS detections_no_update
BEFORE UPDATE ON detections BEGIN
    SELECT RAISE(ABORT, 'detections are immutable');
END;
"""

# Label implied by an event at capture time. Removal of a manual pin carries
# no detector label; interpretation lives in export and can change later.
_EVENT_LABEL = {APPROVE: "positive", REJECT: "negative", ADD_MANUAL: "positive", REMOVE_MANUAL: None}

_AUTO = object()


class LearningStore:
    """One instance per thread/process; SQLite locking coordinates writers."""

    def __init__(self, data_dir: Optional[os.PathLike] = None, *,
                 crop_renderer: Optional[CropRenderer] = None,
                 default_reviewer: Any = _AUTO,
                 clock: Callable[[], str] = _utcnow) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.crops_dir = self.data_dir / CROPS_DIRNAME
        self.exports_dir = self.data_dir / EXPORTS_DIRNAME
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.crop_renderer = crop_renderer
        self.default_reviewer = (local_reviewer_identity() if default_reviewer is _AUTO
                                 else default_reviewer)
        self._clock = clock
        self._db = sqlite3.connect(self.data_dir / DB_FILENAME, isolation_level=None, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        try:
            self._check_schema_version()
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO store_meta(key, value) VALUES ('schema_version', ?)",
                             (str(contract.STORE_SCHEMA_VERSION),))
        except BaseException:
            self._db.close()
            raise

    # ------------------------------------------------------------------ infra

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "LearningStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _check_schema_version(self) -> None:
        has_meta = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_meta'").fetchone()
        if not has_meta:
            return
        row = self._db.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
        if row is not None and int(row["value"]) != contract.STORE_SCHEMA_VERSION:
            raise SchemaMismatch(
                f"{self.data_dir / DB_FILENAME} has store schema {row['value']}, this code supports "
                f"{contract.STORE_SCHEMA_VERSION}; move it aside (pre-contract prototype stores "
                f"are not migrated)")

    class _Tx:
        def __init__(self, db: sqlite3.Connection):
            self.db = db

        def __enter__(self):
            self.db.execute("BEGIN IMMEDIATE")
            return self.db

        def __exit__(self, exc_type, exc, tb):
            self.db.execute("COMMIT" if exc_type is None else "ROLLBACK")
            return False

    def _tx(self) -> "LearningStore._Tx":
        return LearningStore._Tx(self._db)

    # ------------------------------------------------------------------ scans

    def record_scan(self, scan: Scan, detections: Sequence[Detection]) -> None:
        """Persist scan metadata, original detections and unreviewed pins.

        Idempotent on ``scan_id``: re-recording identical content is a no-op;
        different content under the same id raises ``IdempotencyConflict``.
        """
        try:
            w, h = contract.validate_frame(scan.coordinate_frame)
            tpl_box = Box.coerce(scan.template_box).to_dict() if scan.template_box is not None else None
            norm = []
            for d in detections:
                box = Box.coerce(d.box)
                cx, cy = box.center
                x, y = contract.normalize_point(d.x if d.x is not None else cx,
                                                d.y if d.y is not None else cy)
                if d.rotation not in (0, 90, 180, 270):
                    raise ValueError(f"detection {d.detection_id}: rotation {d.rotation} not in 0/90/180/270")
                if not all(math.isfinite(v) for v in (x, y, float(d.score))):
                    raise ValueError(f"detection {d.detection_id}: non-finite value")
                norm.append((d, box, x, y))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise InvalidArgument(f"invalid scan {scan.scan_id}: {e}") from e
        ids = [d.detection_id for d, *_ in norm]
        if len(set(ids)) != len(ids):
            raise InvalidArgument(f"duplicate detection id in scan {scan.scan_id}")

        fingerprint = _sha({
            "document": [scan.document_id, scan.document_version, scan.page_index],
            "frame": [w, h, contract.CANONICAL_DPI],
            "template": [tpl_box, scan.template_sha256],
            "detector": [scan.detector_name, scan.detector_version, scan.detector_settings],
            "created_at": scan.created_at,
            "metadata": scan.metadata,
            "detections": [[d.detection_id, b.to_dict(), x, y, d.score, d.rotation, d.source, d.raw]
                           for d, b, x, y in norm],
        })
        now = self._clock()
        with self._tx() as db:
            existing = db.execute("SELECT fingerprint FROM scans WHERE scan_id=?",
                                  (scan.scan_id,)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise IdempotencyConflict(f"scan {scan.scan_id} already recorded with different content")
                return
            db.execute(
                "INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scan.scan_id, scan.document_id, scan.document_version, scan.page_index,
                 scan.canonical_page_id, w, h, contract.CANONICAL_DPI,
                 _canon(tpl_box) if tpl_box else None, scan.template_sha256,
                 scan.detector_name, scan.detector_version, _canon(scan.detector_settings),
                 _sha(scan.detector_settings), _canon(scan.metadata), fingerprint,
                 scan.created_at or now, now))
            for d, b, x, y in norm:
                db.execute("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (scan.scan_id, d.detection_id, *b.edges(), x, y, float(d.score),
                            d.rotation, d.source, _canon(d.raw)))
                db.execute(
                    "INSERT INTO pins VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scan.scan_id, d.detection_id, MACHINE, UNREVIEWED, d.detection_id,
                     x, y, *b.edges(), 1, now, now))

    def record_scan_result(self, result: Mapping[str, Any]) -> str:
        """Record a section-4 scan result dict; returns its ``scan_id``."""
        scan, dets = Scan.from_scan_result(result)
        self.record_scan(scan, dets)
        return scan.scan_id

    def list_scans(self, document_version: Optional[str] = None, page_index: Optional[int] = None,
                   *, document_id: Optional[str] = None) -> List[Scan]:
        q, args = "SELECT * FROM scans WHERE 1=1", []
        for col, val in (("document_version", document_version), ("page_index", page_index),
                         ("document_id", document_id)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        return [self._scan(r) for r in self._db.execute(q + " ORDER BY recorded_at, rowid", args)]

    def load_scan(self, scan_id: str) -> ScanState:
        row = self._db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if row is None:
            raise NotFound(f"scan {scan_id} not found")
        dets = [Detection(detection_id=r["detection_id"],
                          box=Box.from_edges((r["x0"], r["y0"], r["x1"], r["y1"])),
                          score=r["score"], rotation=r["rotation"], source=r["source"],
                          x=r["x"], y=r["y"], raw=json.loads(r["raw"]))
                for r in self._db.execute(
                    "SELECT * FROM detections WHERE scan_id=? ORDER BY rowid", (scan_id,))]
        pins = [self._pin(r) for r in self._db.execute(
            "SELECT * FROM pins WHERE scan_id=? ORDER BY created_at, rowid", (scan_id,))]
        events = [self._event(r) for r in self._db.execute(
            "SELECT * FROM review_events WHERE scan_id=? ORDER BY seq", (scan_id,))]
        return ScanState(scan=self._scan(row), recorded_at=row["recorded_at"],
                         detections=dets, pins=pins, events=events)

    def scan_result(self, scan_id: str) -> Dict[str, Any]:
        """The original detector output as a section-4 scan result dict."""
        st = self.load_scan(scan_id)
        s = st.scan
        dets = []
        for d in st.detections:
            e = {"id": d.detection_id, "box": d.box.to_dict(), "x": d.x, "y": d.y,
                 "score": d.score, "rotation": d.rotation, "source": d.source}
            e.update(d.raw)
            dets.append(e)
        return {
            "scan_id": s.scan_id,
            "document": {"document_id": s.document_id, "document_version": s.document_version,
                         "page_index": s.page_index},
            "coordinate_frame": s.coordinate_frame,
            "template": {"box": s.template_box, "sha256": s.template_sha256},
            "detector": {"name": s.detector_name, "version": s.detector_version,
                         "settings": s.detector_settings},
            "created_at": s.created_at,
            "detections": dets,
        }

    def get_pin(self, scan_id: str, pin_id: str) -> Pin:
        r = self._db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                             (scan_id, pin_id)).fetchone()
        if r is None:
            raise NotFound(f"pin {pin_id} not found in scan {scan_id}")
        return self._pin(r)

    # ---------------------------------------------------------------- reviews

    def approve(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
                reviewer: Any = _AUTO, expected_version: Optional[int] = None,
                event_id: Optional[str] = None) -> ReviewResult:
        """Approve a machine detection (positive example)."""
        return self._review(APPROVE, scan_id, pin_id, request_id=request_id, source=source,
                            reviewer=reviewer, expected_version=expected_version, event_id=event_id)

    def reject(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
               reviewer: Any = _AUTO, expected_version: Optional[int] = None,
               event_id: Optional[str] = None) -> ReviewResult:
        """Reject a machine detection (negative example)."""
        return self._review(REJECT, scan_id, pin_id, request_id=request_id, source=source,
                            reviewer=reviewer, expected_version=expected_version, event_id=event_id)

    def remove_manual(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
                      reviewer: Any = _AUTO, expected_version: Optional[int] = None,
                      event_id: Optional[str] = None) -> ReviewResult:
        """Remove a manually added pin. Not treated as a negative example."""
        return self._review(REMOVE_MANUAL, scan_id, pin_id, request_id=request_id, source=source,
                            reviewer=reviewer, expected_version=expected_version, event_id=event_id)

    def delete_pin(self, scan_id: str, pin_id: str, *, request_id: str, **kw) -> ReviewResult:
        """UI "delete": rejects machine pins, removes manual pins."""
        prev = self._db.execute("SELECT action FROM review_events WHERE request_id=?",
                                (request_id,)).fetchone()
        if prev is not None:  # replay: dispatch exactly as the first time
            action = prev["action"]
        else:
            action = REJECT if self.get_pin(scan_id, pin_id).origin == MACHINE else REMOVE_MANUAL
        return self._review(action, scan_id, pin_id, request_id=request_id, **kw)

    def add_manual(self, scan_id: str, x: float, y: float, *, request_id: str, source: str,
                   reviewer: Any = _AUTO, pin_id: Optional[str] = None,
                   event_id: Optional[str] = None) -> ReviewResult:
        """Add a missed receptacle at canonical px ``(x, y)`` (positive example).

        ``pin_id`` defaults to an id derived from ``request_id`` so retries
        create exactly one pin."""
        try:
            point = contract.normalize_point(x, y)
        except (TypeError, ValueError) as e:
            raise InvalidArgument(f"invalid point ({x!r}, {y!r})") from e
        return self._review(ADD_MANUAL, scan_id, pin_id or _derive_id("pin", request_id),
                            request_id=request_id, source=source, reviewer=reviewer,
                            point=point, event_id=event_id)

    def _review(self, action: str, scan_id: str, pin_id: str, *, request_id: str, source: str,
                reviewer: Any = _AUTO, expected_version: Optional[int] = None,
                event_id: Optional[str] = None, point: Optional[tuple] = None) -> ReviewResult:
        if not request_id or not isinstance(request_id, str):
            raise InvalidArgument("request_id is required")
        if source not in SOURCES:
            raise InvalidArgument(f"source must be one of {sorted(SOURCES)}, got {source!r}")
        reviewer = self.default_reviewer if reviewer is _AUTO else reviewer
        event_id = event_id or _derive_id("event", request_id)
        request_sha = _sha({"action": action, "scan_id": scan_id, "pin_id": pin_id,
                            "point": list(point) if point else None, "source": source,
                            "reviewer": reviewer, "event_id": event_id})

        with self._tx() as db:
            prev = db.execute("SELECT * FROM review_events WHERE request_id=?",
                              (request_id,)).fetchone()
            if prev is not None:
                if prev["request_sha"] != request_sha:
                    raise IdempotencyConflict(f"request_id {request_id} reused for a different request")
                replay = self._event(prev)
                pin = self._pin(db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                                           (scan_id, pin_id)).fetchone())
                result = ReviewResult(event=replay, pin=pin, replayed=True)
                crop_key = replay.crop_key
            else:
                if db.execute("SELECT 1 FROM review_events WHERE event_id=?", (event_id,)).fetchone():
                    raise IdempotencyConflict(f"event_id {event_id} already used")
                scan = db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
                if scan is None:
                    raise NotFound(f"scan {scan_id} not found")
                row = db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                                 (scan_id, pin_id)).fetchone()
                prior = self._pin(row) if row is not None else None
                self._check_transition(action, prior, pin_id, expected_version)
                now = self._clock()

                if action == ADD_MANUAL:
                    x, y = point  # type: ignore[misc]
                    if not (0 <= x < scan["frame_width"] and 0 <= y < scan["frame_height"]):
                        raise InvalidArgument(
                            f"point ({x}, {y}) is outside the {scan['frame_width']}x"
                            f"{scan['frame_height']} canonical raster")
                    db.execute(
                        "INSERT INTO pins VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (scan_id, pin_id, MANUAL, ADDED, None, x, y, None, None, None, None, 1, now, now))
                else:
                    new_state = {APPROVE: APPROVED, REJECT: REJECTED, REMOVE_MANUAL: REMOVED}[action]
                    db.execute("UPDATE pins SET state=?, version=version+1, updated_at=? "
                               "WHERE scan_id=? AND pin_id=?", (new_state, now, scan_id, pin_id))
                pin = self._pin(db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                                           (scan_id, pin_id)).fetchone())

                crop_key = None
                if _EVENT_LABEL[action] is not None:
                    crop_key = self._ensure_crop(db, self._crop_spec(scan, pin), now)

                db.execute(
                    "INSERT INTO review_events(event_id, request_id, request_sha, scan_id, pin_id, action,"
                    " prior_state, new_state, source, reviewer, crop_key, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, request_id, request_sha, scan_id, pin_id, action,
                     _canon(prior.snapshot()) if prior else None, _canon(pin.snapshot()),
                     source, reviewer, crop_key, now))
                ev = self._event(db.execute("SELECT * FROM review_events WHERE event_id=?",
                                            (event_id,)).fetchone())
                result = ReviewResult(event=ev, pin=pin, replayed=False)

        # Best effort after commit; failures stay recoverable in the crops table.
        if crop_key is not None:
            self._materialize(crop_key)
        return result

    @staticmethod
    def _check_transition(action: str, prior: Optional[Pin], pin_id: str,
                          expected_version: Optional[int]) -> None:
        if action == ADD_MANUAL:
            if prior is not None:
                raise IdempotencyConflict(f"pin {pin_id} already exists")
            return
        if prior is None:
            raise NotFound(f"pin {pin_id} not found")
        if expected_version is not None and prior.version != expected_version:
            raise StaleVersion(f"pin {pin_id} is at version {prior.version}, expected {expected_version}; "
                               f"reload and retry")
        if action in (APPROVE, REJECT):
            if prior.origin != MACHINE:
                raise InvalidTransition(f"{action} applies to machine detections; use remove_manual")
        elif action == REMOVE_MANUAL:
            if prior.origin != MANUAL:
                raise InvalidTransition("remove_manual applies to manual pins; use reject")
            if prior.state == REMOVED:
                raise InvalidTransition(f"manual pin {pin_id} is already removed")

    # ------------------------------------------------------------------ crops

    @staticmethod
    def _crop_spec(scan: Mapping[str, Any], pin: Pin) -> CropSpec:
        return contract.pin_crop(scan["document_version"], scan["page_index"],
                                 scan["frame_width"], scan["frame_height"], pin.x, pin.y, pin.box)

    def _ensure_crop(self, db: sqlite3.Connection, spec: CropSpec, now: str) -> str:
        spec_d = spec.to_dict()
        key = _sha(spec_d)
        db.execute("INSERT OR IGNORE INTO crops(crop_key, spec, status, created_at, updated_at)"
                   " VALUES (?,?,?,?,?)", (key, _canon(spec_d), CROP_PENDING, now, now))
        return key

    def crop_path(self, crop_key: str) -> Path:
        return self.crops_dir / crop_key[:2] / f"{crop_key}.png"

    def _materialize(self, crop_key: str) -> str:
        row = self._db.execute("SELECT * FROM crops WHERE crop_key=?", (crop_key,)).fetchone()
        if row is None:
            raise NotFound(f"crop {crop_key} not found")
        if row["status"] == CROP_WRITTEN and (self.data_dir / row["rel_path"]).exists():
            return CROP_WRITTEN
        if self.crop_renderer is None:
            return row["status"]
        spec = CropSpec.from_dict(json.loads(row["spec"]))
        path = self.crop_path(crop_key)
        try:
            data = self.crop_renderer(spec)
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise LearningStoreError("crop renderer returned no bytes", "crop_render_failed")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".png")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception as e:  # recorded, retried by process_pending_crops()
            self._db.execute("UPDATE crops SET status=?, attempts=attempts+1, last_error=?, updated_at=?"
                             " WHERE crop_key=?", (CROP_FAILED, f"{type(e).__name__}: {e}"[:2000],
                                                   self._clock(), crop_key))
            return CROP_FAILED
        self._db.execute("UPDATE crops SET status=?, rel_path=?, sha256=?, attempts=attempts+1,"
                         " last_error=NULL, updated_at=? WHERE crop_key=?",
                         (CROP_WRITTEN, path.relative_to(self.data_dir).as_posix(), _sha(bytes(data)),
                          self._clock(), crop_key))
        return CROP_WRITTEN

    def process_pending_crops(self, *, verify_written: bool = True) -> Dict[str, int]:
        """(Re)generate crops that are pending, failed, or missing on disk."""
        if verify_written:
            for r in self._db.execute("SELECT crop_key, rel_path FROM crops WHERE status=?",
                                      (CROP_WRITTEN,)).fetchall():
                if not (self.data_dir / r["rel_path"]).exists():
                    self._db.execute("UPDATE crops SET status=?, last_error=?, updated_at=? WHERE crop_key=?",
                                     (CROP_PENDING, "file missing", self._clock(), r["crop_key"]))
        counts = {CROP_WRITTEN: 0, CROP_FAILED: 0, CROP_PENDING: 0}
        keys = [r["crop_key"] for r in self._db.execute(
            "SELECT crop_key FROM crops WHERE status IN (?,?) ORDER BY created_at",
            (CROP_PENDING, CROP_FAILED))]
        for k in keys:
            counts[self._materialize(k)] += 1
        return counts

    def crop_status_counts(self) -> Dict[str, int]:
        return {r["status"]: r["n"] for r in self._db.execute(
            "SELECT status, COUNT(*) AS n FROM crops GROUP BY status")}

    # ----------------------------------------------------------------- export

    def export(self, *, document_version: Optional[str] = None,
               include_unlabeled: bool = True, out_path: Optional[os.PathLike] = None) -> Dict[str, Any]:
        """Versioned metadata export with crop references.

        Labels are derived from raw events at export time (see
        ``interpret_pin``); raw events are included so they can be
        reinterpreted later without the database.
        """
        scans = self.list_scans(document_version=document_version)
        crops: Dict[str, Dict[str, Any]] = {}
        examples: List[Dict[str, Any]] = []
        raw_events: List[Dict[str, Any]] = []

        for s in scans:
            state = self.load_scan(s.scan_id)
            scan_row = self._db.execute("SELECT * FROM scans WHERE scan_id=?", (s.scan_id,)).fetchone()
            dets = {d.detection_id: d for d in state.detections}
            by_pin: Dict[str, List[ReviewEvent]] = {}
            for ev in state.events:
                by_pin.setdefault(ev.pin_id, []).append(ev)
                raw_events.append(self._event_dict(ev))
            for pin in state.pins:
                evs = by_pin.get(pin.pin_id, [])
                label, label_event, status = interpret_pin(pin, evs)
                if label is None and not include_unlabeled:
                    continue
                crop_key = label_event.crop_key if label_event else None
                if crop_key is None:
                    spec = self._crop_spec(scan_row, pin)
                    crop_key = _sha(spec.to_dict())
                    crops.setdefault(crop_key, {"status": "not_captured", "path": None,
                                                "sha256": None, "spec": spec.to_dict()})
                else:
                    crops.setdefault(crop_key, self._crop_ref(crop_key))
                det = dets.get(pin.detection_id) if pin.detection_id else None
                examples.append({
                    "example_id": f"{s.scan_id}/{pin.pin_id}",
                    "scan_id": s.scan_id,
                    "canonical_page_id": s.canonical_page_id,
                    "pin_id": pin.pin_id,
                    "origin": pin.origin,
                    "pin_state": pin.state,
                    "label": label,
                    "label_status": status,
                    "label_event_id": label_event.event_id if label_event else None,
                    "event_ids": [e.event_id for e in evs],
                    "point": _pt(pin.x, pin.y),
                    "box": pin.box.to_dict() if pin.box else None,
                    "detection": ({"id": det.detection_id, "score": det.score, "rotation": det.rotation,
                                   "source": det.source, "raw": det.raw} if det else None),
                    "reviewer": label_event.reviewer if label_event else None,
                    "source": label_event.source if label_event else None,
                    "labeled_at": label_event.created_at if label_event else None,
                    "crop_key": crop_key,
                })

        doc = {
            "schema": contract.EXPORT_SCHEMA,
            "schema_version": contract.EXPORT_SCHEMA_VERSION,
            "store_schema_version": contract.STORE_SCHEMA_VERSION,
            "exported_at": self._clock(),
            "filter": {"document_version": document_version, "include_unlabeled": include_unlabeled},
            "crop_settings": {
                "spec_version": contract.CROP_SPEC_VERSION,
                "units": contract.FRAME_SPACE,
                "dpi": contract.CANONICAL_DPI,
                "manual_crop_size_px": contract.MANUAL_CROP_SIZE_PX,
                "detection_crop_margin_px": contract.DETECTION_CROP_MARGIN_PX,
                "manual_rule": "x0 = floor(x - size/2 + 0.5), x1 = x0 + size (same for y), clipped",
                "detection_rule": "floor(x) - margin .. ceil(x + width) + margin (same for y), clipped",
            },
            "label_policy": {
                "approve": "positive", "reject": "negative", "add_manual": "positive",
                "remove_manual": "none (manual pin withdrawn; not a detector negative)",
                "unreviewed": "unlabeled", "multiple_reviews": "latest approve/reject wins",
            },
            "scans": [self._scan_dict(s) for s in scans],
            "examples": examples,
            "crops": crops,
            "events": raw_events,
        }
        if out_path is not None:
            out = Path(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, out)
        return doc

    def _crop_ref(self, crop_key: str) -> Dict[str, Any]:
        r = self._db.execute("SELECT * FROM crops WHERE crop_key=?", (crop_key,)).fetchone()
        return {"status": r["status"], "path": r["rel_path"], "sha256": r["sha256"],
                "spec": json.loads(r["spec"]), "last_error": r["last_error"]}

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _scan(r: sqlite3.Row) -> Scan:
        return Scan(scan_id=r["scan_id"], document_id=r["document_id"],
                    document_version=r["document_version"], page_index=r["page_index"],
                    frame_width=r["frame_width"], frame_height=r["frame_height"],
                    detector_name=r["detector_name"], detector_version=r["detector_version"],
                    detector_settings=json.loads(r["detector_settings"]),
                    template_box=json.loads(r["template_box"]) if r["template_box"] else None,
                    template_sha256=r["template_sha256"], created_at=r["created_at"],
                    metadata=json.loads(r["metadata"]))

    def _scan_dict(self, s: Scan) -> Dict[str, Any]:
        r = self._db.execute("SELECT detector_settings_sha, recorded_at FROM scans WHERE scan_id=?",
                             (s.scan_id,)).fetchone()
        return {"scan_id": s.scan_id,
                "document": {"document_id": s.document_id, "document_version": s.document_version,
                             "page_index": s.page_index},
                "canonical_page_id": s.canonical_page_id,
                "coordinate_frame": s.coordinate_frame,
                "template": {"box": s.template_box, "sha256": s.template_sha256},
                "detector": {"name": s.detector_name, "version": s.detector_version,
                             "settings": s.detector_settings,
                             "settings_sha256": r["detector_settings_sha"]},
                "metadata": s.metadata, "created_at": s.created_at, "recorded_at": r["recorded_at"]}

    @staticmethod
    def _pin(r: sqlite3.Row) -> Pin:
        box = Box.from_edges((r["x0"], r["y0"], r["x1"], r["y1"])) if r["x0"] is not None else None
        return Pin(pin_id=r["pin_id"], scan_id=r["scan_id"], origin=r["origin"], state=r["state"],
                   x=r["x"], y=r["y"], box=box, detection_id=r["detection_id"], version=r["version"],
                   created_at=r["created_at"], updated_at=r["updated_at"])

    @staticmethod
    def _event(r: sqlite3.Row) -> ReviewEvent:
        return ReviewEvent(event_id=r["event_id"], request_id=r["request_id"], scan_id=r["scan_id"],
                           pin_id=r["pin_id"], action=r["action"],
                           prior_state=json.loads(r["prior_state"]) if r["prior_state"] else None,
                           new_state=json.loads(r["new_state"]), source=r["source"],
                           reviewer=r["reviewer"], created_at=r["created_at"], crop_key=r["crop_key"])

    @staticmethod
    def _event_dict(e: ReviewEvent) -> Dict[str, Any]:
        return {"event_id": e.event_id, "request_id": e.request_id, "scan_id": e.scan_id,
                "pin_id": e.pin_id, "action": e.action, "prior_state": e.prior_state,
                "new_state": e.new_state, "source": e.source, "reviewer": e.reviewer,
                "created_at": e.created_at, "crop_key": e.crop_key}


def interpret_pin(pin: Pin, events: Iterable[ReviewEvent]):
    """Default label policy. Returns ``(label, label_event, label_status)``.

    * machine, latest approve/reject -> positive/negative
    * machine, no review             -> unlabeled
    * manual, still added            -> positive
    * manual, later removed          -> no label ("manual_removed"); the add
      event is preserved, and removal is *not* a detector negative.
    """
    evs = list(events)
    if pin.origin == MACHINE:
        labeling = [e for e in evs if e.action in (APPROVE, REJECT)]
        if not labeling:
            return None, None, "unlabeled"
        last = labeling[-1]
        status = "reviewed" if len(labeling) == 1 else "re-reviewed"
        return _EVENT_LABEL[last.action], last, status
    add = next((e for e in evs if e.action == ADD_MANUAL), None)
    if pin.state == REMOVED:
        return None, add, "manual_removed"
    return "positive", add, "manual_added"
