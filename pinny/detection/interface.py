"""The replaceable detector interface.

Callers depend only on :class:`Detector` and the types in ``types.py``; the
OpenCV template matcher is one implementation and can be swapped for another
(e.g. a learned detector) without changing call sites.
"""

from __future__ import annotations

from typing import Protocol, Union, runtime_checkable

import numpy as np

from .types import DetectionResult, ScanSettings, Template


@runtime_checkable
class Detector(Protocol):
    #: Stable identifier recorded in every ``DetectionResult``.
    name: str

    def detect(
        self,
        page: np.ndarray,
        template: Union[Template, np.ndarray],
        settings: ScanSettings = ...,
    ) -> DetectionResult:
        """Search one canonical page raster for one symbol type.

        Must return candidates in canonical page coordinates, sorted by
        descending score, with duplicates suppressed across rotations, and
        raise :class:`~.types.DetectionError` for invalid input.
        """
        ...
