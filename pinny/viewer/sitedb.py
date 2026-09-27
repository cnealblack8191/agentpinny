"""Site data: members, uploads, deleted documents and the audit log
(docs/training-site.md sections 1, 2, 6 and 7), in ``site.sqlite3``.

Standard library only. One connection per thread (``threading.local``), so
the web tier's thread pool can use it directly.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

ADMIN = "admin"
REVIEWER = "reviewer"
ROLES = (ADMIN, REVIEWER)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    email      TEXT PRIMARY KEY,
    role       TEXT NOT NULL CHECK (role IN ('admin','reviewer')),
    added_at   TEXT NOT NULL,
    added_by   TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
    document_version TEXT PRIMARY KEY,
    uploaded_by      TEXT NOT NULL,
    uploaded_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_documents (
    document_version TEXT NOT NULL,
    document_id      TEXT,
    filename         TEXT,
    deleted_by       TEXT NOT NULL,
    deleted_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_email(email: Any) -> str:
    if not isinstance(email, str) or not _EMAIL_RE.match(email.strip()):
        raise ValueError("That is not an email address.")
    return email.strip().lower()


@dataclass(frozen=True)
class Member:
    email: str
    role: str
    added_at: str
    added_by: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"email": self.email, "role": self.role, "added_at": self.added_at,
                "added_by": self.added_by}


class SiteDB:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "site.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._write_lock:
            db = self._db()
            db.executescript(_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = FULL")
            self._local.db = db
        return db

    # --------------------------------------------------------------- members
    def member(self, email: str) -> Optional[Member]:
        r = self._db().execute("SELECT * FROM members WHERE email=?", (email.lower(),)).fetchone()
        return Member(**dict(r)) if r else None

    def members(self) -> List[Member]:
        return [Member(**dict(r)) for r in self._db().execute("SELECT * FROM members ORDER BY email")]

    def put_member(self, email: str, role: str, *, actor: Optional[str]) -> Member:
        email = normalize_email(email)
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}.")
        with self._write_lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute("SELECT role FROM members WHERE email=?", (email,)).fetchone()
                if cur is None:
                    db.execute("INSERT INTO members(email, role, added_at, added_by) VALUES (?,?,?,?)",
                               (email, role, _now(), actor))
                    self._audit(db, actor, "member_added", email, {"role": role})
                elif cur["role"] != role:
                    if cur["role"] == ADMIN and self._admin_count(db) <= 1:
                        raise ValueError("That is the last admin; add another admin first.")
                    db.execute("UPDATE members SET role=? WHERE email=?", (role, email))
                    self._audit(db, actor, "member_role_changed", email, {"from": cur["role"], "to": role})
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.member(email)

    def remove_member(self, email: str, *, actor: Optional[str]) -> bool:
        email = normalize_email(email)
        with self._write_lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute("SELECT role FROM members WHERE email=?", (email,)).fetchone()
                if cur is None:
                    db.execute("ROLLBACK")
                    return False
                if cur["role"] == ADMIN and self._admin_count(db) <= 1:
                    raise ValueError("That is the last admin; add another admin first.")
                db.execute("DELETE FROM members WHERE email=?", (email,))
                self._audit(db, actor, "member_removed", email, {"role": cur["role"]})
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        return True

    def ensure_admins(self, emails) -> None:
        """Add configured first admins if missing. Never demotes or removes."""
        for e in emails:
            if self.member(e) is None:
                self.put_member(e, ADMIN, actor="PINNY_ADMIN_EMAILS")

    @staticmethod
    def _admin_count(db: sqlite3.Connection) -> int:
        return db.execute("SELECT COUNT(*) FROM members WHERE role='admin'").fetchone()[0]

    # --------------------------------------------------------------- uploads
    def record_upload(self, document_version: str, email: str, filename: str) -> None:
        with self._write_lock:
            db = self._db()
            cur = db.execute("SELECT 1 FROM uploads WHERE document_version=?", (document_version,)).fetchone()
            if cur is None:
                db.execute("INSERT INTO uploads VALUES (?,?,?)", (document_version, email, _now()))
                self._audit(db, email, "document_uploaded", document_version, {"filename": filename})

    def uploaded_by(self, document_version: str) -> Optional[str]:
        r = self._db().execute("SELECT uploaded_by FROM uploads WHERE document_version=?",
                               (document_version,)).fetchone()
        return r["uploaded_by"] if r else None

    def record_deletion(self, document_version: str, document_id: Optional[str], filename: Optional[str],
                        email: str) -> None:
        with self._write_lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO deleted_documents VALUES (?,?,?,?,?)",
                           (document_version, document_id, filename, email, _now()))
                # A later re-upload of the same bytes starts a fresh ownership record.
                db.execute("DELETE FROM uploads WHERE document_version=?", (document_version,))
                self._audit(db, email, "document_deleted", document_version, {"filename": filename})
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    # ----------------------------------------------------------------- audit
    def audit(self, actor: Optional[str], action: str, target: Optional[str] = None,
              detail: Optional[Dict[str, Any]] = None) -> None:
        with self._write_lock:
            self._audit(self._db(), actor, action, target, detail)

    @staticmethod
    def _audit(db: sqlite3.Connection, actor, action, target, detail=None) -> None:
        db.execute("INSERT INTO audit(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                   (_now(), actor, action, target, json.dumps(detail or {}, sort_keys=True)))

    def audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._db().execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (int(limit),))
        return [{"seq": r["seq"], "at": r["at"], "actor": r["actor"], "action": r["action"],
                 "target": r["target"], "detail": json.loads(r["detail"])} for r in rows]
