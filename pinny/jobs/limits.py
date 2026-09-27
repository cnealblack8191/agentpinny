"""Per-kind job limits (docs/training-site.md sections 4 and 5).

Memory is the child's address-space limit (RLIMIT_AS), which is larger than
its resident size; the figures leave room for the page raster, the
detector's score maps and the libraries. They are sized for one EC2
instance with 8 GB of RAM running one interactive and one scan worker.
"""

from __future__ import annotations

from dataclasses import dataclass

MB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    memory_mb: int  # address space
    cpu_s: int  # CPU time
    wall_s: int  # wall clock, enforced by the worker
    file_mb: int = 1024  # largest file the child may write
    open_files: int = 64
    max_attempts: int = 2  # a job lost with its worker is retried this many times in total


# pool per kind: "interactive" jobs (uploads, page images) never wait behind scans.
POOLS = {
    "ingest": "interactive",
    "render_page": "interactive",
    "template": "interactive",
    "scan": "scan",
    "selftest": "interactive",
}

LIMITS = {
    "ingest": Limits(memory_mb=2048, cpu_s=120, wall_s=180),
    # A 100 MP page is ~300 MB as RGB, plus PDFium's own buffers and the PNG encoder.
    "render_page": Limits(memory_mb=3072, cpu_s=180, wall_s=240),
    "template": Limits(memory_mb=2048, cpu_s=60, wall_s=120),
    # Measured ~760 MB RSS for four rotations on an Arch D sheet; allow Arch E.
    "scan": Limits(memory_mb=4096, cpu_s=300, wall_s=360),
    "selftest": Limits(memory_mb=1024, cpu_s=30, wall_s=60),
}

HEARTBEAT_S = 3.0
# A running job whose heartbeat is older than this lost its worker.
STALE_AFTER_S = 30.0
