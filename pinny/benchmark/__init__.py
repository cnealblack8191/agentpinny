"""Phase 2 benchmark and promotion gate (docs/phase2-contracts.md P9).

``python -m pinny.benchmark run`` scores each scan mode on a dataset's test
split with the stdlib evaluator in ``evaluation/``. ``promote-report`` turns
that summary into a ``pinny.promotion`` v1 report. See
``docs/phase2-evaluation.md``.
"""

from .aggregate import aggregate_counts, ratio
from .gate import PROMOTION_FORMAT, build_promotion_report
from .run import BENCHMARK_FORMAT, MODES, TOLERANCE_PX, run_benchmark

__all__ = [
    "BENCHMARK_FORMAT",
    "MODES",
    "PROMOTION_FORMAT",
    "TOLERANCE_PX",
    "aggregate_counts",
    "build_promotion_report",
    "ratio",
    "run_benchmark",
]
