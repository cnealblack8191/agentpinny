"""Pinny local persistence and training-example capture (no training)."""

from .contract import Box, CropSpec, canonical_page_id, detection_crop, manual_pin_crop
from .store import (
    CropRenderer, Detection, IdempotencyConflict, InvalidArgument, InvalidTransition,
    LearningStore, LearningStoreError, NotFound, Pin, ReviewEvent, ReviewResult, Scan, ScanState,
    SchemaMismatch, StaleVersion, default_data_dir, interpret_pin, local_reviewer_identity,
)

__all__ = [
    "Box", "CropRenderer", "CropSpec", "Detection", "IdempotencyConflict", "InvalidArgument",
    "InvalidTransition", "LearningStore", "LearningStoreError", "NotFound", "Pin", "ReviewEvent",
    "ReviewResult", "Scan", "ScanState", "SchemaMismatch", "StaleVersion", "canonical_page_id",
    "default_data_dir", "detection_crop", "interpret_pin", "local_reviewer_identity",
    "manual_pin_crop",
]
