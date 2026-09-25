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
* ``mirrored`` (v1.1, additive) means the template was flipped horizontally
  (left-right) *before* that clockwise rotation was applied.
"""

from __future__ import annotations

import dataclasses
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
    max_candidates_per_rotation: int = 2000
    max_page_pixels: int = 120_000_000
    min_template_side: int = 8
    max_template_side: int = 1024
    #: Reject templates whose grayscale standard deviation (0-255 scale) is
    #: below this: they are blank or near-uniform and match anything flat.
    min_template_stddev: float = 4.0
    #: Checked before each orientation after the first and between page
    #: strips; a single OpenCV call is not interrupted.
    max_runtime_seconds: float = 60.0

    # Matching behaviour (v1.1).
    #: Standard deviation, in pixels, of the Gaussian blur applied identically
    #: to page and template before matching. Tolerates sub-pixel shifts, small
    #: scale differences and anti-aliasing. 0 disables the blur.
    blur_sigma: float = 1.0
    #: Also search the horizontally flipped template (flip applied before the
    #: clockwise rotation). Candidates found this way have ``mirrored=True``.
    include_mirrored: bool = False
    #: Use a half-resolution pre-pass and refine its peaks at full resolution.
    #: Only applies to orientations whose shorter template side is at least
    #: 24 px; results match the full-resolution search.
    coarse_to_fine: bool = True
    #: The coarse pre-pass keeps peaks scoring at least ``threshold -
    #: coarse_slack`` (at half resolution scores are lower).
    coarse_slack: float = 0.15
    #: If the coarse pre-pass finds more peaks than this for one orientation,
    #: that orientation falls back to the full-resolution search.
    max_coarse_peaks: int = 5000
    #: Worker threads for page strips (OpenCV releases the GIL). 0 = auto,
    #: ``min(4, os.cpu_count())``; 1 = run in the calling thread.
    num_threads: int = 0

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
        if not _finite_number(self.blur_sigma) or not (0.0 <= self.blur_sigma <= 10.0):
            fail(f"blur_sigma must be a number in [0, 10] (0 disables), got {self.blur_sigma!r}.")
        for name in ("include_mirrored", "coarse_to_fine"):
            if not isinstance(getattr(self, name), bool):
                fail(f"{name} must be True or False, got {getattr(self, name)!r}.")
        if not _finite_number(self.coarse_slack) or not (0.0 <= self.coarse_slack <= 1.0):
            fail(f"coarse_slack must be in [0, 1], got {self.coarse_slack!r}.")
        if (
            isinstance(self.max_coarse_peaks, bool)
            or not isinstance(self.max_coarse_peaks, int)
            or self.max_coarse_peaks <= 0
        ):
            fail(f"max_coarse_peaks must be a positive integer, got {self.max_coarse_peaks!r}.")
        if (
            isinstance(self.num_threads, bool)
            or not isinstance(self.num_threads, int)
            or not (0 <= self.num_threads <= 64)
        ):
            fail(f"num_threads must be an integer in [0, 64] (0 = auto), got {self.num_threads!r}.")

    def to_dict(self) -> dict:
        """JSON-serialisable form (recorded as ``detector.settings`` in scans)."""
        return {
            "threshold": float(self.threshold),
            "rotations": [int(r) for r in self.rotations],
            "search_region": None if self.search_region is None else self.search_region.to_dict(),
            "nms_iou_threshold": float(self.nms_iou_threshold),
            "duplicate_center_ratio": float(self.duplicate_center_ratio),
            "max_candidates": self.max_candidates,
            "max_candidates_per_rotation": self.max_candidates_per_rotation,
            "max_page_pixels": self.max_page_pixels,
            "min_template_side": self.min_template_side,
            "max_template_side": self.max_template_side,
            "min_template_stddev": float(self.min_template_stddev),
            "max_runtime_seconds": float(self.max_runtime_seconds),
            "blur_sigma": float(self.blur_sigma),
            "include_mirrored": self.include_mirrored,
            "coarse_to_fine": self.coarse_to_fine,
            "coarse_slack": float(self.coarse_slack),
            "max_coarse_peaks": self.max_coarse_peaks,
            "num_threads": self.num_threads,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScanSettings":
        """Inverse of :meth:`to_dict`. Unknown keys are rejected so a newer
        settings dict is never silently misread; missing keys use defaults."""
        if not isinstance(data, dict):
            raise DetectionError("invalid_settings", "Scan settings must be a JSON object.")
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise DetectionError(
                "invalid_settings",
                f"Unknown scan setting(s) {unknown}. They may come from a newer Pinny; upgrade it.",
            )
        kwargs = dict(data)
        if "rotations" in kwargs:
            kwargs["rotations"] = tuple(kwargs["rotations"])
        region = kwargs.get("search_region")
        if region is not None:
            kwargs["search_region"] = BoundingBox(
                int(region["x"]), int(region["y"]), int(region["width"]), int(region["height"])
            )
        settings = cls(**kwargs)
        settings.validate()
        return settings


@dataclass(frozen=True)
class Candidate:
    """One detected symbol instance, in canonical page coordinates."""

    #: Matching score (higher is better). Not a calibrated probability.
    score: float
    box: BoundingBox
    rotation: int
    #: v1.1: the template was flipped horizontally before ``rotation``.
    mirrored: bool = False

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
            "mirrored": self.mirrored,
        }


@dataclass(frozen=True)
class SkippedOrientation:
    """An orientation that was not searched because the template looks the
    same under it as under ``reported_as`` (the canonical orientation, whose
    label matches are reported with)."""

    rotation: int
    mirrored: bool
    reported_rotation: int
    reported_mirrored: bool
    #: Normalized correlation between the two oriented templates.
    similarity: float

    def to_dict(self) -> dict:
        return {
            "rotation": self.rotation,
            "mirrored": self.mirrored,
            "reported_as": {"rotation": self.reported_rotation, "mirrored": self.reported_mirrored},
            "similarity": self.similarity,
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
    #: v1.1: whether mirrored orientations were requested.
    include_mirrored: bool = False
    #: v1.1: requested orientations skipped because the template is
    #: symmetric under them; their matches carry the canonical label.
    skipped_orientations: Tuple[SkippedOrientation, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "detector": self.detector,
            "threshold": self.threshold,
            "rotations_searched": list(self.rotations_searched),
            "truncated": self.truncated,
            "elapsed_seconds": self.elapsed_seconds,
            "warnings": list(self.warnings),
            "include_mirrored": self.include_mirrored,
            "skipped_orientations": [o.to_dict() for o in self.skipped_orientations],
        }


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float, np.integer, np.floating))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
