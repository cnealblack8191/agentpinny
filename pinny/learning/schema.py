"""Versioned SQLite schema for the learning store.

The schema version lives in ``store_meta.schema_version``. A new database is
created from the frozen schema-2 baseline and then upgraded through every
migration, so fresh and upgraded stores always go through the same DDL.

Rules for adding a migration:

* Never edit ``BASELINE_V2`` or an existing migration. Append a new
  ``_MIGRATIONS[n]`` that turns version ``n`` into ``n + 1`` and bump
  ``contract.STORE_SCHEMA_VERSION``.
* Migrations are plain SQL statements (no ``executescript``, which would
  commit). ``migrate()`` runs all pending steps in one ``BEGIN IMMEDIATE``
  transaction, so an upgrade either fully applies or leaves the file as it was.
* Schema 1 (the pre-contract prototype in PDF points) has no migration and is
  refused, because its coordinates cannot be converted without the PDFs.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Dict, List, Optional

MIN_MIGRATABLE_VERSION = 2

# Schema 2 exactly as shipped at commit fcb2728 (contracts v1). Frozen.
BASELINE_V2 = """
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

# Schema 2 -> 3: learning-loop data and stricter immutability.
_V2_TO_V3 = """
CREATE TABLE templates (
    template_id  TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL,
    width        INTEGER NOT NULL CHECK (width > 0),
    height       INTEGER NOT NULL CHECK (height > 0),
    png          BLOB,
    path         TEXT,
    class_label  TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE INDEX templates_by_sha ON templates(sha256);

ALTER TABLE scans ADD COLUMN template_id TEXT REFERENCES templates(template_id);
ALTER TABLE pins ADD COLUMN class_label TEXT;
ALTER TABLE pins ADD COLUMN rotation INTEGER CHECK (rotation IS NULL OR rotation IN (0, 90, 180, 270));
UPDATE pins SET rotation = (SELECT d.rotation FROM detections d
                            WHERE d.scan_id = pins.scan_id AND d.detection_id = pins.detection_id)
 WHERE origin = 'machine';

CREATE INDEX scans_by_document ON scans(document_id);

-- Requests that changed nothing (e.g. approving an approved pin). Kept so a
-- retry of the same request_id replays instead of being applied later.
CREATE TABLE review_noops (
    request_id   TEXT PRIMARY KEY,
    request_sha  TEXT NOT NULL,
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    pin_id       TEXT NOT NULL,
    action       TEXT NOT NULL,
    event_id     TEXT REFERENCES review_events(event_id),
    pin          TEXT NOT NULL,
    source       TEXT NOT NULL,
    reviewer     TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE page_reviews (
    canonical_page_id TEXT PRIMARY KEY,
    document_id       TEXT,
    status            TEXT NOT NULL CHECK (status IN ('in_progress','complete')),
    completed_at      TEXT,
    reviewer          TEXT,
    updated_at        TEXT NOT NULL
);

CREATE TABLE splits (
    document_id  TEXT PRIMARY KEY,
    split        TEXT NOT NULL CHECK (split IN ('train','val','test','eval')),
    assigned_at  TEXT NOT NULL,
    reviewer     TEXT
);

CREATE TABLE dataset_exports (
    export_id    TEXT PRIMARY KEY,
    format       TEXT NOT NULL,
    split        TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    out_dir      TEXT NOT NULL,
    manifest     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TRIGGER scans_no_update
BEFORE UPDATE ON scans BEGIN
    SELECT RAISE(ABORT, 'scans are immutable');
END;
CREATE TRIGGER detections_no_delete
BEFORE DELETE ON detections BEGIN
    SELECT RAISE(ABORT, 'detections are immutable');
END;
CREATE TRIGGER dataset_exports_no_update
BEFORE UPDATE ON dataset_exports BEGIN
    SELECT RAISE(ABORT, 'dataset_exports is append-only');
END;
"""


def statements(sql: str) -> List[str]:
    """Split a SQL script into complete statements (trigger bodies included)."""
    out: List[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        if not buf and (not line.strip() or line.strip().startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement: {buf!r}")
    return out


Migration = Callable[[sqlite3.Connection], None]


def _sql_step(sql: str) -> Migration:
    stmts = statements(sql)

    def run(db: sqlite3.Connection) -> None:
        for s in stmts:
            db.execute(s)
    return run


# version n -> n + 1
MIGRATIONS: Dict[int, Migration] = {
    2: _sql_step(_V2_TO_V3),
}


def read_version(db: sqlite3.Connection) -> Optional[int]:
    """``None`` for an empty database."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_meta'").fetchone():
        return None
    row = db.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row is not None else None


def pending(current: Optional[int], target: int) -> List[int]:
    start = MIN_MIGRATABLE_VERSION if current is None else current
    return list(range(start, target))


def migrate(db: sqlite3.Connection, target: int,
            migrations: Optional[Dict[int, Migration]] = None) -> List[int]:
    """Create or upgrade the schema to ``target`` atomically.

    Returns the versions that were migrated from (empty when up to date).
    Raises ``ValueError`` for an unsupported version; the caller maps it to
    ``SchemaMismatch``. ``db`` must be in autocommit mode (``isolation_level=None``).
    """
    migrations = MIGRATIONS if migrations is None else migrations
    current = read_version(db)
    if current == target:
        return []
    db.execute("BEGIN IMMEDIATE")
    try:
        current = read_version(db)  # another process may have migrated meanwhile
        if current is None:
            for s in statements(BASELINE_V2):
                db.execute(s)
            db.execute("INSERT OR REPLACE INTO store_meta(key, value) VALUES ('schema_version', ?)",
                       (str(MIN_MIGRATABLE_VERSION),))
            current = MIN_MIGRATABLE_VERSION
        if current > target:
            raise ValueError(f"store schema {current} is newer than this code (supports {target}); "
                             f"upgrade Pinny")
        if current < MIN_MIGRATABLE_VERSION:
            raise ValueError(f"store schema {current} is a pre-contract prototype (PDF points) and "
                             f"cannot be migrated; move it aside")
        done = []
        for v in range(current, target):
            step = migrations.get(v)
            if step is None:
                raise ValueError(f"no migration from store schema {v} to {v + 1}")
            step(db)
            db.execute("UPDATE store_meta SET value=? WHERE key='schema_version'", (str(v + 1),))
            done.append(v)
        db.execute("COMMIT")
        return done
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
