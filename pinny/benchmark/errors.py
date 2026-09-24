"""Benchmark errors (contracts section 7)."""

from __future__ import annotations

from pinny.errors import PinnyError


class BenchmarkError(PinnyError):
    """Invalid benchmark input or a run that cannot be scored."""
