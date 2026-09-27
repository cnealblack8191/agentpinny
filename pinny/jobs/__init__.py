"""Jobs and sandbox (docs/training-site.md section 4).

Untrusted PDFs are parsed, rendered and scanned only in short-lived child
processes with memory, CPU-time, wall-clock and file limits and no network.
The web process queues jobs by id and waits for their results.

* ``queue``  SQLite job queue: submit, claim, heartbeat, cancel, recover.
* ``limits`` per-kind resource limits.
* ``child``  the sandboxed child entry point (``python -m pinny.jobs.child``).
* ``tasks``  what each job kind does, run inside the child.
* ``worker`` claims jobs and runs each in a child (``python -m pinny.jobs.worker``).
"""

from .queue import Job, JobError, JobQueue

__all__ = ["Job", "JobError", "JobQueue"]
