"""The replaceable OCR engine interface.

Callers depend on :class:`OcrEngine` and ``types.py`` only, so Tesseract can
be swapped for another engine without changing call sites.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

import numpy as np

from .types import OcrBox, OcrResult, OcrSettings


@runtime_checkable
class OcrEngine(Protocol):
    #: Stable identifier, also the value of ``$PINNY_OCR_ENGINE``.
    name: str
    #: SPDX licence identifier of the engine itself.
    license: str

    def is_available(self) -> bool:
        """Whether the engine can run on this machine. Never raises."""
        ...

    def recognize(
        self,
        page: np.ndarray,
        settings: OcrSettings = ...,
        region: Optional[OcrBox] = None,
    ) -> OcrResult:
        """Read text from one canonical page raster (or ``region`` of it).

        Must return word boxes in canonical page coordinates and raise
        :class:`~.types.OcrError` for invalid input or a failed run.
        """
        ...
