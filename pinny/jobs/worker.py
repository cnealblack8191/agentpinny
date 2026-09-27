"""Job workers (docs/training-site.md section 4).

A ``Worker`` claims jobs from one pool and runs each in a fresh sandboxed
child (``pinny.jobs.child``), heartbeating while it runs, killing it at its
wall-clock limit or when the job is cancelled. Run workers as their own
service in production::

    python -m pinny.jobs.worker --pool interactive
    python -m pinny.jobs.worker --pool scan

or let the web process start them in threads (``PINNY_JOBS=sandbox``);
either way each job still runs in its own locked-down child process.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .limits import HEARTBEAT_S, LIMITS
from .queue import Job, JobQueue

_log = logging.getLogger("pinny.jobs")

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
POOL_NAMES = ("interactive", "scan")


def child_env() -> Dict[str, str]:
    """A minimal environment: no secrets, one thread per numeric library."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
           "PYTHONPATH": _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "OPENCV_FOR_THREADS_NUM": "1"}
    return env


class Worker:
    def __init__(self, queue: JobQueue, pool: str, data_dir: os.PathLike, *,
                 require_isolation: bool = False, python: str = sys.executable,
                 worker_id: Optional[str] = None) -> None:
        if pool not in POOL_NAMES:
            raise ValueError(f"pool must be one of {POOL_NAMES}")
        self.queue = queue
        self.pool = pool
        self.data_dir = str(Path(data_dir).resolve())
        self.require_isolation = require_isolation
        self.python = python
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{pool}:{uuid.uuid4().hex[:6]}"
        self.child: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------ run
    def run_once(self) -> bool:
        """Claim and run one job. False when the pool is empty."""
        job = self.queue.claim(self.pool, self.worker_id)
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_job(self, job: Job) -> None:
        lim = LIMITS[job.kind]
        request = json.dumps({"kind": job.kind, "payload": job.payload, "data_dir": self.data_dir,
                              "require_isolation": self.require_isolation,
                              "parent_pid": os.getpid()}).encode()
        started = time.monotonic()
        try:
            proc = subprocess.Popen([self.python, "-m", "pinny.jobs.child"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.data_dir,
                                    env=child_env(), start_new_session=True, close_fds=True)
        except OSError as exc:
            _log.exception("could not start a job child")
            self.queue.fail(job.job_id, self.worker_id, "sandbox_unavailable",
                            "The scanning sandbox could not start.", 500, log_tail=str(exc))
            return
        self.child = proc
        stop_reason = None
        out, err = b"", b""
        pending_input: Optional[bytes] = request
        while True:
            try:
                out, err = proc.communicate(pending_input, timeout=HEARTBEAT_S)
                break
            except subprocess.TimeoutExpired:
                pending_input = None  # already written; communicate keeps its state
                if time.monotonic() - started > lim.wall_s:
                    stop_reason = "timeout"
                elif not self.queue.heartbeat(job.job_id, self.worker_id):
                    stop_reason = "cancelled"
                if stop_reason:
                    self._kill(proc)
                    out, err = proc.communicate()
                    break
        self.child = None
        log_tail = err.decode("utf-8", "replace")[-4000:]
        if stop_reason == "timeout":
            self.queue.fail(job.job_id, self.worker_id, "timeout",
                            f"The job took longer than {lim.wall_s} s and was stopped.", 504, log_tail)
            return
        if stop_reason == "cancelled":
            self.queue.fail(job.job_id, self.worker_id, "cancelled", "The job was cancelled.", 409, log_tail)
            return
        answer = _last_json(out)
        if answer is None:
            code = proc.returncode
            if code is not None and code < 0 and -code in (signal.SIGXCPU, signal.SIGKILL):
                self.queue.fail(job.job_id, self.worker_id, "cpu_limit",
                                "The job used more processor time than allowed and was stopped.", 422, log_tail)
            else:
                _log.error("job %s child exited %s without an answer", job.job_id, code)
                self.queue.fail(job.job_id, self.worker_id, "job_crashed",
                                "The job stopped unexpectedly.", 500, log_tail)
            return
        if answer.get("ok"):
            self.queue.finish(job.job_id, self.worker_id, answer.get("result"), log_tail)
        else:
            self.queue.fail(job.job_id, self.worker_id, str(answer.get("code") or "job_failed"),
                            str(answer.get("message") or "The job failed."),
                            int(answer.get("status") or 500), log_tail)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def serve(self, stop: threading.Event, idle_s: float = 0.2) -> None:
        last_recover, last_prune = 0.0, 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now - last_recover > 10:
                try:
                    self.queue.recover()
                except Exception:  # noqa: BLE001
                    _log.exception("job recovery failed")
                last_recover = now
            if now - last_prune > 3600:
                try:
                    self.queue.prune(older_than_days=7)
                except Exception:  # noqa: BLE001
                    _log.exception("job pruning failed")
                last_prune = now
            try:
                worked = self.run_once()
            except Exception:  # noqa: BLE001 - keep serving
                _log.exception("worker %s failed a job", self.worker_id)
                worked = False
            if not worked:
                stop.wait(idle_s)

    def kill_child(self) -> None:
        if self.child is not None:
            self._kill(self.child)


def _last_json(out: bytes) -> Optional[dict]:
    for line in reversed(out.decode("utf-8", "replace").strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


class WorkerPool:
    """Worker threads inside another process (the web process with
    ``PINNY_JOBS=sandbox``, and tests). Each job still runs in a child."""

    def __init__(self, queue: JobQueue, data_dir: os.PathLike, *, sizes: Optional[Dict[str, int]] = None,
                 require_isolation: bool = False) -> None:
        self.stop_event = threading.Event()
        self.workers: List[Worker] = []
        self.threads: List[threading.Thread] = []
        for pool, n in (sizes or {"interactive": 1, "scan": 1}).items():
            for _ in range(n):
                self.workers.append(Worker(queue, pool, data_dir, require_isolation=require_isolation))

    def start(self) -> "WorkerPool":
        for w in self.workers:
            t = threading.Thread(target=w.serve, args=(self.stop_event,), name=f"pinny-{w.pool}", daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self, timeout: float = 10) -> None:
        self.stop_event.set()
        for w in self.workers:
            w.kill_child()  # a killed job is recovered by the next worker
        for t in self.threads:
            t.join(timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pinny.jobs.worker")
    ap.add_argument("--pool", choices=POOL_NAMES, required=True)
    ap.add_argument("--data-dir", default=os.environ.get("PINNY_DATA_DIR"))
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--allow-network", action="store_true",
                    help="development only: run jobs even if the child cannot be cut off from the network")
    args = ap.parse_args(argv)
    if not args.data_dir:
        print("Set PINNY_DATA_DIR or pass --data-dir.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    queue = JobQueue(args.data_dir)
    pool = WorkerPool(queue, args.data_dir, sizes={args.pool: args.concurrency},
                      require_isolation=not args.allow_network).start()

    def _stop(signum, frame):
        pool.stop_event.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _log.info("worker pool %s x%d on %s", args.pool, args.concurrency, args.data_dir)
    while not pool.stop_event.wait(1):
        pass
    pool.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
