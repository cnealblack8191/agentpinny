"""Pinny receptacle-symbol detection.

Depend on :class:`Detector` and the types below; :class:`OpenCVTemplateDetector`
is the current implementation.
"""

from .interface import Detector
from .opencv_matcher import OpenCVTemplateDetector
from .types import (
    ALLOWED_ROTATIONS,
    DEFAULT_SCALE_STEP,
    DEFAULT_SCORE_THRESHOLD,
    MAX_SCALE,
    MIN_SCALE,
    BoundingBox,
    Candidate,
    DetectionError,
    DetectionResult,
    DetectionTimeout,
    ScanSettings,
    SkippedOrientation,
    Template,
    scale_range,
)

__all__ = [
    "ALLOWED_ROTATIONS",
    "DEFAULT_SCALE_STEP",
    "DEFAULT_SCORE_THRESHOLD",
    "MAX_SCALE",
    "MIN_SCALE",
    "BoundingBox",
    "Candidate",
    "DetectionError",
    "DetectionResult",
    "DetectionTimeout",
    "Detector",
    "OpenCVTemplateDetector",
    "ScanSettings",
    "SkippedOrientation",
    "Template",
    "scale_range",
]
