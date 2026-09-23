"""Pinny local persistence and training-example capture (no training)."""

from .contract import CropSpec, detection_crop, manual_pin_crop
from .store import (
    CropRenderer, Detection, IdempotencyConflict, InvalidTransition, LearningStore,
    LearningStoreError, NotFound, Pin, ReviewEvent, ReviewResult, Scan, ScanState, StaleVersion,
    default_data_dir, interpret_pin, local_reviewer_identity,
)

__all__ = [
    "CropRenderer", "CropSpec", "Detection", "IdempotencyConflict", "InvalidTransition",
    "LearningStore", "LearningStoreError", "NotFound", "Pin", "ReviewEvent", "ReviewResult",
    "Scan", "ScanState", "StaleVersion", "default_data_dir", "detection_crop", "interpret_pin",
    "local_reviewer_identity", "manual_pin_crop",
]
