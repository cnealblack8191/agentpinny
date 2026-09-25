"""Internal detection record shared by the XObject and path matchers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class RawDetection:
    box: Tuple[float, float, float, float]  # canonical px (x0, y0, x1, y1), float
    score: float
    rotation: int
    mirrored: bool
    source: str
    #: Exact clockwise angle when it is not a quarter turn.
    angle: Optional[float] = None
    scale: float = 1.0
    coverage: Optional[float] = None

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0)
