"""Site data: members, passwords, sign-in sessions, uploads, deleted
documents and the audit log (docs/training-site.md sections 1, 2, 6 and 7),
in ``site.sqlite3``.

Standard library only. One connection per thread (``threading.local``), so
the web tier's thread pool can use it directly.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ADMIN = "admin"
REVIEWER = "reviewer"
ROLES = (ADMIN, REVIEWER)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

# Sign-in (docs/training-site.md section 1).
SESSION_IDLE_S = 7 * 24 * 3600  # signed out after a week without use
SESSION_MAX_S = 30 * 24 * 3600  # and after 30 days regardless
SETUP_LINK_S = 72 * 3600  # a set-password link works once, for 3 days
LOCK_AFTER = 10  # wrong passwords in a row lock the account
LOCK_S = 15 * 60  # for 15 minutes
_TOUCH_S = 60  # last_seen is written at most once a minute per session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    email      TEXT PRIMARY KEY,
    role       TEXT NOT NULL CHECK (role IN ('admin','reviewer')),
    added_at   TEXT NOT NULL,
    added_by   TEXT
);
CREATE TABLE IF NOT EXISTS passwords (
    email        TEXT PRIMARY KEY,
    hash         TEXT NOT NULL,
    set_at       TEXT NOT NULL,
    failed       INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_email ON sessions(email);
CREATE TABLE IF NOT EXISTS setup_links (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    created_by TEXT
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


def _token_hash(token: str) -> str:
    """Only hashes of session and setup tokens are stored, so a copy of the
    database (a backup, say) cannot be used to sign in."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
                self._forget_credentials(db, email)
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

    # --------------------------------------------------------------- sign-in
    @staticmethod
    def _forget_credentials(db: sqlite3.Connection, email: str) -> None:
        for table in ("passwords", "sessions", "setup_links"):
            db.execute(f"DELETE FROM {table} WHERE email=?", (email,))

    def has_password(self, email: str) -> bool:
        return self._db().execute("SELECT 1 FROM passwords WHERE email=?", (email.lower(),)).fetchone() is not None

    def password_record(self, email: str) -> Optional[Dict[str, Any]]:
        r = self._db().execute("SELECT * FROM passwords WHERE email=?", (email.lower(),)).fetchone()
        return dict(r) if r else None

    def record_login_failure(self, email: str, now: float) -> None:
        with self._write_lock:
            db = self._db()
            db.execute("UPDATE passwords SET failed = failed + 1 WHERE email=?", (email,))
            r = db.execute("SELECT failed FROM passwords WHERE email=?", (email,)).fetchone()
            if r is not None and r["failed"] >= LOCK_AFTER:
                db.execute("UPDATE passwords SET failed = 0, locked_until = ? WHERE email=?", (now + LOCK_S, email))
                self._audit(db, None, "account_locked", email, {"minutes": LOCK_S // 60})

    def clear_login_failures(self, email: str) -> None:
        with self._write_lock:
            self._db().execute("UPDATE passwords SET failed = 0, locked_until = 0 WHERE email=?", (email,))

    def set_password(self, email: str, password_hash: str, *, actor: Optional[str],
                     keep_session: Optional[str] = None) -> None:
        """Replace the password and sign the member out everywhere (except,
        optionally, the session making the change)."""
        with self._write_lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO passwords(email, hash, set_at) VALUES (?,?,?) ON CONFLICT(email) "
                           "DO UPDATE SET hash=excluded.hash, set_at=excluded.set_at, failed=0, locked_until=0",
                           (email, password_hash, _now()))
                keep = _token_hash(keep_session) if keep_session else ""
                db.execute("DELETE FROM sessions WHERE email=? AND token_hash<>?", (email, keep))
                db.execute("DELETE FROM setup_links WHERE email=?", (email,))
                self._audit(db, actor, "password_set", email)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def issue_setup_link(self, email: str, *, actor: Optional[str], now: float,
                         reset: bool = False) -> Tuple[str, float]:
        """A one-time token that lets ``email`` choose a password. Replaces any
        earlier link. ``reset`` also removes the current password and signs
        the member out everywhere."""
        email = normalize_email(email)
        token = secrets.token_urlsafe(32)
        expires = now + SETUP_LINK_S
        with self._write_lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT 1 FROM members WHERE email=?", (email,)).fetchone() is None:
                    raise ValueError(f"{email} is not a member. Add them first.")
                if reset:
                    self._forget_credentials(db, email)
                else:
                    db.execute("DELETE FROM setup_links WHERE email=?", (email,))
                db.execute("INSERT INTO setup_links VALUES (?,?,?,?,?)",
                           (_token_hash(token), email, now, expires, actor))
                self._audit(db, actor, "password_reset" if reset else "setup_link_issued", email)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return token, expires

    def setup_link_email(self, token: str, now: float) -> Optional[str]:
        r = self._db().execute("SELECT email, expires_at FROM setup_links WHERE token_hash=?",
                               (_token_hash(token),)).fetchone()
        if r is None or r["expires_at"] < now or self.member(r["email"]) is None:
            return None
        return r["email"]

    def create_session(self, email: str, now: float) -> str:
        token = secrets.token_urlsafe(32)
        with self._write_lock:
            db = self._db()
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_token_hash(token), email, now, now))
            # Housekeeping: drop sessions that can no longer be used.
            db.execute("DELETE FROM sessions WHERE last_seen_at < ? OR created_at < ?",
                       (now - SESSION_IDLE_S, now - SESSION_MAX_S))
            db.execute("DELETE FROM setup_links WHERE expires_at < ?", (now,))
        return token

    def session_email(self, token: str, now: float) -> Optional[str]:
        h = _token_hash(token)
        r = self._db().execute("SELECT * FROM sessions WHERE token_hash=?", (h,)).fetchone()
        if r is None:
            return None
        if r["last_seen_at"] < now - SESSION_IDLE_S or r["created_at"] < now - SESSION_MAX_S:
            self.delete_session(token)
            return None
        if now - r["last_seen_at"] > _TOUCH_S:
            with self._write_lock:
                self._db().execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, h))
        return r["email"]

    def delete_session(self, token: str) -> None:
        with self._write_lock:
            self._db().execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    def sign_out_everyone(self, *, actor: Optional[str]) -> int:
        with self._write_lock:
            db = self._db()
            n = db.execute("DELETE FROM sessions").rowcount
            self._audit(db, actor, "all_sessions_ended", None, {"sessions": n})
        return n

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
