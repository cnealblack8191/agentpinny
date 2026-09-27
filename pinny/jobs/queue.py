"""SQLite job queue (docs/training-site.md section 4).

States: ``queued`` -> ``running`` -> ``done`` | ``failed`` | ``cancelled``.
A worker claims the highest-priority, oldest queued job of its pool and
heartbeats while it runs. A running job whose heartbeat goes stale lost its
worker (crash, kill, restart): ``recover`` puts it back in the queue, or
fails it with ``worker_lost`` once it has used its attempts.

Payloads are ids and small settings only, never file paths from a client.
One connection per thread; SQLite coordinates processes.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .limits import LIMITS, POOLS, STALE_AFTER_S

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE = (QUEUED, RUNNING)

# Priorities: lower runs first. Interactive requests go ahead of batch pages.
PRIORITY_INTERACTIVE = 0
PRIORITY_BATCH = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    pool          TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL,
    dedupe_key    TEXT,
    status        TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','cancelled')),
    result        TEXT,
    error_code    TEXT,
    error_message TEXT,
    error_status  INTEGER,
    log_tail      TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL,
    worker_id     TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    requested_by  TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    heartbeat_at  REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(pool, status, priority, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued','running');
"""


class JobError(Exception):
    """A job ended without a result. ``code`` is stable; ``message`` is safe
    to show a user (internal detail stays in the job's log)."""

    def __init__(self, code: str, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    pool: str
    priority: int
    payload: Dict[str, Any]
    status: str
    result: Optional[Any]
    error_code: Optional[str]
    error_message: Optional[str]
    error_status: Optional[int]
    log_tail: Optional[str]
    attempts: int
    max_attempts: int
    worker_id: Optional[str]
    cancel_requested: bool
    requested_by: Optional[str]
    created_at: float
    started_at: Optional[float]
    heartbeat_at: Optional[float]
    finished_at: Optional[float]

    @property
    def finished(self) -> bool:
        return self.status in (DONE, FAILED, CANCELLED)

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "job_id", "kind", "pool", "status", "error_code", "error_message", "attempts",
            "requested_by", "created_at", "started_at", "finished_at")}


def _iso(t: Optional[float]) -> Optional[str]:
    if t is None:
        return None
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


class JobQueue:
    def __init__(self, data_dir: os.PathLike, clock=time.time) -> None:
        self.path = Path(data_dir) / "jobs.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._clock = clock
        self._db().executescript(_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            self._local.db = db
        return db

    def _tx(self):
        db = self._db()

        class _Tx:
            def __enter__(self_):
                db.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_, exc_type, exc, tb):
                db.execute("COMMIT" if exc_type is None else "ROLLBACK")
                return False
        return _Tx()

    # ---------------------------------------------------------------- submit
    def submit(self, kind: str, payload: Dict[str, Any], *, priority: int = PRIORITY_INTERACTIVE,
               dedupe_key: Optional[str] = None, requested_by: Optional[str] = None) -> str:
        """Queue a job; returns its id. With ``dedupe_key``, an identical job
        already queued or running is reused instead of a second one."""
        if kind not in LIMITS:
            raise ValueError(f"unknown job kind {kind!r}")
        body = json.dumps(payload, sort_keys=True)
        with self._tx() as db:
            if dedupe_key is not None:
                row = db.execute("SELECT job_id FROM jobs WHERE dedupe_key=? AND status IN ('queued','running')",
                                 (dedupe_key,)).fetchone()
                if row is not None:
                    return row["job_id"]
            job_id = str(uuid.uuid4())
            db.execute("INSERT INTO jobs(job_id, kind, pool, priority, payload, dedupe_key, status,"
                       " max_attempts, requested_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (job_id, kind, POOLS[kind], int(priority), body, dedupe_key, QUEUED,
                        LIMITS[kind].max_attempts, requested_by, self._clock()))
        return job_id

    def get(self, job_id: str) -> Job:
        r = self._db().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if r is None:
            raise KeyError(job_id)
        return self._job(r)

    def list(self, status: Optional[str] = None, limit: int = 100) -> List[Job]:
        q, args = "SELECT * FROM jobs", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        return [self._job(r) for r in self._db().execute(q + " ORDER BY created_at DESC LIMIT ?",
                                                        (*args, int(limit)))]

    def wait(self, job_id: str, timeout: float) -> Any:
        """Block until the job finishes; its result, or ``JobError``."""
        deadline = self._clock() + timeout
        delay = 0.01
        while True:
            job = self.get(job_id)
            if job.status == DONE:
                return job.result
            if job.status == FAILED:
                raise JobError(job.error_code or "job_failed", job.error_message or "The job failed.",
                               job.error_status or 500)
            if job.status == CANCELLED:
                raise JobError("cancelled", "The job was cancelled.", 409)
            if self._clock() >= deadline:
                raise JobError("job_wait_timeout", "The server is busy; try again in a minute.", 503)
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)

    def run(self, kind: str, payload: Dict[str, Any], *, timeout: Optional[float] = None, **kw) -> Any:
        """Submit and wait. The default timeout covers the job's wall-clock
        limit plus time spent queued."""
        job_id = self.submit(kind, payload, **kw)
        return self.wait(job_id, timeout if timeout is not None else LIMITS[kind].wall_s * 2 + 30)

    # ---------------------------------------------------------------- worker
    def claim(self, pool: str, worker_id: str) -> Optional[Job]:
        with self._tx() as db:
            r = db.execute("SELECT job_id FROM jobs WHERE pool=? AND status='queued'"
                           " ORDER BY priority, created_at LIMIT 1", (pool,)).fetchone()
            if r is None:
                return None
            now = self._clock()
            db.execute("UPDATE jobs SET status='running', worker_id=?, attempts=attempts+1, started_at=?,"
                       " heartbeat_at=? WHERE job_id=?", (worker_id, now, now, r["job_id"]))
        return self.get(r["job_id"])

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Refresh the heartbeat. Returns ``False`` when the job should stop:
        cancel requested, or it is no longer this worker's."""
        db = self._db()
        db.execute("UPDATE jobs SET heartbeat_at=? WHERE job_id=? AND worker_id=? AND status='running'",
                   (self._clock(), job_id, worker_id))
        r = db.execute("SELECT status, worker_id, cancel_requested FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(r and r["status"] == RUNNING and r["worker_id"] == worker_id and not r["cancel_requested"])

    def finish(self, job_id: str, worker_id: str, result: Any, log_tail: str = "") -> None:
        self._end(job_id, worker_id, DONE, result=result, log_tail=log_tail)

    def fail(self, job_id: str, worker_id: str, code: str, message: str, status: int = 500,
             log_tail: str = "") -> None:
        self._end(job_id, worker_id, FAILED, code=code, message=message, http_status=status, log_tail=log_tail)

    def _end(self, job_id, worker_id, status, *, result=None, code=None, message=None, http_status=None,
             log_tail="") -> None:
        with self._tx() as db:
            r = db.execute("SELECT status, worker_id, cancel_requested FROM jobs WHERE job_id=?",
                           (job_id,)).fetchone()
            if r is None or r["status"] != RUNNING or r["worker_id"] != worker_id:
                return  # recovered and taken over by another worker meanwhile
            if r["cancel_requested"] and status != DONE:
                status, code, message, http_status = CANCELLED, "cancelled", "The job was cancelled.", 409
            db.execute("UPDATE jobs SET status=?, result=?, error_code=?, error_message=?, error_status=?,"
                       " log_tail=?, finished_at=? WHERE job_id=?",
                       (status, json.dumps(result) if status == DONE else None, code, message, http_status,
                        (log_tail or "")[-4000:], self._clock(), job_id))

    # ---------------------------------------------------- cancel and recover
    def cancel(self, job_id: str) -> Job:
        """Queued: cancelled now. Running: the worker kills it at its next
        heartbeat. Finished: unchanged."""
        with self._tx() as db:
            db.execute("UPDATE jobs SET status='cancelled', error_code='cancelled',"
                       " error_message='The job was cancelled.', finished_at=? WHERE job_id=? AND status='queued'",
                       (self._clock(), job_id))
            db.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=? AND status='running'", (job_id,))
        return self.get(job_id)

    def recover(self, stale_after: float = STALE_AFTER_S) -> Dict[str, int]:
        """Requeue running jobs whose worker stopped heartbeating, or fail
        them once they have used their attempts."""
        cutoff = self._clock() - stale_after
        out = {"requeued": 0, "failed": 0}
        with self._tx() as db:
            rows = db.execute("SELECT job_id, attempts, max_attempts, cancel_requested FROM jobs"
                              " WHERE status='running' AND heartbeat_at < ?", (cutoff,)).fetchall()
            for r in rows:
                if r["cancel_requested"]:
                    db.execute("UPDATE jobs SET status='cancelled', error_code='cancelled', finished_at=?"
                               " WHERE job_id=?", (self._clock(), r["job_id"]))
                elif r["attempts"] < r["max_attempts"]:
                    db.execute("UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL,"
                               " heartbeat_at=NULL WHERE job_id=?", (r["job_id"],))
                    out["requeued"] += 1
                else:
                    db.execute("UPDATE jobs SET status='failed', error_code='worker_lost',"
                               " error_message='The job stopped unexpectedly; try again.', finished_at=?"
                               " WHERE job_id=?", (self._clock(), r["job_id"]))
                    out["failed"] += 1
        return out

    def prune(self, older_than_days: float = 7) -> int:
        """Delete finished jobs older than the cutoff."""
        cutoff = self._clock() - older_than_days * 86400
        cur = self._db().execute("DELETE FROM jobs WHERE status IN ('done','failed','cancelled')"
                                 " AND finished_at < ?", (cutoff,))
        return cur.rowcount

    @staticmethod
    def _job(r: sqlite3.Row) -> Job:
        return Job(job_id=r["job_id"], kind=r["kind"], pool=r["pool"], priority=r["priority"],
                   payload=json.loads(r["payload"]), status=r["status"],
                   result=json.loads(r["result"]) if r["result"] is not None else None,
                   error_code=r["error_code"], error_message=r["error_message"],
                   error_status=r["error_status"], log_tail=r["log_tail"],
                   attempts=r["attempts"], max_attempts=r["max_attempts"], worker_id=r["worker_id"],
                   cancel_requested=bool(r["cancel_requested"]), requested_by=r["requested_by"],
                   created_at=r["created_at"], started_at=r["started_at"], heartbeat_at=r["heartbeat_at"],
                   finished_at=r["finished_at"])

