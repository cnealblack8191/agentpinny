"""Run one pool's jobs in a thread of this process (``PINNY_JOBS=inprocess``).

Development and tests only: the development server has no workers, and
training jobs must still run so the training pages work. Jobs keep the
same records as under a worker (states, progress, log tail,
cancellation), but there is no sandbox and no resource limit, so
production refuses this mode (docs/training-site.md section 10).

Cancellation and the wall-clock limit are cooperative: the task stops at
its next progress report with ``JobCancelled``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import progress as _progress
from .limits import LIMITS
from .queue import Job, JobQueue

_log = logging.getLogger("pinny.jobs")

TICK_S = 0.5
RECOVER_EVERY_S = 10.0


class InProcessRunner:
    def __init__(self, queue: JobQueue, pool: str, data_dir: os.PathLike) -> None:
        self.queue = queue
        self.pool = pool
        self.data_dir = str(Path(data_dir).resolve())
        self.worker_id = f"inprocess:{os.getpid()}:{pool}:{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> "InProcessRunner":
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._serve, name=f"pinny-inprocess-{self.pool}",
                                                daemon=True)
                self._thread.start()
        return self

    def wake(self) -> None:
        self.start()
        self._wake.set()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _serve(self) -> None:
        last_recover = 0.0
        while not self._stop.is_set():
            # A job left running by a stopped server has a stale heartbeat:
            # requeue it, or fail it (training jobs) with a clear reason.
            if time.monotonic() - last_recover > RECOVER_EVERY_S:
                try:
                    self.queue.recover()
                except Exception:  # noqa: BLE001
                    _log.exception("job recovery failed")
                last_recover = time.monotonic()
            try:
                job = self.queue.claim(self.pool, self.worker_id)
            except Exception:  # noqa: BLE001
                _log.exception("could not claim a job")
                job = None
            if job is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self.run_job(job)
            except Exception:  # noqa: BLE001 - keep serving
                _log.exception("in-process job %s failed", job.job_id)

    def run_job(self, job: Job) -> None:
        lim = LIMITS[job.kind]
        cancel = threading.Event()
        out: dict = {}

        def target() -> None:
            from . import tasks
            try:
                directory = _progress.job_dir(self.data_dir, job.job_id)
                prog = _progress.Progress(directory, data_dir=self.data_dir, cancelled=cancel.is_set)
                out["result"] = tasks.run(job.kind, job.payload, self.data_dir, job_id=job.job_id,
                                          progress=prog)
            except BaseException as exc:  # noqa: BLE001 - reported below
                out["error"] = exc

        t = threading.Thread(target=target, name=f"pinny-job-{job.job_id[:8]}", daemon=True)
        started = time.monotonic()
        t.start()
        timed_out = False
        while t.is_alive():
            t.join(TICK_S)
            self._sync(job)
            if time.monotonic() - started > lim.wall_s:
                timed_out = True
                cancel.set()
            elif not self.queue.heartbeat(job.job_id, self.worker_id):
                cancel.set()
        self._sync(job)
        if timed_out:
            self.queue.fail(job.job_id, self.worker_id, "timeout",
                            f"The job took longer than {lim.wall_s} s and was stopped.", 504)
            return
        exc = out.get("error")
        if exc is None:
            self.queue.finish(job.job_id, self.worker_id, out.get("result"))
            return
        code = getattr(exc, "code", None)
        status = getattr(exc, "http_status", None) or getattr(exc, "status", None)
        if isinstance(code, str) and isinstance(status, int) and 400 <= status < 500:
            self.queue.fail(job.job_id, self.worker_id, code, str(getattr(exc, "message", None) or exc), status)
        else:
            _log.error("in-process job %s (%s) failed", job.job_id, job.kind, exc_info=exc)
            self.queue.fail(job.job_id, self.worker_id, "job_failed",
                            "The job failed because of an internal error.", 500)

    def _sync(self, job: Job) -> None:
        fraction, tail = _progress.read(self.data_dir, job.job_id)
        if fraction is not None or tail is not None:
            self.queue.set_progress(job.job_id, self.worker_id, fraction, tail)
