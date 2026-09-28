"""Progress and log of a long job (docs/training-site.md section 4).

A training task reports through a ``Progress``. It writes two files in the
job's own directory, derived from the data directory and the job id::

    $PINNY_DATA_DIR/jobs/<job_id>/progress.json   {"fraction": 0.42}
    $PINNY_DATA_DIR/jobs/<job_id>/log.txt         one line per message

The worker reads them at each heartbeat and copies them into the job
record (``JobQueue.set_progress``), so the web tier polls one table. The
log holds only messages our own code writes for the admin (epochs, losses,
counts); tracebacks and other internal detail go to the child's stderr.
Server paths are replaced by ``$PINNY_DATA_DIR`` before they are written.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_LOG_BYTES = 1024 * 1024  # a job's log stops growing here
TAIL_CHARS = 4000


class JobCancelled(Exception):
    """Raised inside a task when its job was cancelled (in-process runs)."""

    code = "cancelled"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("The job was cancelled.")


def job_dir(data_dir: os.PathLike, job_id: str) -> Path:
    """The job's own directory. ``job_id`` must be a lower-case UUID."""
    if not isinstance(job_id, str) or not _UUID_RE.match(job_id):
        raise ValueError("bad job id")
    return Path(data_dir) / "jobs" / job_id


class Progress:
    """What a task calls to report progress. ``cancelled`` is checked on every
    call; when it returns True the task stops with ``JobCancelled``. (In a
    sandboxed child the worker kills the process instead.)"""

    def __init__(self, directory: Optional[Path], *, data_dir: Optional[os.PathLike] = None,
                 cancelled: Callable[[], bool] = lambda: False) -> None:
        self.dir = directory
        self._cancelled = cancelled
        self._hide = [str(Path(data_dir).resolve()), str(data_dir)] if data_dir is not None else []
        self._hide = sorted({h for h in self._hide if h and h != "."}, key=len, reverse=True)
        self._log_bytes = 0
        self.fraction = 0.0
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)
            log = self.dir / "log.txt"
            self._log_bytes = log.stat().st_size if log.exists() else 0

    def check(self) -> None:
        if self._cancelled():
            raise JobCancelled()

    def update(self, fraction: Optional[float] = None, message: Optional[str] = None) -> None:
        self.check()
        if fraction is not None:
            self.fraction = min(1.0, max(self.fraction, float(fraction)))  # never goes backwards
            self._write_fraction()
        if message:
            self.log(message)

    def log(self, message: str) -> None:
        self.check()
        if self.dir is None:
            return
        for h in self._hide:
            message = message.replace(h, "$PINNY_DATA_DIR")
        line = time.strftime("%H:%M:%S ", time.gmtime()) + message.replace("\n", " ").strip() + "\n"
        data = line.encode("utf-8", "replace")
        if self._log_bytes + len(data) > MAX_LOG_BYTES:
            return
        with open(self.dir / "log.txt", "ab") as f:
            f.write(data)
        self._log_bytes += len(data)

    def _write_fraction(self) -> None:
        if self.dir is None:
            return
        tmp = self.dir / "progress.json.tmp"
        tmp.write_text(json.dumps({"fraction": round(self.fraction, 4)}))
        os.replace(tmp, self.dir / "progress.json")


def read(data_dir: os.PathLike, job_id: str) -> Tuple[Optional[float], Optional[str]]:
    """``(fraction, log tail)`` from a job's directory; ``None`` for what is missing."""
    try:
        d = job_dir(data_dir, job_id)
    except ValueError:
        return None, None
    fraction = None
    try:
        fraction = float(json.loads((d / "progress.json").read_text())["fraction"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    tail = None
    try:
        with open(d / "log.txt", "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_CHARS * 2))
            text = f.read().decode("utf-8", "replace")
        tail = text[-TAIL_CHARS:]
        if size > TAIL_CHARS and "\n" in tail:
            tail = tail[tail.index("\n") + 1:]  # start on a whole line
    except OSError:
        pass
    return fraction, tail
