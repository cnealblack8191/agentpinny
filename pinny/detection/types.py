"""Detector-agnostic data types for Pinny's receptacle detection.

Coordinate convention (all public values):

* Coordinates are in pixels of the *canonical page raster* passed to the
  detector, never of any preprocessed, cropped or rotated intermediate.
* Origin is the top-left corner of the raster; ``x`` grows right, ``y`` grows
  down. Pixel ``(i, j)`` covers the continuous area ``[i, i+1) x [j, j+1)``.
* A bounding box is ``(x, y, width, height)`` in integer pixels; it covers
  columns ``x .. x+width-1`` and rows ``y .. y+height-1``.
* A center is the continuous box center ``(x + width / 2, y + height / 2)``.
* ``rotation`` is the clockwise quarter-turn (0, 90, 180 or 270 degrees, in
  the y-down raster) applied to the template to produce the match.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

ALLOWED_ROTATIONS: Tuple[int, ...] = (0, 90, 180, 270)

#: Initial default matching-score threshold. Chosen for clean vector-rendered
#: drawings where an exact symbol scores ~1.0 and unrelated line work rarely
#: exceeds ~0.6. It is a starting point to tune against real drawings, not a
#: calibrated operating point.
DEFAULT_SCORE_THRESHOLD = 0.80

#: Bounds on a template scale factor (see ``ScanSettings.scales``).
MIN_SCALE = 0.25
MAX_SCALE = 4.0

#: Default ratio between neighbouring scales in :func:`scale_range`. On
#: synthetic line-art symbols a 5% size mismatch still scores above the
#: default threshold while 10% does not, so neighbouring steps of about 4%
#: keep every size within ~2% of a searched scale.
DEFAULT_SCALE_STEP = 1.04


class DetectionError(ValueError):
    """Invalid input or an unusable result. ``code`` is a stable identifier;
    the message says what to change."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"DetectionError({self.code!r}, {str(self)!r})"


class DetectionTimeout(DetectionError):
    """The scan exceeded ``ScanSettings.max_runtime_seconds``."""

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


@dataclass(frozen=True)
class BoundingBox:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
                raise DetectionError(
                    "invalid_box",
                    f"BoundingBox.{name} must be an integer pixel value, got {value!r}.",
                )
            object.__setattr__(self, name, int(value))
        if self.width <= 0 or self.height <= 0:
            raise DetectionError(
                "invalid_box",
                f"BoundingBox width and height must be positive, got "
                f"{self.width}x{self.height}.",
            )

    @property
    def x2(self) -> int:
        """Exclusive right edge."""
        return self.x + self.width

    @property
    def y2(self) -> int:
        """Exclusive bottom edge."""
        return self.y + self.height

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class Template:
    """A receptacle symbol image, usually cropped from the page by the user.

    ``image`` is a 2-D (grayscale) or 3-D (H, W, 3|4) array with the same
    channel layout as the page raster. ``source_box`` records where it was
    cropped from, if anywhere; detectors do not exclude that location, since
    it is itself a real receptacle.
    """

    image: np.ndarray
    source_box: Optional[BoundingBox] = None

    @classmethod
    def from_page_crop(cls, page: np.ndarray, box: BoundingBox) -> "Template":
        if not isinstance(page, np.ndarray) or page.ndim not in (2, 3):
            raise DetectionError(
                "invalid_page",
                "Page raster must be a 2-D or 3-D numpy array to crop a template from.",
            )
        if not isinstance(box, BoundingBox):
            raise DetectionError("invalid_crop", "Template crop must be a BoundingBox.")
        page_h, page_w = page.shape[:2]
        if box.x < 0 or box.y < 0 or box.x2 > page_w or box.y2 > page_h:
            raise DetectionError(
                "invalid_crop",
                f"Template crop {box.to_dict()} extends outside the {page_w}x{page_h} "
                "page raster. Select a region fully inside the page.",
            )
        return cls(image=page[box.y : box.y2, box.x : box.x2].copy(), source_box=box)


@dataclass(frozen=True)
class ScanSettings:
    """Settings for one scan of one page for one symbol type.

    Detector implementations may ignore fields that do not apply to them but
    must honour ``threshold``, ``rotations``, ``search_region`` and the
    candidate/runtime bounds.
    """

    #: Minimum matching score to report. For the OpenCV detector this is a
    #: normalized cross-correlation in [-1, 1]; it is *not* a probability.
    threshold: float = DEFAULT_SCORE_THRESHOLD
    rotations: Tuple[int, ...] = ALLOWED_ROTATIONS
    #: Template scale factors to search: 1.0 is the template as cropped, 1.1
    #: finds symbols drawn 10% larger. Each scale costs a full pass per
    #: rotation. Build a range with :func:`scale_range`.
    scales: Tuple[float, ...] = (1.0,)
    #: Restrict the search to this page-space region; results stay in page
    #: coordinates. ``None`` searches the whole page.
    search_region: Optional[BoundingBox] = None

    # Duplicate suppression (applied across all rotations together).
    #: Two candidates whose boxes overlap by more than this IoU are duplicates.
    nms_iou_threshold: float = 0.5
    #: Two candidates are also duplicates if their centers are closer than this
    #: fraction of the smaller of the two boxes' short sides. Catches the same
    #: symbol matched at two rotations whose boxes differ in shape.
    duplicate_center_ratio: float = 0.5

    # Resource bounds.
    max_candidates: int = 500
    #: Applies to each (scale, rotation) pass separately.
    max_candidates_per_rotation: int = 2000
    max_page_pixels: int = 60_000_000
    min_template_side: int = 8
    max_template_side: int = 1024
    #: Reject templates whose grayscale standard deviation (0-255 scale) is
    #: below this: they are blank or near-uniform and match anything flat.
    min_template_stddev: float = 4.0
    #: Checked between stages; a single OpenCV call is not interrupted.
    max_runtime_seconds: float = 60.0

    def validate(self) -> None:
        def fail(message: str) -> None:
            raise DetectionError("invalid_settings", message)

        if not _finite_number(self.threshold) or not (-1.0 <= self.threshold <= 1.0):
            fail(f"threshold must be a number in [-1, 1], got {self.threshold!r}.")
        if not isinstance(self.rotations, (tuple, list)) or not self.rotations:
            fail("rotations must be a non-empty sequence drawn from 0, 90, 180, 270.")
        for r in self.rotations:
            if isinstance(r, bool) or r not in ALLOWED_ROTATIONS:
                fail(
                    f"Unsupported rotation {r!r}. Only quarter turns "
                    f"{ALLOWED_ROTATIONS} are supported; arbitrary angles are not."
                )
        if len(set(self.rotations)) != len(self.rotations):
            fail(f"rotations contains duplicates: {tuple(self.rotations)}.")
        if not isinstance(self.scales, (tuple, list)) or not self.scales:
            fail("scales must be a non-empty sequence of template scale factors.")
        for s in self.scales:
            if not _finite_number(s) or not (MIN_SCALE <= s <= MAX_SCALE):
                fail(f"Unsupported scale {s!r}. Scales must be numbers in [{MIN_SCALE}, {MAX_SCALE}].")
        if len(set(float(s) for s in self.scales)) != len(self.scales):
            fail(f"scales contains duplicates: {tuple(self.scales)}.")
        if self.search_region is not None and not isinstance(self.search_region, BoundingBox):
            fail("search_region must be a BoundingBox or None.")
        if not _finite_number(self.nms_iou_threshold) or not (0.0 <= self.nms_iou_threshold < 1.0):
            fail(f"nms_iou_threshold must be in [0, 1), got {self.nms_iou_threshold!r}.")
        if not _finite_number(self.duplicate_center_ratio) or not (
            0.0 <= self.duplicate_center_ratio <= 1.0
        ):
            fail(
                "duplicate_center_ratio must be in [0, 1], got "
                f"{self.duplicate_center_ratio!r}."
            )
        for name in (
            "max_candidates",
            "max_candidates_per_rotation",
            "max_page_pixels",
            "min_template_side",
            "max_template_side",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                fail(f"{name} must be a positive integer, got {value!r}.")
        if self.min_template_side > self.max_template_side:
            fail("min_template_side must not exceed max_template_side.")
        if not _finite_number(self.min_template_stddev) or self.min_template_stddev < 0:
            fail(f"min_template_stddev must be >= 0, got {self.min_template_stddev!r}.")
        if not _finite_number(self.max_runtime_seconds) or self.max_runtime_seconds <= 0:
            fail(f"max_runtime_seconds must be > 0, got {self.max_runtime_seconds!r}.")


@dataclass(frozen=True)
class Candidate:
    """One detected symbol instance, in canonical page coordinates."""

    #: Matching score (higher is better). Not a calibrated probability.
    score: float
    box: BoundingBox
    rotation: int
    #: Template scale factor that produced the match (1.0 = as cropped).
    scale: float = 1.0

    @property
    def center(self) -> Tuple[float, float]:
        return self.box.center

    def to_dict(self) -> dict:
        cx, cy = self.center
        return {
            "score": self.score,
            "box": self.box.to_dict(),
            "center": {"x": cx, "y": cy},
            "rotation": self.rotation,
            "scale": self.scale,
        }


@dataclass(frozen=True)
class DetectionResult:
    #: Sorted by descending score, then top-to-bottom, left-to-right.
    candidates: Tuple[Candidate, ...]
    detector: str
    threshold: float
    rotations_searched: Tuple[int, ...]
    #: True if any candidate cap was hit, so more matches may exist.
    truncated: bool = False
    elapsed_seconds: float = 0.0
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    scales_searched: Tuple[float, ...] = (1.0,)

    def to_dict(self) -> dict:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "detector": self.detector,
            "threshold": self.threshold,
            "rotations_searched": list(self.rotations_searched),
            "scales_searched": list(self.scales_searched),
            "truncated": self.truncated,
            "elapsed_seconds": self.elapsed_seconds,
            "warnings": list(self.warnings),
        }


def scale_range(
    low: float, high: float, step: float = DEFAULT_SCALE_STEP
) -> Tuple[float, ...]:
    """Geometric scale factors from ``low`` to ``high`` inclusive, always
    containing 1.0 when it is in range. Each side of 1.0 is split into equal
    ratios no larger than ``step`` (1.04 = at most 4% apart). Values are
    rounded to 4 decimals."""
    for name, value in (("low", low), ("high", high), ("step", step)):
        if not _finite_number(value):
            raise DetectionError("invalid_settings", f"scale_range {name} must be a finite number.")
    if not (MIN_SCALE <= low <= high <= MAX_SCALE):
        raise DetectionError(
            "invalid_settings",
            f"scale_range needs {MIN_SCALE} <= low <= high <= {MAX_SCALE}, got {low}, {high}.",
        )
    if not (1.001 <= step <= 2.0):
        raise DetectionError("invalid_settings", f"scale_range step must be in [1.001, 2], got {step}.")

    def spaced(a: float, b: float) -> list:
        # Equal ratios from a to b (a <= b), each at most `step`.
        n = max(1, math.ceil(math.log(b / a) / math.log(step) - 1e-9))
        return [a * (b / a) ** (i / n) for i in range(n + 1)]

    if high <= 1.0 or low >= 1.0:
        points = spaced(low, high) if low < high else [low]
    else:
        # Anchor at 1.0 so the template's own size is always searched exactly.
        points = spaced(low, 1.0) + spaced(1.0, high)
    return tuple(sorted({round(float(v), 4) for v in points}))


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float, np.integer, np.floating))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
