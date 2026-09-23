"""SQLite persistence for scans, pin state, review events and training examples.

The store captures examples only; it never trains or adjusts the detector.

Invariants:
  * Every review action updates pin state and appends an immutable event in a
    single ``BEGIN IMMEDIATE`` transaction.
  * Every action carries a caller-supplied ``request_id``. Replaying the same
    request returns the original result without writing anything; reusing a
    ``request_id`` for a different request raises ``IdempotencyConflict``.
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
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence

from . import contract
from .contract import Box, CropSpec

DB_FILENAME = "pinny.sqlite3"
CROPS_DIRNAME = "crops"
EXPORTS_DIRNAME = "exports"

# Deterministic ids derived from request ids so retries converge.
_ID_NAMESPACE = uuid.UUID("5d0f6c1e-7a51-4b8e-9f0e-6b1f3c0a9e21")

# Pin origins / states / actions.
MACHINE = "machine"
MANUAL = "manual"

UNREVIEWED = "unreviewed"
APPROVED = "approved"
REJECTED = "rejected"
ADDED = "added"
REMOVED = "removed"

APPROVE = "approve"
REJECT = "reject"
ADD = "add"
REMOVE_MANUAL = "remove_manual"

# Crop file states.
CROP_PENDING = "pending"
CROP_WRITTEN = "written"
CROP_FAILED = "failed"


class LearningStoreError(Exception):
    pass


class IdempotencyConflict(LearningStoreError):
    """A request/event/scan id was reused for a different payload."""


class NotFound(LearningStoreError):
    pass


class InvalidTransition(LearningStoreError):
    pass


class StaleVersion(LearningStoreError):
    """``expected_version`` did not match the pin's current version."""


class CropRenderer(Protocol):
    """Renders a crop to PNG bytes from the exact document version/page.

    Supplied by integration (the PDF/page rendering owner). The store never
    fetches drawings itself and never sends them anywhere.
    """

    def __call__(self, spec: CropSpec) -> bytes: ...


def local_reviewer_identity() -> Optional[str]:
    """Best-effort local reviewer id: ``$PINNY_REVIEWER`` then the OS user."""
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


# --------------------------------------------------------------------------
# Public records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    detection_id: str
    box: Box
    score: Optional[float] = None
    label: Optional[str] = None
    # Pin point; defaults to the box centre.
    x: Optional[float] = None
    y: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scan:
    scan_id: str
    document_version_id: str
    canonical_page_id: str
    page_width: float
    page_height: float
    detector_version: str
    matching_settings: Dict[str, Any]
    template_id: Optional[str] = None
    page_index: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


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
            "x": self.x, "y": self.y, "box": list(self.box) if self.box else None,
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
    created_at: str
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
    document_version_id   TEXT NOT NULL,
    canonical_page_id     TEXT NOT NULL,
    page_index            INTEGER,
    page_width            REAL NOT NULL,
    page_height           REAL NOT NULL,
    template_id           TEXT,
    detector_version      TEXT NOT NULL,
    matching_settings     TEXT NOT NULL,
    matching_settings_sha TEXT NOT NULL,
    metadata              TEXT NOT NULL,
    fingerprint           TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scans_by_page ON scans(document_version_id, canonical_page_id);

-- Original detector output; never mutated after insert.
CREATE TABLE IF NOT EXISTS detections (
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    detection_id TEXT NOT NULL,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    x  REAL NOT NULL, y  REAL NOT NULL,
    score        REAL,
    label        TEXT,
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
    action       TEXT NOT NULL CHECK (action IN ('approve','reject','add','remove_manual')),
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
_EVENT_LABEL = {APPROVE: "positive", REJECT: "negative", ADD: "positive", REMOVE_MANUAL: None}


class LearningStore:
    """One instance per thread/process; SQLite locking coordinates writers."""

    def __init__(self, data_dir: Optional[os.PathLike] = None, *,
                 crop_renderer: Optional[CropRenderer] = None,
                 default_reviewer: Optional[str] = None,
                 clock: Callable[[], str] = _utcnow) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.crops_dir = self.data_dir / CROPS_DIRNAME
        self.exports_dir = self.data_dir / EXPORTS_DIRNAME
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.crop_renderer = crop_renderer
        self.default_reviewer = default_reviewer
        self._clock = clock
        self._db = sqlite3.connect(self.data_dir / DB_FILENAME, isolation_level=None,
                                   timeout=30.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.executescript(_SCHEMA)
        self._init_meta()

    # ------------------------------------------------------------------ infra

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "LearningStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _init_meta(self) -> None:
        row = self._db.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._db.execute("INSERT INTO store_meta(key, value) VALUES ('schema_version', ?)",
                             (str(contract.STORE_SCHEMA_VERSION),))
        elif int(row["value"]) != contract.STORE_SCHEMA_VERSION:
            raise LearningStoreError(
                f"store schema {row['value']} != supported {contract.STORE_SCHEMA_VERSION}")

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
        norm_dets = []
        for d in detections:
            box = contract.normalize_box(d.box)
            x = d.x if d.x is not None else (box[0] + box[2]) / 2.0
            y = d.y if d.y is not None else (box[1] + box[3]) / 2.0
            x, y = contract.normalize_point(x, y)
            norm_dets.append((d, box, x, y))
        ids = [d.detection_id for d, *_ in norm_dets]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate detection_id in scan")

        fingerprint = _sha({
            "scan": {
                "document_version_id": scan.document_version_id,
                "canonical_page_id": scan.canonical_page_id,
                "page_index": scan.page_index,
                "page_size": contract.normalize_point(scan.page_width, scan.page_height),
                "template_id": scan.template_id,
                "detector_version": scan.detector_version,
                "matching_settings": scan.matching_settings,
                "metadata": scan.metadata,
            },
            "detections": [[d.detection_id, list(b), x, y, d.score, d.label, d.raw]
                           for d, b, x, y in norm_dets],
        })
        now = self._clock()
        with self._tx() as db:
            existing = db.execute("SELECT fingerprint FROM scans WHERE scan_id=?",
                                  (scan.scan_id,)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise IdempotencyConflict(f"scan {scan.scan_id} already recorded with different content")
                return
            pw, ph = contract.normalize_point(scan.page_width, scan.page_height)
            db.execute(
                "INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scan.scan_id, scan.document_version_id, scan.canonical_page_id, scan.page_index,
                 pw, ph, scan.template_id, scan.detector_version,
                 _canon(scan.matching_settings), _sha(scan.matching_settings),
                 _canon(scan.metadata), fingerprint, now))
            for d, b, x, y in norm_dets:
                db.execute("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (scan.scan_id, d.detection_id, *b, x, y, d.score, d.label, _canon(d.raw)))
                db.execute(
                    "INSERT INTO pins VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scan.scan_id, d.detection_id, MACHINE, UNREVIEWED, d.detection_id,
                     x, y, *b, 1, now, now))

    def list_scans(self, document_version_id: Optional[str] = None,
                   canonical_page_id: Optional[str] = None) -> List[Scan]:
        q, args = "SELECT * FROM scans WHERE 1=1", []
        if document_version_id is not None:
            q += " AND document_version_id=?"
            args.append(document_version_id)
        if canonical_page_id is not None:
            q += " AND canonical_page_id=?"
            args.append(canonical_page_id)
        return [self._scan(r) for r in self._db.execute(q + " ORDER BY created_at, scan_id", args)]

    def load_scan(self, scan_id: str) -> ScanState:
        row = self._db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if row is None:
            raise NotFound(f"scan {scan_id}")
        dets = [Detection(detection_id=r["detection_id"], box=(r["x0"], r["y0"], r["x1"], r["y1"]),
                          score=r["score"], label=r["label"], x=r["x"], y=r["y"], raw=json.loads(r["raw"]))
                for r in self._db.execute(
                    "SELECT * FROM detections WHERE scan_id=? ORDER BY rowid", (scan_id,))]
        pins = [self._pin(r) for r in self._db.execute(
            "SELECT * FROM pins WHERE scan_id=? ORDER BY created_at, rowid", (scan_id,))]
        events = [self._event(r) for r in self._db.execute(
            "SELECT * FROM review_events WHERE scan_id=? ORDER BY seq", (scan_id,))]
        return ScanState(scan=self._scan(row), created_at=row["created_at"],
                         detections=dets, pins=pins, events=events)

    def get_pin(self, scan_id: str, pin_id: str) -> Pin:
        r = self._db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                             (scan_id, pin_id)).fetchone()
        if r is None:
            raise NotFound(f"pin {scan_id}/{pin_id}")
        return self._pin(r)

    # ---------------------------------------------------------------- reviews

    def approve(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
                reviewer: Optional[str] = None, expected_version: Optional[int] = None,
                event_id: Optional[str] = None) -> ReviewResult:
        """Approve a machine detection (positive example)."""
        return self._review(APPROVE, scan_id, pin_id, request_id=request_id, source=source,
                            reviewer=reviewer, expected_version=expected_version, event_id=event_id)

    def reject(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
               reviewer: Optional[str] = None, expected_version: Optional[int] = None,
               event_id: Optional[str] = None) -> ReviewResult:
        """Reject a machine detection (negative example)."""
        return self._review(REJECT, scan_id, pin_id, request_id=request_id, source=source,
                            reviewer=reviewer, expected_version=expected_version, event_id=event_id)

    def remove_manual(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
                      reviewer: Optional[str] = None, expected_version: Optional[int] = None,
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
                   reviewer: Optional[str] = None, pin_id: Optional[str] = None,
                   event_id: Optional[str] = None) -> ReviewResult:
        """Add a missed receptacle (positive example). ``pin_id`` defaults to
        an id derived from ``request_id`` so retries create exactly one pin."""
        return self._review(ADD, scan_id, pin_id or _derive_id("pin", request_id),
                            request_id=request_id, source=source, reviewer=reviewer,
                            point=contract.normalize_point(x, y), event_id=event_id)

    def _review(self, action: str, scan_id: str, pin_id: str, *, request_id: str, source: str,
                reviewer: Optional[str] = None, expected_version: Optional[int] = None,
                event_id: Optional[str] = None, point: Optional[tuple] = None) -> ReviewResult:
        if not request_id:
            raise ValueError("request_id is required")
        if not source:
            raise ValueError("source is required")
        reviewer = reviewer if reviewer is not None else self.default_reviewer
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
                    raise NotFound(f"scan {scan_id}")
                row = db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                                 (scan_id, pin_id)).fetchone()
                prior = self._pin(row) if row is not None else None
                self._check_transition(action, prior, expected_version)
                now = self._clock()

                if action == ADD:
                    x, y = point  # type: ignore[misc]
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
                    spec = contract.pin_crop(scan["document_version_id"], scan["canonical_page_id"],
                                             scan["page_width"], scan["page_height"],
                                             pin.x, pin.y, pin.box)
                    crop_key = self._ensure_crop(db, spec, now)

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
    def _check_transition(action: str, prior: Optional[Pin], expected_version: Optional[int]) -> None:
        if action == ADD:
            if prior is not None:
                raise IdempotencyConflict(f"pin {prior.pin_id} already exists")
            return
        if prior is None:
            raise NotFound("pin not found")
        if expected_version is not None and prior.version != expected_version:
            raise StaleVersion(f"pin {prior.pin_id} is at version {prior.version}, expected {expected_version}")
        if action in (APPROVE, REJECT):
            if prior.origin != MACHINE:
                raise InvalidTransition(f"{action} applies to machine detections; use remove_manual")
        elif action == REMOVE_MANUAL:
            if prior.origin != MANUAL:
                raise InvalidTransition("remove_manual applies to manual pins; use reject")
            if prior.state == REMOVED:
                raise InvalidTransition("manual pin already removed")

    # ------------------------------------------------------------------ crops

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
            raise NotFound(f"crop {crop_key}")
        if row["status"] == CROP_WRITTEN and (self.data_dir / row["rel_path"]).exists():
            return CROP_WRITTEN
        if self.crop_renderer is None:
            return row["status"]
        spec = CropSpec.from_dict(json.loads(row["spec"]))
        path = self.crop_path(crop_key)
        try:
            data = self.crop_renderer(spec)
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise LearningStoreError("crop renderer returned no bytes")
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

    def export(self, *, document_version_id: Optional[str] = None,
               include_unlabeled: bool = True, out_path: Optional[os.PathLike] = None) -> Dict[str, Any]:
        """Versioned metadata export with crop references.

        Labels are derived from raw events at export time (see
        ``interpret_pin``); raw events are included so they can be
        reinterpreted later without the database.
        """
        scans = self.list_scans(document_version_id=document_version_id)
        crops: Dict[str, Dict[str, Any]] = {}
        examples: List[Dict[str, Any]] = []
        raw_events: List[Dict[str, Any]] = []

        for s in scans:
            state = self.load_scan(s.scan_id)
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
                    crop_key = self._spec_key(s, pin)
                crops.setdefault(crop_key, self._crop_ref(crop_key, s, pin))
                det = dets.get(pin.detection_id) if pin.detection_id else None
                examples.append({
                    "example_id": f"{s.scan_id}/{pin.pin_id}",
                    "scan_id": s.scan_id,
                    "pin_id": pin.pin_id,
                    "origin": pin.origin,
                    "pin_state": pin.state,
                    "label": label,
                    "label_status": status,
                    "label_event_id": label_event.event_id if label_event else None,
                    "event_ids": [e.event_id for e in evs],
                    "point": [pin.x, pin.y],
                    "box": list(pin.box) if pin.box else None,
                    "detection": ({"score": det.score, "label": det.label, "raw": det.raw}
                                  if det else None),
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
            "filter": {"document_version_id": document_version_id,
                       "include_unlabeled": include_unlabeled},
            "crop_settings": {
                "spec_version": contract.CROP_SPEC_VERSION,
                "manual_crop_size": contract.MANUAL_CROP_SIZE,
                "detection_crop_margin": contract.DETECTION_CROP_MARGIN,
                "dpi": contract.CROP_RENDER_DPI,
                "units": "canonical page units, origin top-left",
            },
            "label_policy": {
                "approve": "positive", "reject": "negative", "add": "positive",
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

    def _spec_key(self, s: Scan, pin: Pin) -> str:
        spec = contract.pin_crop(s.document_version_id, s.canonical_page_id,
                                 s.page_width, s.page_height, pin.x, pin.y, pin.box)
        return _sha(spec.to_dict())

    def _crop_ref(self, crop_key: str, s: Scan, pin: Pin) -> Dict[str, Any]:
        r = self._db.execute("SELECT * FROM crops WHERE crop_key=?", (crop_key,)).fetchone()
        if r is None:  # unlabeled pins: spec only, never rendered
            spec = contract.pin_crop(s.document_version_id, s.canonical_page_id,
                                     s.page_width, s.page_height, pin.x, pin.y, pin.box)
            return {"status": "not_captured", "path": None, "sha256": None, "spec": spec.to_dict()}
        return {"status": r["status"], "path": r["rel_path"], "sha256": r["sha256"],
                "spec": json.loads(r["spec"]), "last_error": r["last_error"]}

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _scan(r: sqlite3.Row) -> Scan:
        return Scan(scan_id=r["scan_id"], document_version_id=r["document_version_id"],
                    canonical_page_id=r["canonical_page_id"], page_width=r["page_width"],
                    page_height=r["page_height"], detector_version=r["detector_version"],
                    matching_settings=json.loads(r["matching_settings"]), template_id=r["template_id"],
                    page_index=r["page_index"], metadata=json.loads(r["metadata"]))

    def _scan_dict(self, s: Scan) -> Dict[str, Any]:
        r = self._db.execute("SELECT matching_settings_sha, created_at FROM scans WHERE scan_id=?",
                             (s.scan_id,)).fetchone()
        return {"scan_id": s.scan_id, "document_version_id": s.document_version_id,
                "canonical_page_id": s.canonical_page_id, "page_index": s.page_index,
                "page_size": [s.page_width, s.page_height], "template_id": s.template_id,
                "detector_version": s.detector_version, "matching_settings": s.matching_settings,
                "matching_settings_sha256": r["matching_settings_sha"], "metadata": s.metadata,
                "created_at": r["created_at"]}

    @staticmethod
    def _pin(r: sqlite3.Row) -> Pin:
        box = (r["x0"], r["y0"], r["x1"], r["y1"]) if r["x0"] is not None else None
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
    add = next((e for e in evs if e.action == ADD), None)
    if pin.state == REMOVED:
        return None, add, "manual_removed"
    return "positive", add, "manual_added"
