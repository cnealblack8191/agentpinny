"""SQLite persistence for scans, pin state, review events and training examples.

Implements docs/contracts.md sections 4-6. The store captures examples only;
it never trains or adjusts the detector.

Invariants:
  * Every review action updates pin state and appends an immutable event in a
    single ``BEGIN IMMEDIATE`` transaction.
  * Every action carries a client-generated ``request_id`` (uuid4). Replaying
    the same request returns the result *as recorded* (rebuilt from the
    event) without writing anything; reusing a ``request_id`` for a different
    request raises ``IdempotencyConflict``. ``reviewer`` is recorded but is
    not part of the request identity.
  * Reads that span several tables (``export``, ``load_scan``, dataset
    export) run in one read transaction, so they see one consistent snapshot.
  * Crop specs are committed with the event, before any image is written.
    Image writing happens after commit; a failure leaves the crop ``pending``
    or ``failed`` and ``process_pending_crops()`` regenerates it later, so a
    failed file write never loses a training example.
  * One ``LearningStore`` per thread. Using it from another thread raises
    ``WrongThread``; SQLite (WAL + busy timeout) coordinates the connections.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Protocol,
                    Sequence, Tuple, Union)

from . import contract, loop, schema
from .contract import Box, CropSpec
from .loop import CropRecord

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

# Page review states.
PAGE_IN_PROGRESS = "in_progress"
PAGE_COMPLETE = "complete"

# Document splits. Held-out splits never reach training data by default.
SPLITS = ("train", "val", "test", "eval")
HELD_OUT_SPLITS = ("test", "eval")

# Detector settings keys read as the scan's score threshold.
_THRESHOLD_KEYS = ("threshold", "score_threshold", "min_score")

# Batch page states (contracts section 5a).
BATCH_PENDING = "pending"
BATCH_RUNNING = "running"
BATCH_DONE = "done"
BATCH_FAILED = "failed"
BATCH_SKIPPED = "skipped"
BATCH_PAGE_STATES = (BATCH_PENDING, BATCH_RUNNING, BATCH_DONE, BATCH_FAILED, BATCH_SKIPPED)
PIN_STATES = ("unreviewed", "approved", "rejected", "added", "removed")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


class WrongThread(LearningStoreError):
    """The store was used from a thread other than the one that opened it."""

    code = "wrong_thread"


class CropRenderer(Protocol):
    """Cuts ``spec.box`` from the canonical raster and returns PNG bytes.

    Supplied by the foundation's render service (contracts sections 6, 9).
    It may expose ``renderer_version`` (str), which becomes part of every
    crop spec and crop key. The store never reads drawings itself and never
    sends them anywhere.
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


def detector_settings_sha(settings: Mapping[str, Any]) -> str:
    """The ``settings_sha256`` recorded for a scan's detector settings."""
    return _sha(dict(settings))


def _derive_id(kind: str, request_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"{kind}:{request_id}"))


def _pt(x: float, y: float) -> Dict[str, float]:
    return {"x": x, "y": y}


def _atomic_write(path: Path, data: bytes) -> None:
    """tmp + fsync + rename + directory fsync (best effort on the directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
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
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(dfd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(dfd)


def _png_size(data: bytes) -> Tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise InvalidArgument("template png is not a PNG file")
    return struct.unpack(">II", data[16:24])


def _clean_label(class_label: Optional[str]) -> Optional[str]:
    if class_label is None:
        return None
    if not isinstance(class_label, str) or not class_label.strip():
        raise InvalidArgument("class_label must be a non-empty string or None")
    return class_label.strip()


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
    template_id: Optional[str] = None  # registered template (register_template)

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
                       created_at=d.get("created_at"), template_id=tpl.get("template_id"))
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
    class_label: Optional[str] = None
    rotation: Optional[int] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "pin_id": self.pin_id, "origin": self.origin, "state": self.state,
            "point": _pt(self.x, self.y), "box": self.box.to_dict() if self.box else None,
            "detection_id": self.detection_id, "version": self.version,
            "class_label": self.class_label, "rotation": self.rotation,
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
    event: ReviewEvent  # for a no-op: the latest event that set the pin's state
    pin: Pin
    replayed: bool  # True when the request_id had already been applied
    noop: bool = False  # True when the request changed nothing (e.g. re-approve)


@dataclass(frozen=True)
class ScanState:
    scan: Scan
    recorded_at: str
    detections: List[Detection]
    pins: List[Pin]
    events: List[ReviewEvent]


@dataclass(frozen=True)
class Template:
    template_id: str
    sha256: str  # hex sha256 of the template pixels (contracts section 4)
    width: int
    height: int
    png: Optional[bytes]
    path: Optional[str]
    class_label: Optional[str]
    metadata: Dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class PageReview:
    canonical_page_id: str
    status: str  # "in_progress" | "complete"
    completed_at: Optional[str]
    reviewer: Optional[str]
    updated_at: str


@dataclass(frozen=True)
class BatchPage:
    """One page of a batch scan (contracts section 5a)."""

    page_index: int
    status: str  # pending | running | done | failed | skipped
    scan_id: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    attempts: int
    started_at: Optional[str]
    finished_at: Optional[str]
    counts: Dict[str, int]  # pin states of scan_id, plus "total"; empty without a scan
    review_complete: bool  # the page is marked review-complete (contracts section 5)


@dataclass(frozen=True)
class Batch:
    """A "scan every page" request: one template, many per-page scans."""

    batch_id: str
    document_id: str
    document_version: str
    mode: str
    template_page_index: Optional[int]
    template_box: Optional[Dict[str, Any]]
    template_sha256: Optional[str]
    settings: Dict[str, Any]
    metadata: Dict[str, Any]
    cancelled_at: Optional[str]
    created_at: str
    updated_at: str
    pages: List[BatchPage]

    @property
    def status(self) -> str:
        """``queued`` (nothing started), ``running``, ``cancelled`` or ``complete``."""
        states = [p.status for p in self.pages]
        if BATCH_RUNNING in states or (BATCH_PENDING in states and any(
                s != BATCH_PENDING for s in states)):
            return "running"
        if BATCH_PENDING in states:
            return "queued"
        return "cancelled" if self.cancelled_at else "complete"

    @property
    def page_counts(self) -> Dict[str, int]:
        c = {k: 0 for k in BATCH_PAGE_STATES}
        for p in self.pages:
            c[p.status] += 1
        c["total"] = len(self.pages)
        return c

    @property
    def pin_counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {k: 0 for k in PIN_STATES}
        c["total"] = 0
        for p in self.pages:
            for k, v in p.counts.items():
                c[k] = c.get(k, 0) + v
        return c


@dataclass(frozen=True)
class QueueItem:
    """An unreviewed machine pin in a batch review queue."""

    scan_id: str
    page_index: int
    pin_id: str
    score: float
    x: float
    y: float
    box: Optional[Box]
    rotation: Optional[int]
    version: int


# Label implied by an event at capture time. Removal of a manual pin carries
# no detector label; interpretation lives in export and can change later.
_EVENT_LABEL = {APPROVE: "positive", REJECT: "negative", ADD_MANUAL: "positive", REMOVE_MANUAL: None}

_AUTO = object()

TileSpec = Union[None, int, Tuple[int, int]]


class LearningStore:
    """Local learning store. One instance per thread (see ``WrongThread``).

    Opening a store creates or upgrades the schema in place (``schema.py``);
    a store newer than this code, or a pre-contract prototype, raises
    ``SchemaMismatch``.
    """

    def __init__(self, data_dir: Optional[os.PathLike] = None, *,
                 crop_renderer: Optional[CropRenderer] = None,
                 default_reviewer: Any = _AUTO,
                 clock: Callable[[], str] = _utcnow,
                 renderer_version: Optional[str] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.crops_dir = self.data_dir / CROPS_DIRNAME
        self.exports_dir = self.data_dir / EXPORTS_DIRNAME
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        self.crop_renderer = crop_renderer
        self.renderer_version = str(renderer_version or getattr(crop_renderer, "renderer_version", None)
                                    or contract.UNKNOWN_RENDERER)
        self.default_reviewer = (local_reviewer_identity() if default_reviewer is _AUTO
                                 else default_reviewer)
        self._clock = clock
        self._owner = threading.get_ident()
        self._conn = sqlite3.connect(self.data_dir / DB_FILENAME, isolation_level=None, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            try:
                self.migrated_from = schema.migrate(self._conn, contract.STORE_SCHEMA_VERSION)
            except ValueError as e:
                raise SchemaMismatch(f"{self.data_dir / DB_FILENAME}: {e}") from e
        except BaseException:
            self._conn.close()
            raise

    # ------------------------------------------------------------------ infra

    @property
    def _db(self) -> sqlite3.Connection:
        if threading.get_ident() != self._owner:
            raise WrongThread(
                "this LearningStore was opened in another thread; open one LearningStore per "
                "thread (SQLite coordinates them), e.g. keep it in a threading.local()")
        return self._conn

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "LearningStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(schema.read_version(self._db) or 0)

    class _Tx:
        def __init__(self, db: sqlite3.Connection, mode: str = "IMMEDIATE"):
            self.db = db
            self.mode = mode

        def __enter__(self):
            self.db.execute(f"BEGIN {self.mode}")
            return self.db

        def __exit__(self, exc_type, exc, tb):
            if exc_type is not None:
                self._rollback()
                return False
            try:
                self.db.execute("COMMIT")
            except BaseException:
                # A failed COMMIT (disk full, I/O error, deferred constraint)
                # can leave the transaction open; roll back so the connection
                # stays usable, then surface the original error.
                self._rollback()
                raise
            return False

        def _rollback(self) -> None:
            try:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def _tx(self) -> "LearningStore._Tx":
        return LearningStore._Tx(self._db)

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One consistent read snapshot (``BEGIN DEFERRED``). Re-entrant."""
        db = self._db
        if db.in_transaction:
            yield db
            return
        with LearningStore._Tx(db, "DEFERRED"):
            yield db

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
                if d.rotation not in contract.ROTATIONS:
                    raise ValueError(f"detection {d.detection_id}: rotation {d.rotation} not in 0/90/180/270")
                if not all(math.isfinite(v) for v in (x, y, float(d.score))):
                    raise ValueError(f"detection {d.detection_id}: non-finite value")
                norm.append((d, box, x, y))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise InvalidArgument(f"invalid scan {scan.scan_id}: {e}") from e
        ids = [d.detection_id for d, *_ in norm]
        if len(set(ids)) != len(ids):
            raise InvalidArgument(f"duplicate detection id in scan {scan.scan_id}")

        fp = {
            "document": [scan.document_id, scan.document_version, scan.page_index],
            "frame": [w, h, contract.CANONICAL_DPI],
            "template": [tpl_box, scan.template_sha256],
            "detector": [scan.detector_name, scan.detector_version, scan.detector_settings],
            "created_at": scan.created_at,
            "metadata": scan.metadata,
            "detections": [[d.detection_id, b.to_dict(), x, y, d.score, d.rotation, d.source, d.raw]
                           for d, b, x, y in norm],
        }
        if scan.template_id is not None:  # absent key keeps schema-2 fingerprints stable
            fp["template_id"] = scan.template_id
        fingerprint = _sha(fp)
        now = self._clock()
        with self._tx() as db:
            existing = db.execute("SELECT fingerprint FROM scans WHERE scan_id=?",
                                  (scan.scan_id,)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise IdempotencyConflict(f"scan {scan.scan_id} already recorded with different content")
                return
            if scan.template_id is not None and not db.execute(
                    "SELECT 1 FROM templates WHERE template_id=?", (scan.template_id,)).fetchone():
                raise NotFound(f"template {scan.template_id} is not registered; call register_template first")
            db.execute(
                "INSERT INTO scans(scan_id, document_id, document_version, page_index, canonical_page_id,"
                " frame_width, frame_height, dpi, template_box, template_sha256, detector_name,"
                " detector_version, detector_settings, detector_settings_sha, metadata, fingerprint,"
                " created_at, recorded_at, template_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scan.scan_id, scan.document_id, scan.document_version, scan.page_index,
                 scan.canonical_page_id, w, h, contract.CANONICAL_DPI,
                 _canon(tpl_box) if tpl_box else None, scan.template_sha256,
                 scan.detector_name, scan.detector_version, _canon(scan.detector_settings),
                 _sha(scan.detector_settings), _canon(scan.metadata), fingerprint,
                 scan.created_at or now, now, scan.template_id))
            for d, b, x, y in norm:
                db.execute("INSERT INTO detections(scan_id, detection_id, x0, y0, x1, y1, x, y, score,"
                           " rotation, source, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (scan.scan_id, d.detection_id, *b.edges(), x, y, float(d.score),
                            d.rotation, d.source, _canon(d.raw)))
                db.execute(
                    "INSERT INTO pins(scan_id, pin_id, origin, state, detection_id, x, y, x0, y0, x1, y1,"
                    " version, created_at, updated_at, class_label, rotation)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scan.scan_id, d.detection_id, MACHINE, UNREVIEWED, d.detection_id,
                     x, y, *b.edges(), 1, now, now, None, d.rotation))

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
        with self._read() as db:
            row = db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
            if row is None:
                raise NotFound(f"scan {scan_id} not found")
            dets = [self._detection(r) for r in db.execute(
                "SELECT * FROM detections WHERE scan_id=? ORDER BY rowid", (scan_id,))]
            pins = [self._pin(r) for r in db.execute(
                "SELECT * FROM pins WHERE scan_id=? ORDER BY created_at, rowid", (scan_id,))]
            events = [self._event(r) for r in db.execute(
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
        template: Dict[str, Any] = {"box": s.template_box, "sha256": s.template_sha256}
        if s.template_id is not None:
            template["template_id"] = s.template_id
        return {
            "scan_id": s.scan_id,
            "document": {"document_id": s.document_id, "document_version": s.document_version,
                         "page_index": s.page_index},
            "coordinate_frame": s.coordinate_frame,
            "template": template,
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
                event_id: Optional[str] = None, class_label: Optional[str] = None) -> ReviewResult:
        """Approve a machine detection (positive example).

        ``class_label`` sets the symbol class (``None`` keeps the current one).
        Approving an already approved pin without a new class label is a
        no-op (``noop=True``, no event)."""
        return self._review(APPROVE, scan_id, pin_id, request_id=request_id, source=source,
                            reviewer=reviewer, expected_version=expected_version, event_id=event_id,
                            class_label=_clean_label(class_label))

    def reject(self, scan_id: str, pin_id: str, *, request_id: str, source: str,
               reviewer: Any = _AUTO, expected_version: Optional[int] = None,
               event_id: Optional[str] = None) -> ReviewResult:
        """Reject a machine detection (negative example). Rejecting a rejected
        pin is a no-op."""
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
        db = self._db
        prev = (db.execute("SELECT action FROM review_events WHERE request_id=?", (request_id,)).fetchone()
                or db.execute("SELECT action FROM review_noops WHERE request_id=?", (request_id,)).fetchone())
        if prev is not None:  # replay: dispatch exactly as the first time
            action = prev["action"]
        else:
            action = REJECT if self.get_pin(scan_id, pin_id).origin == MACHINE else REMOVE_MANUAL
        return self._review(action, scan_id, pin_id, request_id=request_id, **kw)

    def add_manual(self, scan_id: str, x: float, y: float, *, request_id: str, source: str,
                   reviewer: Any = _AUTO, pin_id: Optional[str] = None,
                   event_id: Optional[str] = None, class_label: Optional[str] = None,
                   box: Any = None, rotation: Optional[int] = None) -> ReviewResult:
        """Add a missed receptacle at canonical px ``(x, y)`` (positive example).

        Optional ``box`` (section-3 box around the symbol, canonical px),
        ``rotation`` (0/90/180/270) and ``class_label`` describe the symbol;
        the training crop stays the section-6 128 px square around the point.
        ``pin_id`` defaults to an id derived from ``request_id`` so retries
        create exactly one pin."""
        try:
            point = contract.normalize_point(x, y)
            if not all(math.isfinite(v) for v in point):
                raise ValueError("non-finite point")
            nbox = Box.coerce(box) if box is not None else None
        except (TypeError, ValueError, KeyError, AttributeError) as e:
            raise InvalidArgument(f"invalid point ({x!r}, {y!r}) or box {box!r}: {e}") from e
        if rotation is not None and rotation not in contract.ROTATIONS:
            raise InvalidArgument(f"rotation {rotation!r} not in 0/90/180/270")
        return self._review(ADD_MANUAL, scan_id, pin_id or _derive_id("pin", request_id),
                            request_id=request_id, source=source, reviewer=reviewer,
                            point=point, event_id=event_id, class_label=_clean_label(class_label),
                            box=nbox, rotation=rotation)

    @staticmethod
    def _request_sha(action: str, scan_id: str, pin_id: str, point: Optional[tuple], source: str,
                     event_id: str, class_label: Optional[str], box: Optional[Box],
                     rotation: Optional[int]) -> str:
        return _sha({"v": 3, "action": action, "scan_id": scan_id, "pin_id": pin_id,
                     "point": list(point) if point else None, "source": source, "event_id": event_id,
                     "class_label": class_label, "box": box.to_dict() if box else None,
                     "rotation": rotation})

    @staticmethod
    def _legacy_request_sha(action: str, scan_id: str, pin_id: str, point: Optional[tuple],
                            source: str, event_id: str, recorded_reviewer: Optional[str]) -> str:
        """Schema-2 request hash, which included the reviewer. Recomputed with
        the *recorded* reviewer so a retry under a new identity still replays."""
        return _sha({"action": action, "scan_id": scan_id, "pin_id": pin_id,
                     "point": list(point) if point else None, "source": source,
                     "reviewer": recorded_reviewer, "event_id": event_id})

    def _review(self, action: str, scan_id: str, pin_id: str, *, request_id: str, source: str,
                reviewer: Any = _AUTO, expected_version: Optional[int] = None,
                event_id: Optional[str] = None, point: Optional[tuple] = None,
                class_label: Optional[str] = None, box: Optional[Box] = None,
                rotation: Optional[int] = None) -> ReviewResult:
        if not request_id or not isinstance(request_id, str):
            raise InvalidArgument("request_id is required")
        if source not in SOURCES:
            raise InvalidArgument(f"source must be one of {sorted(SOURCES)}, got {source!r}")
        reviewer = self.default_reviewer if reviewer is _AUTO else reviewer
        event_id = event_id or _derive_id("event", request_id)
        request_sha = self._request_sha(action, scan_id, pin_id, point, source, event_id,
                                        class_label, box, rotation)
        plain = class_label is None and box is None and rotation is None

        def same_request(row: sqlite3.Row) -> bool:
            if row["request_sha"] == request_sha:
                return True
            return plain and row["request_sha"] == self._legacy_request_sha(
                action, scan_id, pin_id, point, source, event_id, row["reviewer"])

        crop_key: Optional[str] = None
        with self._tx() as db:
            prev = db.execute("SELECT * FROM review_events WHERE request_id=?", (request_id,)).fetchone()
            noop_prev = (None if prev is not None else
                         db.execute("SELECT * FROM review_noops WHERE request_id=?", (request_id,)).fetchone())
            if prev is not None:
                if not same_request(prev):
                    raise IdempotencyConflict(f"request_id {request_id} reused for a different request")
                replay = self._event(prev)
                row = db.execute("SELECT created_at FROM pins WHERE scan_id=? AND pin_id=?",
                                 (replay.scan_id, replay.pin_id)).fetchone()
                pin = self._pin_from_snapshot(replay.scan_id, replay.new_state,
                                              created_at=row["created_at"] if row else replay.created_at,
                                              updated_at=replay.created_at)
                result = ReviewResult(event=replay, pin=pin, replayed=True)
                crop_key = replay.crop_key
            elif noop_prev is not None:
                if noop_prev["request_sha"] != request_sha:
                    raise IdempotencyConflict(f"request_id {request_id} reused for a different request")
                ev = self._event(db.execute("SELECT * FROM review_events WHERE event_id=?",
                                            (noop_prev["event_id"],)).fetchone())
                result = ReviewResult(event=ev, pin=self._pin_from_json(json.loads(noop_prev["pin"])),
                                      replayed=True, noop=True)
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

                target = {APPROVE: APPROVED, REJECT: REJECTED}.get(action)
                if (prior is not None and target is not None and prior.state == target
                        and (class_label is None or class_label == prior.class_label)):
                    last = self._event(db.execute(
                        "SELECT * FROM review_events WHERE scan_id=? AND pin_id=? ORDER BY seq DESC LIMIT 1",
                        (scan_id, pin_id)).fetchone())
                    db.execute("INSERT INTO review_noops(request_id, request_sha, scan_id, pin_id, action,"
                               " event_id, pin, source, reviewer, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (request_id, request_sha, scan_id, pin_id, action, last.event_id,
                                _canon(self._pin_json(prior)), source, reviewer, now))
                    return ReviewResult(event=last, pin=prior, replayed=False, noop=True)

                if action == ADD_MANUAL:
                    x, y = point  # type: ignore[misc]
                    fw, fh = scan["frame_width"], scan["frame_height"]
                    # The closed page edge is on the page (contracts §3).
                    if not (0 <= x <= fw and 0 <= y <= fh):
                        raise InvalidArgument(f"point ({x}, {y}) is outside the {fw}x{fh} canonical raster")
                    if box is not None and not (box.x < fw and box.y < fh and box.x2 > 0 and box.y2 > 0):
                        raise InvalidArgument(f"box {box.to_dict()} lies outside the {fw}x{fh} canonical raster")
                    edges = box.edges() if box is not None else (None, None, None, None)
                    db.execute(
                        "INSERT INTO pins(scan_id, pin_id, origin, state, detection_id, x, y, x0, y0, x1, y1,"
                        " version, created_at, updated_at, class_label, rotation)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (scan_id, pin_id, MANUAL, ADDED, None, x, y, *edges, 1, now, now,
                         class_label, rotation))
                else:
                    new_state = {APPROVE: APPROVED, REJECT: REJECTED, REMOVE_MANUAL: REMOVED}[action]
                    db.execute("UPDATE pins SET state=?, class_label=COALESCE(?, class_label),"
                               " version=version+1, updated_at=? WHERE scan_id=? AND pin_id=?",
                               (new_state, class_label, now, scan_id, pin_id))
                pin = self._pin(db.execute("SELECT * FROM pins WHERE scan_id=? AND pin_id=?",
                                           (scan_id, pin_id)).fetchone())

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

    # --------------------------------------------------------------- batches

    def create_batch(self, batch_id: str, *, document_id: str, document_version: str,
                     page_indexes: Sequence[int], mode: str, settings: Mapping[str, Any],
                     template_page_index: Optional[int] = None, template_box: Any = None,
                     template_sha256: Optional[str] = None,
                     metadata: Optional[Mapping[str, Any]] = None) -> Batch:
        """Record a batch scan and its pages, all ``pending`` (contracts section 5a).

        Idempotent on ``batch_id``: the same request returns the stored batch
        unchanged (whatever its progress); different content under the same id
        raises ``IdempotencyConflict``. The scans themselves are recorded with
        ``record_scan`` and attached with ``finish_batch_page``.
        """
        if not batch_id or not isinstance(batch_id, str):
            raise InvalidArgument("batch_id is required")
        if not document_id or not document_version or not mode:
            raise InvalidArgument("document_id, document_version and mode are required")
        pages = list(page_indexes)
        if not pages:
            raise InvalidArgument("a batch needs at least one page")
        if any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in pages):
            raise InvalidArgument(f"page indexes must be non-negative integers, got {pages!r}")
        if len(set(pages)) != len(pages):
            raise InvalidArgument("page indexes must not repeat")
        if template_page_index is not None and (isinstance(template_page_index, bool)
                                                or not isinstance(template_page_index, int)
                                                or template_page_index < 0):
            raise InvalidArgument("template_page_index must be a non-negative integer")
        try:
            tpl_box = Box.coerce(template_box).to_dict() if template_box is not None else None
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise InvalidArgument(f"invalid template_box: {e}") from e
        settings, metadata = dict(settings), dict(metadata or {})
        request_sha = _sha({"document": [document_id, document_version], "pages": pages,
                            "mode": mode, "settings": settings,
                            "template": [template_page_index, tpl_box, template_sha256],
                            "metadata": metadata})
        with self._tx() as db:
            cur = db.execute("SELECT request_sha FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if cur is not None:
                if cur["request_sha"] != request_sha:
                    raise IdempotencyConflict(f"batch {batch_id} already recorded with different content")
            else:
                now = self._clock()
                db.execute(
                    "INSERT INTO batches(batch_id, request_sha, document_id, document_version, mode,"
                    " template_page_index, template_box, template_sha256, settings, metadata,"
                    " cancelled_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, request_sha, document_id, document_version, mode, template_page_index,
                     _canon(tpl_box) if tpl_box else None, template_sha256, _canon(settings),
                     _canon(metadata), None, now, now))
                for pos, i in enumerate(pages):
                    db.execute("INSERT INTO batch_pages(batch_id, page_index, position, status)"
                               " VALUES (?,?,?,?)", (batch_id, i, pos, BATCH_PENDING))
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> Batch:
        with self._read() as db:
            row = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFound(f"batch {batch_id} not found")
            return self._batch(db, row)

    def list_batches(self, document_version: Optional[str] = None, *,
                     document_id: Optional[str] = None) -> List[Batch]:
        """Batches, oldest first."""
        q, args = "SELECT * FROM batches WHERE 1=1", []
        for col, val in (("document_version", document_version), ("document_id", document_id)):
            if val is not None:
                q += f" AND {col}=?"
                args.append(val)
        with self._read() as db:
            return [self._batch(db, r) for r in db.execute(q + " ORDER BY created_at, rowid", args).fetchall()]

    def next_batch_page(self, batch_id: str) -> Optional[int]:
        """Claim the next ``pending`` page (in request order) and mark it
        ``running``. ``None`` when nothing is left or the batch is cancelled."""
        with self._tx() as db:
            b = db.execute("SELECT cancelled_at FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if b is None:
                raise NotFound(f"batch {batch_id} not found")
            if b["cancelled_at"] is not None:
                return None
            r = db.execute("SELECT page_index FROM batch_pages WHERE batch_id=? AND status=?"
                           " ORDER BY position LIMIT 1", (batch_id, BATCH_PENDING)).fetchone()
            if r is None:
                return None
            now = self._clock()
            db.execute("UPDATE batch_pages SET status=?, attempts=attempts+1, started_at=?, finished_at=NULL,"
                       " error_code=NULL, error_message=NULL WHERE batch_id=? AND page_index=?",
                       (BATCH_RUNNING, now, batch_id, r["page_index"]))
            db.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", (now, batch_id))
            return int(r["page_index"])

    def finish_batch_page(self, batch_id: str, page_index: int, scan_id: str) -> BatchPage:
        """Attach the recorded scan of a ``running`` page and mark it ``done``.
        Repeating it with the same scan is a no-op."""
        with self._tx() as db:
            page = self._batch_page_row(db, batch_id, page_index)
            if page["status"] == BATCH_DONE and page["scan_id"] == scan_id:
                return self._batch_pages(db, batch_id, page_index)[0]
            if page["status"] != BATCH_RUNNING:
                raise InvalidTransition(f"batch {batch_id} page {page_index} is {page['status']}, not running")
            b = db.execute("SELECT document_version FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            scan = db.execute("SELECT document_version, page_index FROM scans WHERE scan_id=?",
                              (scan_id,)).fetchone()
            if scan is None:
                raise NotFound(f"scan {scan_id} not found; record it before finishing the page")
            if (scan["document_version"], scan["page_index"]) != (b["document_version"], page_index):
                raise InvalidArgument(f"scan {scan_id} is of {scan['document_version']} page "
                                      f"{scan['page_index']}, not batch page {page_index}")
            now = self._clock()
            db.execute("UPDATE batch_pages SET status=?, scan_id=?, finished_at=? WHERE batch_id=? AND page_index=?",
                       (BATCH_DONE, scan_id, now, batch_id, page_index))
            db.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", (now, batch_id))
            return self._batch_pages(db, batch_id, page_index)[0]

    def fail_batch_page(self, batch_id: str, page_index: int, code: str, message: str) -> BatchPage:
        """Mark a ``running`` page ``failed``. The rest of the batch carries on."""
        with self._tx() as db:
            page = self._batch_page_row(db, batch_id, page_index)
            if page["status"] != BATCH_RUNNING:
                raise InvalidTransition(f"batch {batch_id} page {page_index} is {page['status']}, not running")
            now = self._clock()
            db.execute("UPDATE batch_pages SET status=?, error_code=?, error_message=?, finished_at=?"
                       " WHERE batch_id=? AND page_index=?",
                       (BATCH_FAILED, str(code)[:200], str(message)[:2000], now, batch_id, page_index))
            db.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", (now, batch_id))
            return self._batch_pages(db, batch_id, page_index)[0]

    def cancel_batch(self, batch_id: str) -> Batch:
        """Stop a batch: ``pending`` pages become ``skipped``. A page already
        ``running`` still finishes. Idempotent, and a no-op once no page is
        pending, so a finished batch stays ``complete``."""
        with self._tx() as db:
            b = db.execute("SELECT cancelled_at FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if b is None:
                raise NotFound(f"batch {batch_id} not found")
            now = self._clock()
            pending = db.execute("SELECT 1 FROM batch_pages WHERE batch_id=? AND status=? LIMIT 1",
                                 (batch_id, BATCH_PENDING)).fetchone()
            if b["cancelled_at"] is None and pending is not None:  # a finished batch stays complete
                db.execute("UPDATE batches SET cancelled_at=?, updated_at=? WHERE batch_id=?",
                           (now, now, batch_id))
            db.execute("UPDATE batch_pages SET status=?, finished_at=? WHERE batch_id=? AND status=?",
                       (BATCH_SKIPPED, now, batch_id, BATCH_PENDING))
        return self.get_batch(batch_id)

    def requeue_batch(self, batch_id: str, *, retry_failed: bool = False,
                      interrupted: bool = True) -> Batch:
        """Put interrupted pages (left ``running`` by a crash or shutdown) back
        to ``pending``, and ``failed`` ones too when ``retry_failed``. Pass
        ``interrupted=False`` while a job is still scanning the batch, so its
        running page is left alone. A cancelled batch is not resumed; start a
        new one."""
        with self._tx() as db:
            b = db.execute("SELECT cancelled_at FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if b is None:
                raise NotFound(f"batch {batch_id} not found")
            if b["cancelled_at"] is None:
                states = ([BATCH_RUNNING] if interrupted else []) + ([BATCH_FAILED] if retry_failed else [])
                if not states:
                    return self.get_batch(batch_id)
                db.execute(f"UPDATE batch_pages SET status=?, started_at=NULL, finished_at=NULL"
                           f" WHERE batch_id=? AND status IN ({','.join('?' * len(states))})",
                           (BATCH_PENDING, batch_id, *states))
                db.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", (self._clock(), batch_id))
        return self.get_batch(batch_id)

    def batch_review_queue(self, batch_id: str, strategy: str = "margin", threshold: Optional[float] = None,
                           limit: Optional[int] = None) -> List[QueueItem]:
        """Unreviewed machine pins on every finished page of a batch, most
        uncertain first, so a reviewer can work through the whole set in one
        pass. ``strategy`` and ``threshold`` mean what they do in
        ``review_queue``; with no ``threshold`` each scan uses its own
        detector setting. Ties keep page order, then detection order."""
        if strategy not in ("margin", "lowest_score"):
            raise InvalidArgument(f"unknown review_queue strategy {strategy!r}")
        with self._read() as db:
            if db.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone() is None:
                raise NotFound(f"batch {batch_id} not found")
            rows = db.execute(
                "SELECT bp.page_index AS page_index, s.detector_settings AS settings, p.*, d.score AS score"
                " FROM batch_pages bp JOIN scans s ON s.scan_id = bp.scan_id"
                " JOIN pins p ON p.scan_id = s.scan_id"
                " JOIN detections d ON d.scan_id = p.scan_id AND d.detection_id = p.detection_id"
                " WHERE bp.batch_id=? AND bp.status=? AND p.origin='machine' AND p.state='unreviewed'"
                " ORDER BY bp.position, d.rowid", (batch_id, BATCH_DONE)).fetchall()
        if not rows:
            return []
        if strategy == "margin":
            fallback = min(r["score"] for r in rows)
            per_scan: Dict[str, float] = {}
            for r in rows:
                if r["scan_id"] not in per_scan:
                    settings = json.loads(r["settings"])
                    t = next((float(settings[k]) for k in _THRESHOLD_KEYS
                              if isinstance(settings.get(k), (int, float))), None)
                    per_scan[r["scan_id"]] = fallback if t is None else t

            def key(ir):
                i, r = ir
                t = float(threshold) if threshold is not None else per_scan[r["scan_id"]]
                return (abs(r["score"] - t), i)
        else:
            def key(ir):
                return (ir[1]["score"], ir[0])
        ordered = [r for _, r in sorted(enumerate(rows), key=key)]
        if limit is not None:
            ordered = ordered[:max(0, int(limit))]
        out = []
        for r in ordered:
            pin = self._pin(r)
            out.append(QueueItem(scan_id=pin.scan_id, page_index=int(r["page_index"]), pin_id=pin.pin_id,
                                 score=float(r["score"]), x=pin.x, y=pin.y, box=pin.box,
                                 rotation=pin.rotation, version=pin.version))
        return out

    @staticmethod
    def _batch_page_row(db: sqlite3.Connection, batch_id: str, page_index: int) -> sqlite3.Row:
        r = db.execute("SELECT * FROM batch_pages WHERE batch_id=? AND page_index=?",
                       (batch_id, page_index)).fetchone()
        if r is None:
            raise NotFound(f"page {page_index} is not part of batch {batch_id}")
        return r

    def _batch_pages(self, db: sqlite3.Connection, batch_id: str,
                     page_index: Optional[int] = None) -> List[BatchPage]:
        q, args = "SELECT * FROM batch_pages WHERE batch_id=?", [batch_id]
        if page_index is not None:
            q += " AND page_index=?"
            args.append(page_index)
        rows = db.execute(q + " ORDER BY position", args).fetchall()
        scan_ids = [r["scan_id"] for r in rows if r["scan_id"]]
        counts: Dict[str, Dict[str, int]] = {}
        complete = set()
        if scan_ids:
            marks = ",".join("?" * len(scan_ids))
            for c in db.execute(f"SELECT scan_id, state, COUNT(*) AS n FROM pins WHERE scan_id IN ({marks})"
                                f" GROUP BY scan_id, state", scan_ids):
                counts.setdefault(c["scan_id"], {})[c["state"]] = c["n"]
            complete = {r[0] for r in db.execute(
                f"SELECT s.scan_id FROM scans s JOIN page_reviews pr ON pr.canonical_page_id = s.canonical_page_id"
                f" WHERE s.scan_id IN ({marks}) AND pr.status=?", (*scan_ids, PAGE_COMPLETE))}
        out = []
        for r in rows:
            c: Dict[str, int] = {}
            if r["scan_id"]:
                c = {k: counts.get(r["scan_id"], {}).get(k, 0) for k in PIN_STATES}
                c["total"] = sum(c.values())
            out.append(BatchPage(page_index=int(r["page_index"]), status=r["status"], scan_id=r["scan_id"],
                                 error_code=r["error_code"], error_message=r["error_message"],
                                 attempts=int(r["attempts"]), started_at=r["started_at"],
                                 finished_at=r["finished_at"], counts=c,
                                 review_complete=r["scan_id"] in complete))
        return out

    def _batch(self, db: sqlite3.Connection, r: sqlite3.Row) -> Batch:
        return Batch(batch_id=r["batch_id"], document_id=r["document_id"],
                     document_version=r["document_version"], mode=r["mode"],
                     template_page_index=r["template_page_index"],
                     template_box=json.loads(r["template_box"]) if r["template_box"] else None,
                     template_sha256=r["template_sha256"], settings=json.loads(r["settings"]),
                     metadata=json.loads(r["metadata"]), cancelled_at=r["cancelled_at"],
                     created_at=r["created_at"], updated_at=r["updated_at"],
                     pages=self._batch_pages(db, r["batch_id"]))

    # ------------------------------------------------------------ page review

    def mark_page_complete(self, canonical_page_id: str, *, reviewer: Any = _AUTO,
                           allow_unreviewed: bool = False) -> PageReview:
        """Mark a page fully reviewed: every receptacle on it is pinned, so the
        rest of the page is safe background. Refuses while a scan of the page
        still has unreviewed machine pins unless ``allow_unreviewed``.
        Idempotent: an already complete page is returned unchanged."""
        reviewer = self.default_reviewer if reviewer is _AUTO else reviewer
        with self._tx() as db:
            scans = db.execute("SELECT scan_id, document_id FROM scans WHERE canonical_page_id=?",
                               (canonical_page_id,)).fetchall()
            if not scans:
                raise NotFound(f"no scans recorded for page {canonical_page_id}")
            cur = db.execute("SELECT * FROM page_reviews WHERE canonical_page_id=?",
                             (canonical_page_id,)).fetchone()
            if cur is not None and cur["status"] == PAGE_COMPLETE:
                return self._page_review(cur)
            n = db.execute("SELECT COUNT(*) FROM pins p JOIN scans s ON s.scan_id=p.scan_id"
                           " WHERE s.canonical_page_id=? AND p.origin='machine' AND p.state='unreviewed'",
                           (canonical_page_id,)).fetchone()[0]
            if n and not allow_unreviewed:
                raise InvalidTransition(f"page {canonical_page_id} still has {n} unreviewed machine pin(s); "
                                        f"review them or pass allow_unreviewed=True")
            now = self._clock()
            db.execute("INSERT OR REPLACE INTO page_reviews(canonical_page_id, document_id, status,"
                       " completed_at, reviewer, updated_at) VALUES (?,?,?,?,?,?)",
                       (canonical_page_id, scans[0]["document_id"], PAGE_COMPLETE, now, reviewer, now))
            return self._page_review(db.execute("SELECT * FROM page_reviews WHERE canonical_page_id=?",
                                                (canonical_page_id,)).fetchone())

    def mark_page_in_progress(self, canonical_page_id: str, *, reviewer: Any = _AUTO) -> PageReview:
        """Reopen (or start) the review of a page."""
        reviewer = self.default_reviewer if reviewer is _AUTO else reviewer
        with self._tx() as db:
            doc = db.execute("SELECT document_id FROM scans WHERE canonical_page_id=? LIMIT 1",
                             (canonical_page_id,)).fetchone()
            if doc is None:
                raise NotFound(f"no scans recorded for page {canonical_page_id}")
            now = self._clock()
            db.execute("INSERT OR REPLACE INTO page_reviews(canonical_page_id, document_id, status,"
                       " completed_at, reviewer, updated_at) VALUES (?,?,?,?,?,?)",
                       (canonical_page_id, doc["document_id"], PAGE_IN_PROGRESS, None, reviewer, now))
            return self._page_review(db.execute("SELECT * FROM page_reviews WHERE canonical_page_id=?",
                                                (canonical_page_id,)).fetchone())

    def page_review_status(self, canonical_page_id: str) -> Optional[PageReview]:
        """``None`` when the page has never been marked."""
        r = self._db.execute("SELECT * FROM page_reviews WHERE canonical_page_id=?",
                             (canonical_page_id,)).fetchone()
        return self._page_review(r) if r else None

    # ---------------------------------------------------------------- splits

    def set_split(self, document_id: str, split: str, *, reviewer: Any = _AUTO,
                  force: bool = False) -> str:
        """Assign a whole document to ``train``/``val``/``test``/``eval``.

        Splitting by document keeps evaluation pages out of training. Moving a
        document *out of* a held-out split (test/eval) would leak evaluation
        data, so it needs ``force=True``."""
        if split not in SPLITS:
            raise InvalidArgument(f"split must be one of {list(SPLITS)}, got {split!r}")
        if not document_id or not isinstance(document_id, str):
            raise InvalidArgument("document_id is required")
        reviewer = self.default_reviewer if reviewer is _AUTO else reviewer
        with self._tx() as db:
            cur = db.execute("SELECT split FROM splits WHERE document_id=?", (document_id,)).fetchone()
            if cur is not None:
                if cur["split"] == split:
                    return split
                if cur["split"] in HELD_OUT_SPLITS and not force:
                    raise InvalidTransition(f"document {document_id} is in held-out split {cur['split']!r}; "
                                            f"moving it to {split!r} leaks evaluation data (force=True)")
            db.execute("INSERT OR REPLACE INTO splits(document_id, split, assigned_at, reviewer)"
                       " VALUES (?,?,?,?)", (document_id, split, self._clock(), reviewer))
        return split

    def get_split(self, document_id: str) -> Optional[str]:
        r = self._db.execute("SELECT split FROM splits WHERE document_id=?", (document_id,)).fetchone()
        return r["split"] if r else None

    # ------------------------------------------------------------- templates

    def register_template(self, *, sha256: str, png: Optional[bytes] = None,
                          width: Optional[int] = None, height: Optional[int] = None,
                          path: Optional[str] = None, class_label: Optional[str] = None,
                          template_id: Optional[str] = None,
                          metadata: Optional[Mapping[str, Any]] = None) -> str:
        """Register a template (``sha256`` = hex sha256 of its pixels, as in
        the scan result). ``width``/``height`` are read from ``png`` when
        omitted. Idempotent on ``template_id`` (default: derived from sha256,
        size and class label); different content under one id conflicts."""
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise InvalidArgument("sha256 must be 64 lowercase hex characters")
        if png is not None:
            if not isinstance(png, (bytes, bytearray)):
                raise InvalidArgument("png must be bytes")
            pw, ph = _png_size(bytes(png))
            if (width is not None and width != pw) or (height is not None and height != ph):
                raise InvalidArgument(f"png is {pw}x{ph}, not {width}x{height}")
            width, height = pw, ph
        if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
            raise InvalidArgument("width and height (positive ints) are required without png")
        class_label = _clean_label(class_label)
        meta = dict(metadata or {})
        template_id = template_id or "tpl-" + _derive_id(
            "template", _canon([sha256, width, height, class_label]))
        with self._tx() as db:
            cur = db.execute("SELECT * FROM templates WHERE template_id=?", (template_id,)).fetchone()
            if cur is not None:
                same = (cur["sha256"], cur["width"], cur["height"], cur["path"], cur["class_label"],
                        json.loads(cur["metadata"]), _sha(bytes(cur["png"])) if cur["png"] else None) == \
                       (sha256, width, height, path, class_label, meta, _sha(bytes(png)) if png else None)
                if not same:
                    raise IdempotencyConflict(f"template {template_id} already registered with different content")
                return template_id
            db.execute("INSERT INTO templates(template_id, sha256, width, height, png, path, class_label,"
                       " metadata, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (template_id, sha256, width, height, bytes(png) if png else None, path, class_label,
                        _canon(meta), self._clock()))
        return template_id

    def get_template(self, template_id: str) -> Template:
        r = self._db.execute("SELECT * FROM templates WHERE template_id=?", (template_id,)).fetchone()
        if r is None:
            raise NotFound(f"template {template_id} not found")
        return self._template(r)

    def list_templates(self, *, class_label: Optional[str] = None) -> List[Template]:
        q, args = "SELECT * FROM templates", []
        if class_label is not None:
            q, args = q + " WHERE class_label=?", [class_label]
        return [self._template(r) for r in self._db.execute(q + " ORDER BY created_at, rowid", args)]

    # ------------------------------------------------------------------ crops

    def _crop_spec(self, scan: Mapping[str, Any], pin: Pin) -> CropSpec:
        return contract.pin_crop(scan["document_version"], scan["page_index"],
                                 scan["frame_width"], scan["frame_height"], pin.x, pin.y, pin.box,
                                 origin=pin.origin, renderer_version=self.renderer_version)

    def _ensure_crop(self, db: sqlite3.Connection, spec: CropSpec, now: str) -> str:
        key = spec.key()
        db.execute("INSERT OR IGNORE INTO crops(crop_key, spec, status, created_at, updated_at)"
                   " VALUES (?,?,?,?,?)", (key, _canon(spec.to_dict()), CROP_PENDING, now, now))
        return key

    def crop_path(self, crop_key: str) -> Path:
        return self.crops_dir / crop_key[:2] / f"{crop_key}.png"

    def _file_sha(self, rel_path: Optional[str]) -> Optional[str]:
        """sha256 of a crop file, or ``None`` when it is missing."""
        if not rel_path:
            return None
        try:
            return _sha((self.data_dir / rel_path).read_bytes())
        except OSError:
            return None

    def _materialize(self, crop_key: str) -> str:
        db = self._db
        row = db.execute("SELECT * FROM crops WHERE crop_key=?", (crop_key,)).fetchone()
        if row is None:
            raise NotFound(f"crop {crop_key} not found")
        note = None
        if row["status"] == CROP_WRITTEN:
            on_disk = self._file_sha(row["rel_path"])
            if on_disk is not None and (row["sha256"] is None or on_disk == row["sha256"]):
                return CROP_WRITTEN
            note = "file missing" if on_disk is None else "file sha256 does not match the recorded sha256"
        if self.crop_renderer is None:
            if note:
                db.execute("UPDATE crops SET status=?, last_error=?, updated_at=? WHERE crop_key=?",
                           (CROP_PENDING, note, self._clock(), crop_key))
                return CROP_PENDING
            return row["status"]
        spec = CropSpec.from_dict(json.loads(row["spec"]))
        path = self.crop_path(crop_key)
        try:
            data = self.crop_renderer(spec)
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise LearningStoreError("crop renderer returned no bytes", "crop_render_failed")
            _atomic_write(path, bytes(data))
        except Exception as e:  # recorded, retried by process_pending_crops()
            db.execute("UPDATE crops SET status=?, attempts=attempts+1, last_error=?, updated_at=?"
                       " WHERE crop_key=?", (CROP_FAILED, f"{type(e).__name__}: {e}"[:2000],
                                             self._clock(), crop_key))
            return CROP_FAILED
        new_sha = _sha(bytes(data))
        drift = None
        if row["sha256"] and row["sha256"] != new_sha:
            # Same spec, different pixels: the renderer is not deterministic
            # or changed without a new renderer_version.
            drift = f"re-rendered crop differs from recorded sha256 {row['sha256']}"
        db.execute("UPDATE crops SET status=?, rel_path=?, sha256=?, attempts=attempts+1,"
                   " last_error=?, updated_at=? WHERE crop_key=?",
                   (CROP_WRITTEN, path.relative_to(self.data_dir).as_posix(), new_sha, drift,
                    self._clock(), crop_key))
        return CROP_WRITTEN

    def process_pending_crops(self, *, verify_written: bool = True) -> Dict[str, int]:
        """(Re)generate crops that are pending, failed, missing on disk, or
        whose file no longer matches the recorded sha256."""
        db = self._db
        if verify_written:
            for r in db.execute("SELECT crop_key, rel_path, sha256 FROM crops WHERE status=?",
                                (CROP_WRITTEN,)).fetchall():
                on_disk = self._file_sha(r["rel_path"])
                if on_disk is None or (r["sha256"] and on_disk != r["sha256"]):
                    reason = "file missing" if on_disk is None else "file sha256 does not match the recorded sha256"
                    db.execute("UPDATE crops SET status=?, last_error=?, updated_at=? WHERE crop_key=?",
                               (CROP_PENDING, reason, self._clock(), r["crop_key"]))
        counts = {CROP_WRITTEN: 0, CROP_FAILED: 0, CROP_PENDING: 0}
        keys = [r["crop_key"] for r in db.execute(
            "SELECT crop_key FROM crops WHERE status IN (?,?) ORDER BY created_at",
            (CROP_PENDING, CROP_FAILED))]
        for k in keys:
            counts[self._materialize(k)] += 1
        return counts

    def crop_status_counts(self) -> Dict[str, int]:
        return {r["status"]: r["n"] for r in self._db.execute(
            "SELECT status, COUNT(*) AS n FROM crops GROUP BY status")}

    # --------------------------------------------------------------- snapshot

    def _snapshot(self, db: sqlite3.Connection, where: Sequence[str], args: Sequence[Any]) -> Dict[str, Any]:
        """Everything about the selected scans in a fixed number of queries.

        Must run inside ``_read()`` so all queries see one snapshot. ``where``
        fragments may use aliases ``s`` (scans) and ``sp`` (splits)."""
        sel = ("SELECT s.scan_id FROM scans s LEFT JOIN splits sp ON sp.document_id = s.document_id"
               + (" WHERE " + " AND ".join(where) if where else ""))
        a = list(args)
        scans = db.execute(f"SELECT s.*, sp.split AS split FROM scans s LEFT JOIN splits sp"
                           f" ON sp.document_id = s.document_id WHERE s.scan_id IN ({sel})"
                           f" ORDER BY s.recorded_at, s.rowid", a).fetchall()
        dets: Dict[str, List[sqlite3.Row]] = {}
        for r in db.execute(f"SELECT * FROM detections WHERE scan_id IN ({sel}) ORDER BY rowid", a):
            dets.setdefault(r["scan_id"], []).append(r)
        pins: Dict[str, List[sqlite3.Row]] = {}
        for r in db.execute(f"SELECT * FROM pins WHERE scan_id IN ({sel}) ORDER BY created_at, rowid", a):
            pins.setdefault(r["scan_id"], []).append(r)
        events: Dict[str, List[sqlite3.Row]] = {}
        for r in db.execute(f"SELECT * FROM review_events WHERE scan_id IN ({sel}) ORDER BY seq", a):
            events.setdefault(r["scan_id"], []).append(r)
        crops = {r["crop_key"]: r for r in db.execute(
            f"SELECT * FROM crops WHERE crop_key IN (SELECT crop_key FROM review_events"
            f" WHERE crop_key IS NOT NULL AND scan_id IN ({sel}))", a)}
        templates = {r["template_id"]: r for r in db.execute(
            "SELECT template_id, sha256, width, height, path, class_label, metadata, created_at"
            f" FROM templates WHERE template_id IN (SELECT template_id FROM scans WHERE scan_id IN ({sel}))",
            a)}
        page_reviews = {r["canonical_page_id"]: r for r in db.execute(
            f"SELECT * FROM page_reviews WHERE canonical_page_id IN"
            f" (SELECT canonical_page_id FROM scans WHERE scan_id IN ({sel}))", a)}
        return {"scans": scans, "dets": dets, "pins": pins, "events": events, "crops": crops,
                "templates": templates, "page_reviews": page_reviews}

    @staticmethod
    def _heldout_clause(include_heldout: bool) -> List[str]:
        if include_heldout:
            return []
        return ["(sp.split IS NULL OR sp.split NOT IN (%s))" % ",".join(f"'{s}'" for s in HELD_OUT_SPLITS)]

    def _effective_class(self, pin: Pin, scan_row: Mapping[str, Any],
                         templates: Mapping[str, Any]) -> Optional[str]:
        if pin.class_label:
            return pin.class_label
        tpl = templates.get(scan_row["template_id"]) if scan_row["template_id"] else None
        return tpl["class_label"] if tpl is not None else None

    def _labeled_pins(self, snap: Dict[str, Any]):
        """Yield ``(scan_row, pin, events, label, label_event, status)`` per pin."""
        for srow in snap["scans"]:
            by_pin: Dict[str, List[ReviewEvent]] = {}
            for er in snap["events"].get(srow["scan_id"], []):
                ev = self._event(er)
                by_pin.setdefault(ev.pin_id, []).append(ev)
            for prow in snap["pins"].get(srow["scan_id"], []):
                pin = self._pin(prow)
                evs = by_pin.get(pin.pin_id, [])
                label, label_event, status = interpret_pin(pin, evs)
                yield srow, pin, evs, label, label_event, status

    # ----------------------------------------------------------------- export

    def export(self, *, document_version: Optional[str] = None, include_unlabeled: bool = True,
               out_path: Optional[os.PathLike] = None, include_heldout: bool = False) -> Dict[str, Any]:
        """Versioned metadata export with crop references.

        All reads share one snapshot. Labels are derived from raw events at
        export time (see ``interpret_pin``); raw events are included so they
        can be reinterpreted later without the database. Documents in the
        ``test``/``eval`` splits are left out unless ``include_heldout``.
        ``label_conflicts`` lists crops labelled both positive and negative
        (the same crop reviewed differently in two scans).
        """
        where: List[str] = self._heldout_clause(include_heldout)
        args: List[Any] = []
        if document_version is not None:
            where.append("s.document_version = ?")
            args.append(document_version)
        with self._read() as db:
            snap = self._snapshot(db, where, args)
            exported_at = self._clock()

        crops: Dict[str, Dict[str, Any]] = {}
        examples: List[Dict[str, Any]] = []
        raw_events: List[Dict[str, Any]] = []
        for srow in snap["scans"]:
            raw_events.extend(self._event_dict(self._event(r)) for r in snap["events"].get(srow["scan_id"], []))
        for srow, pin, evs, label, label_event, status in self._labeled_pins(snap):
            if label is None and not include_unlabeled:
                continue
            crop_key = label_event.crop_key if label_event else None
            if crop_key is None:
                spec = self._crop_spec(srow, pin)
                crop_key = spec.key()
                crops.setdefault(crop_key, {"status": "not_captured", "path": None,
                                            "sha256": None, "spec": spec.to_dict()})
            else:
                r = snap["crops"][crop_key]
                crops.setdefault(crop_key, {"status": r["status"], "path": r["rel_path"],
                                            "sha256": r["sha256"], "spec": json.loads(r["spec"]),
                                            "last_error": r["last_error"]})
            det = None
            if pin.detection_id:
                det = next((d for d in snap["dets"].get(srow["scan_id"], [])
                            if d["detection_id"] == pin.detection_id), None)
            examples.append({
                "example_id": f"{srow['scan_id']}/{pin.pin_id}",
                "scan_id": srow["scan_id"],
                "canonical_page_id": srow["canonical_page_id"],
                "pin_id": pin.pin_id,
                "origin": pin.origin,
                "pin_state": pin.state,
                "label": label,
                "label_status": status,
                "label_event_id": label_event.event_id if label_event else None,
                "event_ids": [e.event_id for e in evs],
                "point": _pt(pin.x, pin.y),
                "box": pin.box.to_dict() if pin.box else None,
                "rotation": pin.rotation,
                "class_label": self._effective_class(pin, srow, snap["templates"]),
                "detection": ({"id": det["detection_id"], "score": det["score"], "rotation": det["rotation"],
                               "source": det["source"], "raw": json.loads(det["raw"])} if det else None),
                "reviewer": label_event.reviewer if label_event else None,
                "source": label_event.source if label_event else None,
                "labeled_at": label_event.created_at if label_event else None,
                "crop_key": crop_key,
            })

        by_crop: Dict[str, Dict[str, List[str]]] = {}
        for ex in examples:
            if ex["label"] is not None:
                by_crop.setdefault(ex["crop_key"], {}).setdefault(ex["label"], []).append(ex["example_id"])
        conflicts = [{"crop_key": k, "labels": sorted(v), "example_ids": v}
                     for k, v in sorted(by_crop.items()) if len(v) > 1]

        doc = {
            "schema": contract.EXPORT_SCHEMA,
            "schema_version": contract.EXPORT_SCHEMA_VERSION,
            "store_schema_version": contract.STORE_SCHEMA_VERSION,
            "exported_at": exported_at,
            "filter": {"document_version": document_version, "include_unlabeled": include_unlabeled,
                       "include_heldout": include_heldout},
            "crop_settings": {
                "spec_version": contract.CROP_SPEC_VERSION,
                "units": contract.FRAME_SPACE,
                "dpi": contract.CANONICAL_DPI,
                "manual_crop_size_px": contract.MANUAL_CROP_SIZE_PX,
                "detection_crop_margin_px": contract.DETECTION_CROP_MARGIN_PX,
                "renderer_version": self.renderer_version,
                "manual_rule": "x0 = floor(x - size/2 + 0.5), x1 = x0 + size (same for y), clipped",
                "detection_rule": "floor(x) - margin .. ceil(x + width) + margin (same for y), clipped",
            },
            "label_policy": {
                "approve": "positive", "reject": "negative", "add_manual": "positive",
                "remove_manual": "none (manual pin withdrawn; not a detector negative)",
                "unreviewed": "unlabeled", "multiple_reviews": "latest approve/reject wins",
            },
            "scans": [self._scan_dict(r) for r in snap["scans"]],
            "examples": examples,
            "crops": crops,
            "events": raw_events,
            "label_conflicts": conflicts,
            "page_reviews": {k: {"status": r["status"], "completed_at": r["completed_at"],
                                 "reviewer": r["reviewer"]} for k, r in sorted(snap["page_reviews"].items())},
            "splits": {r["document_id"]: r["split"] for r in snap["scans"] if r["split"]},
            "templates": {k: {"sha256": r["sha256"], "width": r["width"], "height": r["height"],
                              "class_label": r["class_label"], "path": r["path"]}
                          for k, r in sorted(snap["templates"].items())},
        }
        if out_path is not None:
            _atomic_write(Path(out_path), json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
        return doc

    # ------------------------------------------------------------ loop APIs

    def review_stats(self, document_id: Optional[str] = None, template_sha256: Optional[str] = None,
                     detector_settings_sha: Optional[str] = None, *,
                     include_heldout: bool = False) -> List[Tuple[float, int]]:
        """``[(score, label)]`` for reviewed machine pins (approved = 1,
        rejected = 0) from comparable scans, highest score first. Filters
        narrow "comparable" (``detector_settings_sha`` is the
        ``settings_sha256`` of the scan's settings). Held-out documents are
        excluded unless ``include_heldout``, so thresholds are never tuned on
        evaluation data. Pins below a scan's own threshold were never
        proposed, so they are absent."""
        where = ["p.origin = 'machine'", "p.state IN ('approved','rejected')"]
        where += self._heldout_clause(include_heldout)
        args: List[Any] = []
        for col, val in (("s.document_id", document_id), ("s.template_sha256", template_sha256),
                         ("s.detector_settings_sha", detector_settings_sha)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        rows = self._db.execute(
            "SELECT d.score AS score, p.state AS state FROM pins p"
            " JOIN detections d ON d.scan_id = p.scan_id AND d.detection_id = p.detection_id"
            " JOIN scans s ON s.scan_id = p.scan_id"
            " LEFT JOIN splits sp ON sp.document_id = s.document_id"
            " WHERE " + " AND ".join(where) + " ORDER BY d.score DESC, s.recorded_at, p.rowid", args)
        return [(float(r["score"]), 1 if r["state"] == APPROVED else 0) for r in rows]

    def review_queue(self, scan_id: str, strategy: str = "margin", threshold: Optional[float] = None,
                     limit: Optional[int] = 50) -> List[str]:
        """Unreviewed machine pin ids, most uncertain first.

        * ``margin``: ascending ``|score - threshold|``. ``threshold``
          defaults to the scan's detector setting (``threshold``,
          ``score_threshold`` or ``min_score``), else the lowest score in the
          scan (which orders by ascending score).
        * ``lowest_score``: ascending score.
        """
        if strategy not in ("margin", "lowest_score"):
            raise InvalidArgument(f"unknown review_queue strategy {strategy!r}")
        with self._read() as db:
            scan = db.execute("SELECT detector_settings FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
            if scan is None:
                raise NotFound(f"scan {scan_id} not found")
            rows = db.execute(
                "SELECT p.pin_id AS pin_id, d.score AS score FROM pins p JOIN detections d"
                " ON d.scan_id = p.scan_id AND d.detection_id = p.detection_id"
                " WHERE p.scan_id=? AND p.origin='machine' AND p.state='unreviewed' ORDER BY d.rowid",
                (scan_id,)).fetchall()
        if not rows:
            return []
        if strategy == "margin":
            if threshold is None:
                settings = json.loads(scan["detector_settings"])
                threshold = next((float(settings[k]) for k in _THRESHOLD_KEYS
                                  if isinstance(settings.get(k), (int, float))), None)
            if threshold is None:
                threshold = min(r["score"] for r in rows)
            t = float(threshold)
            ordered = sorted(enumerate(rows), key=lambda ir: (abs(ir[1]["score"] - t), ir[0]))
        else:
            ordered = sorted(enumerate(rows), key=lambda ir: (ir[1]["score"], ir[0]))
        ids = [r["pin_id"] for _, r in ordered]
        return ids if limit is None else ids[:max(0, int(limit))]

    def template_bank_crops(self, document_id: Optional[str] = None, class_label: Optional[str] = None,
                            *, include_heldout: bool = False, written_only: bool = True) -> List[CropRecord]:
        """Positive crops: approved machine pins and added manual pins.

        ``class_label`` matches the pin's label, else its scan template's
        label. Crops labelled both ways across scans are left out of both
        this and ``negative_crops``. Each crop appears once."""
        return self._crop_records("positive", document_id, class_label, include_heldout, written_only)

    def negative_crops(self, document_id: Optional[str] = None, class_label: Optional[str] = None,
                       *, include_heldout: bool = False, written_only: bool = True) -> List[CropRecord]:
        """Negative crops: rejected machine pins (never removed manual pins)."""
        return self._crop_records("negative", document_id, class_label, include_heldout, written_only)

    def _crop_records(self, want: str, document_id: Optional[str], class_label: Optional[str],
                      include_heldout: bool, written_only: bool) -> List[CropRecord]:
        where = self._heldout_clause(include_heldout)
        args: List[Any] = []
        if document_id is not None:
            where.append("s.document_id = ?")
            args.append(document_id)
        with self._read() as db:
            snap = self._snapshot(db, where, args)
        labels: Dict[str, set] = {}
        cands: List[CropRecord] = []
        for srow, pin, _evs, label, ev, _status in self._labeled_pins(snap):
            if label is None or ev is None or ev.crop_key is None:
                continue
            labels.setdefault(ev.crop_key, set()).add(label)
            if label != want:
                continue
            cls = self._effective_class(pin, srow, snap["templates"])
            if class_label is not None and cls != class_label:
                continue
            crop = snap["crops"][ev.crop_key]
            if written_only and crop["status"] != CROP_WRITTEN:
                continue
            spec = json.loads(crop["spec"])
            score = None
            if pin.detection_id:
                score = next((d["score"] for d in snap["dets"].get(srow["scan_id"], [])
                              if d["detection_id"] == pin.detection_id), None)
            cands.append(CropRecord(
                crop_key=ev.crop_key,
                path=str(self.data_dir / crop["rel_path"]) if crop["rel_path"] and crop["status"] == CROP_WRITTEN
                else None,
                sha256=crop["sha256"], label=label, class_label=cls, status=crop["status"],
                scan_id=srow["scan_id"], pin_id=pin.pin_id, origin=pin.origin,
                canonical_page_id=srow["canonical_page_id"], document_id=srow["document_id"],
                box=pin.box.to_dict() if pin.box else None, crop_box=spec["box"],
                rotation=pin.rotation, score=score))
        out, seen = [], set()
        for c in cands:
            if len(labels[c.crop_key]) > 1 or c.crop_key in seen:
                continue
            seen.add(c.crop_key)
            out.append(c)
        return out

    # --------------------------------------------------------- dataset export

    def export_dataset(self, out_dir: os.PathLike, fmt: str = "coco", split: str = "train",
                       require_page_complete: bool = True, tile: TileSpec = None, *,
                       include_unassigned: Optional[bool] = None,
                       manual_box_px: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        """Write a detector training set for one document split; returns the manifest.

        * Only documents in ``split`` are used (documents with no split count
          as ``train`` unless ``include_unassigned=False``). Held-out splits
          appear only when asked for by name.
        * With ``require_page_complete`` only pages marked complete are used,
          because the rest of such a page is trustworthy background.
        * Annotations: approved machine pins (detection box) and added manual
          pins (their box, else a template-sized box centred on the point:
          scan template box, registered template, then ``manual_box_px``).
          Overlapping boxes from several scans of one page (IoU > 0.5) are merged.
          Categories come from class labels (default ``receptacle``).
        * ``tile``: ``size`` or ``(size, overlap)`` px; boxes are clipped to
          each tile and kept when at least half is visible.
        * The store holds no rasters: images are referenced by
          ``canonical_page_id`` and ``file_name`` and the render service
          supplies them (canonical raster, 200 DPI).

        Writes ``<split>.coco.json`` and ``manifest.json`` into ``out_dir``
        and records the export in ``dataset_exports``.
        """
        if fmt != "coco":
            raise InvalidArgument(f"unsupported dataset format {fmt!r} (supported: 'coco')")
        if split not in SPLITS:
            raise InvalidArgument(f"split must be one of {list(SPLITS)}, got {split!r}")
        if tile is None:
            tile_spec = None
        else:
            size, overlap = (tile, 0) if isinstance(tile, int) else tuple(tile)
            if not (isinstance(size, int) and isinstance(overlap, int) and size > 0 and 0 <= overlap < size):
                raise InvalidArgument("tile must be size or (size, overlap) with 0 <= overlap < size")
            tile_spec = (size, overlap)
        if include_unassigned is None:
            include_unassigned = split == "train"
        split_clause = "(sp.split = ?" + (" OR sp.split IS NULL)" if include_unassigned else ")")

        with self._read() as db:
            snap = self._snapshot(db, [split_clause], [split])

        by_page: Dict[str, List[Tuple[Any, Pin, Optional[str], Optional[ReviewEvent]]]] = {}
        frames: Dict[str, Any] = {}
        for srow, pin, _evs, label, ev, _st in self._labeled_pins(snap):
            frames.setdefault(srow["canonical_page_id"], srow)
            by_page.setdefault(srow["canonical_page_id"], [])
            if label == "positive":
                by_page[srow["canonical_page_id"]].append((srow, pin, label, ev))
        for srow in snap["scans"]:
            frames.setdefault(srow["canonical_page_id"], srow)
            by_page.setdefault(srow["canonical_page_id"], [])

        pages, incomplete, warnings = [], [], []
        for cpid in sorted(by_page):
            pr = snap["page_reviews"].get(cpid)
            if require_page_complete and (pr is None or pr["status"] != PAGE_COMPLETE):
                incomplete.append(cpid)
                continue
            f = frames[cpid]
            W, H = f["frame_width"], f["frame_height"]
            anns = []
            for srow, pin, _label, ev in by_page[cpid]:
                if pin.box is not None:
                    e = pin.box.edges()
                else:
                    size = self._manual_box_size(srow, snap["templates"], manual_box_px)
                    if size is None:
                        warnings.append(f"{srow['scan_id']}/{pin.pin_id}: manual pin without box or "
                                        f"template size; not annotated")
                        continue
                    e = (pin.x - size[0] / 2, pin.y - size[1] / 2, pin.x + size[0] / 2, pin.y + size[1] / 2)
                e = (max(0.0, e[0]), max(0.0, e[1]), min(float(W), e[2]), min(float(H), e[3]))
                if e[2] <= e[0] or e[3] <= e[1]:
                    continue
                cls = self._effective_class(pin, srow, snap["templates"]) or loop.DEFAULT_CLASS_LABEL
                anns.append({"category": cls, "edges": e,
                             "pin": {"scan_id": srow["scan_id"], "pin_id": pin.pin_id, "origin": pin.origin,
                                     "rotation": pin.rotation,
                                     "label_event_id": ev.event_id if ev else None,
                                     "crop_key": ev.crop_key if ev else None}})
            pages.append({"canonical_page_id": cpid, "document_version": f["document_version"],
                          "page_index": f["page_index"], "document_id": f["document_id"],
                          "width": W, "height": H, "annotations": loop.dedupe_boxes(anns)})

        coco = loop.build_coco(pages, tile_spec)
        data = json.dumps(coco, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = _sha(data)
        out = Path(out_dir)
        ann_name = f"{split}.coco.json"
        _atomic_write(out / ann_name, data)
        per_cat: Dict[str, int] = {}
        names = {c["id"]: c["name"] for c in coco["categories"]}
        for a in coco["annotations"]:
            per_cat[names[a["category_id"]]] = per_cat.get(names[a["category_id"]], 0) + 1
        export_id = str(uuid.uuid4())
        manifest = {
            "schema": "pinny.learning.dataset", "schema_version": 1,
            "export_id": export_id, "format": fmt, "split": split,
            "created_at": self._clock(),
            "annotation_file": ann_name, "sha256": digest,
            "store_schema_version": contract.STORE_SCHEMA_VERSION,
            "require_page_complete": require_page_complete, "include_unassigned": include_unassigned,
            "tile": {"size": tile_spec[0], "overlap": tile_spec[1]} if tile_spec else None,
            "pages": [p["canonical_page_id"] for p in pages],
            "documents": sorted({p["document_id"] for p in pages}),
            "excluded_incomplete_pages": incomplete,
            "counts": {"pages": len(pages), "images": len(coco["images"]),
                       "annotations": len(coco["annotations"]), "per_category": per_cat},
            "warnings": warnings,
            "images": {"included": False, "source": "render service: render_page(document_version, "
                                                     "page_index) at 200 DPI (contracts sections 2, 9)",
                       "file_name": "<document_version with ':' as '-'>_p<page_index>[_x<x>_y<y>].png"},
        }
        _atomic_write(out / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
        with self._tx() as db:
            db.execute("INSERT INTO dataset_exports(export_id, format, split, sha256, out_dir, manifest,"
                       " created_at) VALUES (?,?,?,?,?,?,?)",
                       (export_id, fmt, split, digest, str(out), _canon(manifest), manifest["created_at"]))
        return manifest

    def list_dataset_exports(self) -> List[Dict[str, Any]]:
        return [json.loads(r["manifest"]) for r in self._db.execute(
            "SELECT manifest FROM dataset_exports ORDER BY created_at, rowid")]

    @staticmethod
    def _manual_box_size(srow: Mapping[str, Any], templates: Mapping[str, Any],
                         fallback: Optional[Tuple[int, int]]) -> Optional[Tuple[float, float]]:
        if srow["template_box"]:
            b = json.loads(srow["template_box"])
            return (b["width"], b["height"])
        tpl = templates.get(srow["template_id"]) if srow["template_id"] else None
        if tpl is not None:
            return (tpl["width"], tpl["height"])
        return tuple(fallback) if fallback else None  # type: ignore[return-value]

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
                    metadata=json.loads(r["metadata"]), template_id=r["template_id"])

    @staticmethod
    def _scan_dict(r: sqlite3.Row) -> Dict[str, Any]:
        s = LearningStore._scan(r)
        return {"scan_id": s.scan_id,
                "document": {"document_id": s.document_id, "document_version": s.document_version,
                             "page_index": s.page_index},
                "canonical_page_id": s.canonical_page_id,
                "coordinate_frame": s.coordinate_frame,
                "template": {"box": s.template_box, "sha256": s.template_sha256,
                             "template_id": s.template_id},
                "detector": {"name": s.detector_name, "version": s.detector_version,
                             "settings": s.detector_settings,
                             "settings_sha256": r["detector_settings_sha"]},
                "metadata": s.metadata, "created_at": s.created_at, "recorded_at": r["recorded_at"]}

    @staticmethod
    def _detection(r: sqlite3.Row) -> Detection:
        return Detection(detection_id=r["detection_id"],
                         box=Box.from_edges((r["x0"], r["y0"], r["x1"], r["y1"])),
                         score=r["score"], rotation=r["rotation"], source=r["source"],
                         x=r["x"], y=r["y"], raw=json.loads(r["raw"]))

    @staticmethod
    def _pin(r: sqlite3.Row) -> Pin:
        box = Box.from_edges((r["x0"], r["y0"], r["x1"], r["y1"])) if r["x0"] is not None else None
        return Pin(pin_id=r["pin_id"], scan_id=r["scan_id"], origin=r["origin"], state=r["state"],
                   x=r["x"], y=r["y"], box=box, detection_id=r["detection_id"], version=r["version"],
                   created_at=r["created_at"], updated_at=r["updated_at"],
                   class_label=r["class_label"], rotation=r["rotation"])

    @staticmethod
    def _pin_from_snapshot(scan_id: str, snap: Mapping[str, Any], *, created_at: str,
                           updated_at: str) -> Pin:
        """The pin exactly as an event recorded it (``new_state``)."""
        box = snap.get("box")
        return Pin(pin_id=snap["pin_id"], scan_id=scan_id, origin=snap["origin"], state=snap["state"],
                   x=snap["point"]["x"], y=snap["point"]["y"],
                   box=Box(**box) if box else None, detection_id=snap.get("detection_id"),
                   version=snap["version"], created_at=created_at, updated_at=updated_at,
                   class_label=snap.get("class_label"), rotation=snap.get("rotation"))

    @staticmethod
    def _pin_json(p: Pin) -> Dict[str, Any]:
        d = p.snapshot()
        d.update(scan_id=p.scan_id, created_at=p.created_at, updated_at=p.updated_at)
        return d

    @staticmethod
    def _pin_from_json(d: Mapping[str, Any]) -> Pin:
        return LearningStore._pin_from_snapshot(d["scan_id"], d, created_at=d["created_at"],
                                                updated_at=d["updated_at"])

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

    @staticmethod
    def _template(r: sqlite3.Row) -> Template:
        return Template(template_id=r["template_id"], sha256=r["sha256"], width=r["width"],
                        height=r["height"], png=bytes(r["png"]) if r["png"] is not None else None,
                        path=r["path"], class_label=r["class_label"], metadata=json.loads(r["metadata"]),
                        created_at=r["created_at"])

    @staticmethod
    def _page_review(r: sqlite3.Row) -> PageReview:
        return PageReview(canonical_page_id=r["canonical_page_id"], status=r["status"],
                          completed_at=r["completed_at"], reviewer=r["reviewer"], updated_at=r["updated_at"])


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
