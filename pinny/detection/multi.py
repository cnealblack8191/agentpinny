"""Scan one page with several templates (a template bank) and pool results.

:func:`detect_multi` runs any :class:`~pinny.detection.Detector` once per
template, pools every candidate, and removes duplicates *across templates*
with the same :func:`~pinny.detection.suppression.suppress_duplicates` rule
the detector uses across rotations. Each surviving candidate remembers the
index of the template that produced it.

Optionally a :class:`~pinny.detection.template_bank.NegativeBank` of
rejected crops vetoes candidates: a candidate is dropped when its tight
crop correlates with some rejected crop better than with its best positive
template by more than ``veto_margin``.

Raw scores are pooled unchanged. NCC scores of different templates are on
the same [-1, 1] scale but not calibrated against each other; see
``calibration.py`` for per-template background normalization.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple, Union

import numpy as np

from .interface import Detector
from .suppression import suppress_duplicates
from .template_bank import BankTemplate, NegativeBank, convert_channels, ncc, rotate_quarter, to_gray
from .types import Candidate, DetectionError, DetectionResult, DetectionTimeout, ScanSettings, Template

__all__ = ["MultiDetectionResult", "VetoRecord", "detect_multi"]

TemplateLike = Union[Template, BankTemplate, np.ndarray]


@dataclass(frozen=True)
class VetoRecord:
    candidate: Candidate
    template_index: int
    positive_similarity: float
    negative_similarity: float


@dataclass(frozen=True)
class MultiDetectionResult:
    """Pooled result. ``result`` is an ordinary :class:`DetectionResult`;
    ``template_indices[i]`` is the template that produced
    ``result.candidates[i]``."""

    result: DetectionResult
    template_indices: Tuple[int, ...]
    #: Per-template results before cross-template suppression.
    per_template: Tuple[DetectionResult, ...] = ()
    #: Candidates removed by the negative veto (best first).
    vetoed: Tuple[VetoRecord, ...] = field(default_factory=tuple)

    @property
    def candidates(self) -> Tuple[Candidate, ...]:
        return self.result.candidates

    def pairs(self) -> List[Tuple[Candidate, int]]:
        return list(zip(self.result.candidates, self.template_indices))

    def to_dict(self) -> dict:
        d = self.result.to_dict()
        for cd, ti in zip(d["candidates"], self.template_indices):
            cd["template_index"] = ti
        d["templates_searched"] = len(self.per_template)
        d["vetoed"] = [
            {
                **v.candidate.to_dict(),
                "template_index": v.template_index,
                "positive_similarity": v.positive_similarity,
                "negative_similarity": v.negative_similarity,
            }
            for v in self.vetoed
        ]
        return d


def _template_image(t: TemplateLike) -> np.ndarray:
    if isinstance(t, Template):
        return t.image
    if isinstance(t, BankTemplate):
        return t.image
    if isinstance(t, np.ndarray):
        return t
    raise DetectionError("invalid_template", f"Unsupported template type {type(t).__name__}.")


def _channels(img: np.ndarray) -> int:
    return 1 if img.ndim == 2 else img.shape[2]


def _positive_similarity(crop: np.ndarray, template_gray: np.ndarray, rotation: int, mirrored: bool) -> float:
    t = template_gray[:, ::-1] if mirrored else template_gray
    return ncc(crop, rotate_quarter(np.ascontiguousarray(t), rotation))


def detect_multi(
    detector: Detector,
    page: np.ndarray,
    templates: Sequence[TemplateLike],
    settings: ScanSettings = ScanSettings(),
    negatives: Optional[NegativeBank] = None,
    *,
    veto_margin: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> MultiDetectionResult:
    """Run ``detector`` for each template and pool the candidates.

    Templates whose channel count differs from the page are converted (bank
    medoids are often grayscale; the app's pages are RGB). The overall
    ``settings.max_runtime_seconds`` budget is shared across templates.
    Output is sorted by descending score, then top-to-bottom, left-to-right,
    with exact ties resolved in favour of the lower template index.
    """
    if not templates:
        raise DetectionError("invalid_template", "detect_multi needs at least one template.")
    if not isinstance(page, np.ndarray) or page.ndim not in (2, 3):
        raise DetectionError("invalid_page", "Page raster must be a 2-D or 3-D numpy array.")
    if not isinstance(settings, ScanSettings):
        raise DetectionError("invalid_settings", "settings must be a ScanSettings instance.")
    settings.validate()
    start = clock()
    page_ch = _channels(page)

    per_template: List[DetectionResult] = []
    rows: List[Tuple[Candidate, int]] = []
    warnings: List[str] = []
    for ti, t in enumerate(templates):
        remaining = settings.max_runtime_seconds - (clock() - start)
        if remaining <= 0:
            raise DetectionTimeout(
                f"detect_multi exceeded max_runtime_seconds={settings.max_runtime_seconds} "
                f"before template {ti} of {len(templates)}. Use fewer templates or a smaller "
                "search_region."
            )
        img = _template_image(t)
        if _channels(img) != page_ch:
            img = convert_channels(img, page_ch)
        res = detector.detect(
            page, Template(image=img), dataclasses.replace(settings, max_runtime_seconds=remaining)
        )
        per_template.append(res)
        warnings.extend(f"template {ti}: {w}" for w in res.warnings)
        rows.extend((c, ti) for c in res.candidates)

    truncated = any(r.truncated for r in per_template)
    kept_rows: List[Tuple[Candidate, int]] = []
    if rows:
        boxes = np.array([[c.box.x, c.box.y, c.box.width, c.box.height] for c, _ in rows], dtype=np.int64)
        scores = np.array([c.score for c, _ in rows], dtype=np.float64)
        keep = suppress_duplicates(
            boxes,
            scores,
            settings.nms_iou_threshold,
            settings.duplicate_center_ratio,
            settings.max_candidates + 1,
        )
        if len(keep) > settings.max_candidates:
            truncated = True
            keep = keep[: settings.max_candidates]
        kept_rows = [rows[i] for i in keep]

    vetoed: List[VetoRecord] = []
    if negatives is not None and len(negatives) and kept_rows:
        grays = [to_gray(_template_image(t)) for t in templates]
        survivors = []
        for c, ti in kept_rows:
            crop = page[c.box.y : c.box.y2, c.box.x : c.box.x2]
            mirrored = bool(getattr(c, "mirrored", False))
            pos = max(_positive_similarity(crop, g, c.rotation, mirrored) for g in grays)
            neg = negatives.max_similarity(crop)
            if neg > pos + veto_margin:
                vetoed.append(VetoRecord(c, ti, float(pos), float(neg)))
            else:
                survivors.append((c, ti))
        kept_rows = survivors

    if truncated:
        warnings.append(
            "Candidate limit reached; more matches may exist. Raise the threshold, "
            "narrow search_region, or raise max_candidates."
        )
    first = per_template[0]
    merged = DetectionResult(
        candidates=tuple(c for c, _ in kept_rows),
        detector=first.detector,
        threshold=float(settings.threshold),
        rotations_searched=first.rotations_searched,
        truncated=truncated,
        elapsed_seconds=clock() - start,
        warnings=tuple(warnings),
    )
    return MultiDetectionResult(
        result=merged,
        template_indices=tuple(ti for _, ti in kept_rows),
        per_template=tuple(per_template),
        vetoed=tuple(vetoed),
    )
