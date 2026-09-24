"""Match-radius specification: absolute pixels, relative to box size, or both.

Precedence (documented in README "Matching rules"):

* ``--tolerance-px`` only: every prediction uses that radius.
* ``--tolerance-rel`` only: each prediction's radius is
  ``rel * min(box.width, box.height)`` of *its own* detection box. Every
  detection must then carry a ``box``; a detection without one rejects the run.
* both: a prediction **with** a box uses the relative radius; a prediction
  **without** a box falls back to the pixel radius. The pixel value is a
  fallback, not a cap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .inputs import InputError
from .matching import Point

Box = Tuple[float, float, float, float]  # x, y, width, height

PRECEDENCE = (
    "relative radius (rel x short side of the detection box) where a box exists; "
    "otherwise the pixel radius; a detection with neither rejects the run"
)


@dataclass(frozen=True)
class Tolerance:
    px: Optional[float] = None
    rel: Optional[float] = None

    def __post_init__(self) -> None:
        if self.px is None and self.rel is None:
            raise ValueError("a tolerance needs --tolerance-px and/or --tolerance-rel")
        for name, v in (("px", self.px), ("rel", self.rel)):
            if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
                raise ValueError(f"tolerance {name} must be finite and >= 0")

    @property
    def mode(self) -> str:
        if self.rel is None:
            return "px"
        return "rel" if self.px is None else "rel_with_px_fallback"

    def per_prediction(self, predictions: Sequence[Point], boxes: Mapping[str, Box]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        missing = []
        for p in predictions:
            box = boxes.get(p.id)
            if self.rel is not None and box is not None:
                out[p.id] = self.rel * min(box[2], box[3])
            elif self.px is not None:
                out[p.id] = float(self.px)
            else:
                missing.append(p.id)
        if missing:
            shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
            raise InputError(
                f"--tolerance-rel needs a detection box, but {len(missing)} detection(s) have none "
                f"({shown}). Add --tolerance-px as a fallback or export boxes"
            )
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tolerance_px": self.px,
            "tolerance_rel": self.rel,
            "tolerance_mode": self.mode,
            "tolerance_precedence": PRECEDENCE if self.rel is not None else "fixed pixel radius",
        }

    def describe(self) -> str:
        if self.mode == "px":
            return f"{self.px:g} canonical raster pixels"
        rel = f"{self.rel:g} x short side of each detection box"
        if self.mode == "rel":
            return rel
        return f"{rel}; {self.px:g} px for detections without a box"
