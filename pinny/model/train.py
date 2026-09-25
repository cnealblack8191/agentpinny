"""Turn reviewed examples into a :class:`ModelPackage`.

This is the "training" step the training website runs. It uses only data
the learning store already holds and finishes in seconds on a CPU:

* **template bank** from tight crops of approved/added symbols, clustered
  around the user's original exemplar;
* **negative bank** from rejected detections (vetoes look-alikes);
* **kNN verifier** fitted on §6 crops (box + 24 px margin) of approved vs
  rejected detections;
* **isotonic calibration** and a **suggested threshold** from reviewed
  ``(score, label)`` pairs.

Crop inputs follow ``docs/contracts.md`` §6: machine-detection crops are the
detection box plus a 24 px margin. Pass ``template_crops`` (tight crops)
yourself when you have them, e.g. for manual pins, whose §6 crop is a fixed
128 px square rather than box + margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from pinny.detection import ScanSettings
from pinny.detection.calibration import IsotonicCalibrator, ThresholdSuggestion, suggest_threshold
from pinny.detection.template_bank import NegativeBank, build_template_bank, trim_margin
from pinny.detection.types import DetectionError
from pinny.detection.verifier import CROP_MARGIN_PX, KnnVerifier

from .format import ModelPackageError
from .package import Decision, ModelPackage, PackageTemplate


@dataclass
class TrainingResult:
    package: ModelPackage
    threshold_suggestion: ThresholdSuggestion
    #: Plain-language notes on what was and wasn't built, for the UI.
    notes: List[str] = field(default_factory=list)


def _tight(crops: Sequence[np.ndarray], label: str, notes: List[str]) -> List[np.ndarray]:
    out = []
    skipped = 0
    for c in crops:
        try:
            out.append(trim_margin(c, CROP_MARGIN_PX))
        except DetectionError:
            skipped += 1
    if skipped:
        notes.append(f"Skipped {skipped} {label} crop(s) too small to trim the {CROP_MARGIN_PX}px margin.")
    return out


def train_model(
    *,
    name: str,
    version: str,
    symbol_class: str,
    original_template: np.ndarray,
    positive_crops: Sequence[np.ndarray] = (),
    negative_crops: Sequence[np.ndarray] = (),
    template_crops: Optional[Sequence[np.ndarray]] = None,
    review_pairs: Sequence[Tuple[float, object]] = (),
    scan_settings: Optional[ScanSettings] = None,
    max_templates: int = 8,
    min_similarity: float = 0.9,
    max_negatives: int = 64,
    min_verifier_examples: int = 3,
    verifier_threshold: float = 0.5,
    target_precision: float = 0.95,
    min_labels: int = 30,
    description: str = "",
    renderer_version: str = "unknown",
    provenance: Optional[Dict[str, Any]] = None,
    evaluation: Optional[Dict[str, Any]] = None,
) -> TrainingResult:
    """Build a model package from reviewed examples.

    ``positive_crops`` / ``negative_crops`` are §6 crops (box + 24 px margin)
    of approved and rejected machine detections. ``review_pairs`` are
    ``(raw score, approved?)`` for reviewed machine detections of comparable
    scans (same template and settings), e.g. from
    ``LearningStore.review_stats``.

    The decision rule is the verifier (``p >= verifier_threshold``) when
    there are at least ``min_verifier_examples`` positives and negatives;
    otherwise the raw score against the suggested threshold, or the scan
    threshold if no suggestion is possible yet.
    """
    notes: List[str] = []
    settings = scan_settings or ScanSettings()
    settings.validate()
    if not isinstance(original_template, np.ndarray) or original_template.dtype != np.uint8:
        raise ModelPackageError("invalid_model", "original_template must be a uint8 image array.")

    tight_pos = list(template_crops) if template_crops is not None else _tight(positive_crops, "positive", notes)
    bank = build_template_bank(tight_pos, max_templates, min_similarity, original=original_template)
    templates = [PackageTemplate(b.image, b.support, b.is_original) for b in bank]
    notes.append(f"Template bank: {len(templates)} template(s) from {len(tight_pos)} example(s) plus the original.")

    negatives: List[np.ndarray] = []
    if negative_crops:
        tight_neg = _tight(negative_crops, "negative", notes)
        if tight_neg:
            negatives = NegativeBank.from_crops(tight_neg, max_items=max_negatives).crops
            notes.append(f"Negative veto: {len(negatives)} representative rejected crop(s).")

    verifier: Optional[KnnVerifier] = None
    if len(positive_crops) >= min_verifier_examples and len(negative_crops) >= min_verifier_examples:
        verifier = KnnVerifier().fit(list(positive_crops), list(negative_crops))
        notes.append(
            f"Verifier: kNN on {len(positive_crops)} approved and {len(negative_crops)} rejected crop(s)."
        )
    else:
        notes.append(
            f"No verifier: needs at least {min_verifier_examples} approved and {min_verifier_examples} "
            f"rejected crops (have {len(positive_crops)} and {len(negative_crops)})."
        )

    pairs = [(float(s), bool(y)) for s, y in review_pairs]
    n_pos = sum(1 for _, y in pairs if y)
    calibration: Optional[IsotonicCalibrator] = None
    if len(pairs) >= min_labels and 0 < n_pos < len(pairs):
        calibration = IsotonicCalibrator().fit([s for s, _ in pairs], [y for _, y in pairs])
        notes.append(f"Calibration: isotonic on {len(pairs)} reviewed score(s).")
    suggestion = suggest_threshold(pairs, target_precision=target_precision, min_labels=min_labels)

    if verifier is not None:
        decision = Decision("verifier", verifier_threshold)
    elif suggestion.value is not None and suggestion.value >= settings.threshold:
        decision = Decision("score", suggestion.value)
        notes.append(
            f"Decision: raw score >= {suggestion.value:.4f} (precision {suggestion.precision:.3f} "
            f"on {suggestion.n} reviewed detections)."
        )
    else:
        decision = Decision("score", settings.threshold)
        if suggestion.value is not None:
            notes.append(
                f"Suggested threshold {suggestion.value:.4f} is below the scan threshold "
                f"{settings.threshold}; lower the scan threshold to use it."
            )
        else:
            notes.append(f"No threshold suggestion ({suggestion.reason}); using the scan threshold.")

    prov = dict(provenance or {})
    prov.setdefault("n_positive_crops", len(positive_crops))
    prov.setdefault("n_negative_crops", len(negative_crops))
    prov.setdefault("n_template_examples", len(tight_pos))
    prov.setdefault("n_review_pairs", len(pairs))

    package = ModelPackage(
        name=name,
        version=version,
        symbol_class=symbol_class,
        templates=templates,
        scan_settings=settings,
        negatives=negatives,
        verifier=verifier,
        calibration=calibration,
        decision=decision,
        description=description,
        renderer_version=renderer_version,
        provenance=prov,
        evaluation=dict(evaluation or {}),
    )
    package.validate()
    return TrainingResult(package, suggestion, notes)
