"""Per-kind job limits (docs/training-site.md sections 4 and 5).

Memory is the child's address-space limit (RLIMIT_AS), which is larger than
its resident size; the figures leave room for the page raster, the
detector's score maps and the libraries. They are sized for one EC2
instance with 8 GB of RAM running one interactive, one scan and one train
worker. Training jobs (the ``train`` pool) run one at a time, admin-only,
with PyTorch's working set, several hours of CPU and two threads.
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
    threads: int = 1  # threads per numeric library (OMP, BLAS, torch) in the child


# pool per kind: "interactive" jobs (uploads, page images) never wait behind scans.
POOLS = {
    "ingest": "interactive",
    "render_page": "interactive",
    "template": "interactive",
    "scan": "scan",
    "selftest": "interactive",
    # Legend workflow (docs/legend-reader.md, docs/set-scanning.md).
    "read_legend": "interactive",
    "scan_set": "scan",
    # Training (docs/training-site.md section 4): admin jobs, one at a time.
    "build_dataset": "train",
    "train_verifier": "train",
    "train_detector": "train",
    "benchmark": "train",
}

TRAIN_KINDS = ("build_dataset", "train_verifier", "train_detector", "benchmark")

LIMITS = {
    "ingest": Limits(memory_mb=2048, cpu_s=120, wall_s=180),
    # A 100 MP page is ~300 MB as RGB, plus PDFium's own buffers and the PNG encoder.
    "render_page": Limits(memory_mb=3072, cpu_s=180, wall_s=240),
    "template": Limits(memory_mb=2048, cpu_s=60, wall_s=120),
    # Measured ~760 MB RSS for four rotations on an Arch D sheet; allow Arch E.
    "scan": Limits(memory_mb=4096, cpu_s=300, wall_s=360),
    "selftest": Limits(memory_mb=1024, cpu_s=30, wall_s=60),
    # Finding the legend reads only page text (0.75 s for a 91 MB, 42-page
    # set); then one page's drawing is read.
    "read_legend": Limits(memory_mb=2048, cpu_s=120, wall_s=120),
    # Every sheet, one at a time: a 42-page dense set took ~99 s and ~600 MB
    # resident (docs/set-scanning.md). Scanned sheets are rendered at 200 DPI
    # and template matched like a ``scan`` job, so the same address space.
    "scan_set": Limits(memory_mb=4096, cpu_s=1800, wall_s=1800),
    # Renders every labelled page once (up to 100 MP each) and writes crops and
    # grayscale pages; one page raster at a time.
    "build_dataset": Limits(memory_mb=3072, cpu_s=3600, wall_s=3600, open_files=256, max_attempts=1),
    # PyTorch on the CPU: ~1.5 GB resident for the verifier, more for detector
    # tiles; the address space also covers torch's own mappings. Two threads
    # on the 2-vCPU instance; CPU time counts both.
    "train_verifier": Limits(memory_mb=5120, cpu_s=28800, wall_s=14400, open_files=256, max_attempts=1,
                             threads=2),
    "train_detector": Limits(memory_mb=5120, cpu_s=28800, wall_s=14400, open_files=256, max_attempts=1,
                             threads=2),
    # Template matching plus the candidate model on every test page.
    "benchmark": Limits(memory_mb=5120, cpu_s=14400, wall_s=7200, open_files=256, max_attempts=1, threads=2),
}

HEARTBEAT_S = 3.0
# A running job whose heartbeat is older than this lost its worker.
STALE_AFTER_S = 30.0
