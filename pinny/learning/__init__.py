"""Pinny local persistence and training-example capture (no training)."""

from .contract import Box, CropSpec, canonical_page_id, detection_crop, manual_pin_crop
from .loop import CropRecord, ThresholdSuggestion, suggest_threshold
from .store import (
    Batch, BatchPage, CropRenderer, Detection, IdempotencyConflict, InvalidArgument, InvalidTransition,
    LearningStore, LearningStoreError, NotFound, PageReview, Pin, QueueItem, ReviewEvent, ReviewResult, Scan,
    ScanState, SchemaMismatch, StaleVersion, Template, WrongThread, default_data_dir,
    detector_settings_sha, interpret_pin, local_reviewer_identity,
)

__all__ = [
    "Batch", "BatchPage", "Box", "CropRecord", "CropRenderer", "CropSpec", "Detection", "IdempotencyConflict",
    "InvalidArgument", "InvalidTransition", "LearningStore", "LearningStoreError", "NotFound",
    "PageReview", "Pin", "QueueItem", "ReviewEvent", "ReviewResult", "Scan", "ScanState", "SchemaMismatch",
    "StaleVersion", "Template", "ThresholdSuggestion", "WrongThread", "canonical_page_id",
    "default_data_dir", "detection_crop", "detector_settings_sha", "interpret_pin",
    "local_reviewer_identity", "manual_pin_crop", "suggest_threshold",
]
