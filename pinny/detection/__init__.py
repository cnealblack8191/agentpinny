"""Pinny receptacle-symbol detection.

Depend on :class:`Detector` and the types below; :class:`OpenCVTemplateDetector`
is the current implementation.
"""

from .interface import Detector
from .opencv_matcher import OpenCVTemplateDetector
from .types import (
    ALLOWED_ROTATIONS,
    DEFAULT_SCORE_THRESHOLD,
    BoundingBox,
    Candidate,
    DetectionError,
    DetectionResult,
    DetectionTimeout,
    ScanSettings,
    Template,
)

__all__ = [
    "ALLOWED_ROTATIONS",
    "DEFAULT_SCORE_THRESHOLD",
    "BoundingBox",
    "Candidate",
    "DetectionError",
    "DetectionResult",
    "DetectionTimeout",
    "Detector",
    "OpenCVTemplateDetector",
    "ScanSettings",
    "Template",
]
