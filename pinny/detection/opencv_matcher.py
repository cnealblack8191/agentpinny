"""Conventional OpenCV template matching for receptacle symbols.

Scope of this phase: one page, one symbol type, exact scale, quarter-turn
rotations only. No scale search, arbitrary-angle search, OCR or learned
models.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple, Union

import cv2
import numpy as np

from .suppression import suppress_duplicates
from .types import (
    BoundingBox,
    Candidate,
    DetectionError,
    DetectionResult,
    DetectionTimeout,
    ScanSettings,
    Template,
)

_CV_ROTATE = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}

#: Page windows whose grayscale range (max - min) is below this are treated
#: as flat: normalized correlation is undefined there, so they never match.
_FLAT_WINDOW_RANGE = 2

#: Scores may exceed 1 by floating-point error; beyond this it is a fault.
_SCORE_TOLERANCE = 1e-3

_EXCLUDED_SCORE = -2.0


class OpenCVTemplateDetector:
    """Normalized cross-correlation (``cv2.TM_CCOEFF_NORMED``) detector.

    Scores are correlation coefficients in [-1, 1]: 1.0 is a pixel-perfect
    match. They rank candidates for one template on one page and are not
    calibrated probabilities or comparable across templates.
    """

    name = "opencv-template-ccoeff-normed"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock

    def detect(
        self,
        page: np.ndarray,
        template: Union[Template, np.ndarray],
        settings: ScanSettings = ScanSettings(),
    ) -> DetectionResult:
        start = self._clock()
        if not isinstance(settings, ScanSettings):
            raise DetectionError("invalid_settings", "settings must be a ScanSettings instance.")
        settings.validate()

        template_image = template.image if isinstance(template, Template) else template
        _validate_raster(page, "page")
        _validate_raster(template_image, "template")
        if _channels(page) != _channels(template_image):
            raise DetectionError(
                "channel_mismatch",
                f"Template has {_channels(template_image)} channel(s) but the page has "
                f"{_channels(page)}. Crop the template from the same canonical raster.",
            )

        region = settings.search_region or BoundingBox(0, 0, page.shape[1], page.shape[0])
        _validate_region(region, page)
        if region.width * region.height > settings.max_page_pixels:
            raise DetectionError(
                "page_too_large",
                f"Search area is {region.width}x{region.height} = "
                f"{region.width * region.height} pixels, above max_page_pixels="
                f"{settings.max_page_pixels}. Render the page at a lower resolution, "
                "set a smaller search_region, or raise the limit.",
            )

        # Identical preprocessing for page and template: grayscale uint8.
        page_gray = _to_gray(page[region.y : region.y2, region.x : region.x2])
        template_gray = _to_gray(template_image)
        _validate_template(template_gray, region, settings)

        warnings: List[str] = []
        truncated = False
        all_boxes: List[np.ndarray] = []
        all_scores: List[np.ndarray] = []
        all_rotations: List[np.ndarray] = []

        for rotation in settings.rotations:
            rotated = template_gray if rotation == 0 else cv2.rotate(template_gray, _CV_ROTATE[rotation])
            th, tw = rotated.shape
            ys, xs, scores, hit_cap, bad = _match_one_rotation(page_gray, rotated, settings)
            truncated |= hit_cap
            if bad:
                warnings.append(
                    f"rotation {rotation}: ignored {bad} non-finite or out-of-range score(s)."
                )
            if len(scores):
                boxes = np.empty((len(scores), 4), dtype=np.int64)
                # Map back from the search-region crop to canonical page space.
                boxes[:, 0] = xs + region.x
                boxes[:, 1] = ys + region.y
                boxes[:, 2] = tw
                boxes[:, 3] = th
                all_boxes.append(boxes)
                all_scores.append(scores)
                all_rotations.append(np.full(len(scores), rotation, dtype=np.int64))
            self._check_deadline(start, settings, f"after rotation {rotation}")

        candidates: Tuple[Candidate, ...] = ()
        if all_scores:
            boxes = np.concatenate(all_boxes)
            scores = np.concatenate(all_scores)
            rotations = np.concatenate(all_rotations)
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
            candidates = tuple(
                Candidate(
                    score=float(scores[i]),
                    box=BoundingBox(*(int(v) for v in boxes[i])),
                    rotation=int(rotations[i]),
                )
                for i in keep
            )
        if truncated:
            warnings.append(
                "Candidate limit reached; more matches may exist. Raise the threshold, "
                "narrow search_region, or raise max_candidates / max_candidates_per_rotation."
            )

        return DetectionResult(
            candidates=candidates,
            detector=self.name,
            threshold=float(settings.threshold),
            rotations_searched=tuple(int(r) for r in settings.rotations),
            truncated=truncated,
            elapsed_seconds=self._clock() - start,
            warnings=tuple(warnings),
        )

    def _check_deadline(self, start: float, settings: ScanSettings, stage: str) -> None:
        elapsed = self._clock() - start
        if elapsed > settings.max_runtime_seconds:
            raise DetectionTimeout(
                f"Scan exceeded max_runtime_seconds={settings.max_runtime_seconds} "
                f"({elapsed:.2f}s {stage}). Use a smaller search_region, fewer rotations, "
                "a smaller template, or a lower-resolution page."
            )


def _match_one_rotation(
    page_gray: np.ndarray, template_gray: np.ndarray, settings: ScanSettings
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, bool, int]:
    """Return (ys, xs, scores, hit_cap, bad_count) of local maxima >= threshold,
    in search-region coordinates of the template's top-left corner."""
    th, tw = template_gray.shape
    result = cv2.matchTemplate(page_gray, template_gray, cv2.TM_CCOEFF_NORMED)

    # Normalized correlation is undefined on flat page windows and OpenCV
    # returns arbitrary values (including 1.0) there; exclude them.
    kernel = np.ones((th, tw), dtype=np.uint8)
    win_min = cv2.erode(page_gray, kernel, anchor=(0, 0), borderType=cv2.BORDER_REPLICATE)
    win_max = cv2.dilate(page_gray, kernel, anchor=(0, 0), borderType=cv2.BORDER_REPLICATE)
    rh, rw = result.shape
    flat = (win_max[:rh, :rw].astype(np.int16) - win_min[:rh, :rw]) < _FLAT_WINDOW_RANGE
    del win_min, win_max

    invalid = ~np.isfinite(result) | (np.abs(result) > 1.0 + _SCORE_TOLERANCE)
    invalid &= ~flat
    bad = int(np.count_nonzero(invalid))
    if bad == result.size:
        raise DetectionError(
            "invalid_result",
            "Template matching produced no finite scores. Check that the page and "
            "template are valid 8-bit images.",
        )
    # Sentinel below any valid threshold (thresholds are >= -1).
    result[flat | invalid] = _EXCLUDED_SCORE
    np.minimum(result, 1.0, out=result)

    # Local maxima within a neighbourhood of about half the template size;
    # cross-rotation duplicates are removed later by suppress_duplicates.
    peak_kernel = np.ones((2 * (th // 4) + 1, 2 * (tw // 4) + 1), dtype=np.uint8)
    neighbourhood_max = cv2.dilate(result, peak_kernel, borderType=cv2.BORDER_REPLICATE)
    peaks = (result >= settings.threshold) & (result >= neighbourhood_max)
    peaks &= result > _EXCLUDED_SCORE
    del neighbourhood_max

    flat_idx = np.flatnonzero(peaks)
    hit_cap = False
    if len(flat_idx) > settings.max_candidates_per_rotation:
        hit_cap = True
        values = result.ravel()[flat_idx]
        top = np.argpartition(-values, settings.max_candidates_per_rotation - 1)
        flat_idx = flat_idx[top[: settings.max_candidates_per_rotation]]
    ys, xs = np.divmod(flat_idx, rw)
    scores = result.ravel()[flat_idx].astype(np.float64)
    return ys, xs, scores, hit_cap, bad


def _channels(image: np.ndarray) -> int:
    return 1 if image.ndim == 2 else image.shape[2]


def _validate_raster(image: object, label: str) -> None:
    if not isinstance(image, np.ndarray):
        raise DetectionError(f"invalid_{label}", f"The {label} must be a numpy array.")
    if image.dtype != np.uint8:
        raise DetectionError(
            f"invalid_{label}",
            f"The {label} must be an 8-bit (uint8) raster, got dtype {image.dtype}. "
            "Pass the canonical page raster, or a crop of it, unconverted.",
        )
    if image.ndim not in (2, 3) or (image.ndim == 3 and image.shape[2] not in (1, 3, 4)):
        raise DetectionError(
            f"invalid_{label}",
            f"The {label} must have shape (H, W) or (H, W, 1|3|4), got {image.shape}.",
        )
    if image.shape[0] == 0 or image.shape[1] == 0:
        raise DetectionError(f"invalid_{label}", f"The {label} is empty ({image.shape}).")


def _validate_region(region: BoundingBox, page: np.ndarray) -> None:
    page_h, page_w = page.shape[:2]
    if region.x < 0 or region.y < 0 or region.x2 > page_w or region.y2 > page_h:
        raise DetectionError(
            "invalid_search_region",
            f"search_region {region.to_dict()} extends outside the {page_w}x{page_h} "
            "page raster.",
        )


def _validate_template(
    template_gray: np.ndarray, region: BoundingBox, settings: ScanSettings
) -> None:
    th, tw = template_gray.shape
    if min(th, tw) < settings.min_template_side:
        raise DetectionError(
            "template_too_small",
            f"Template is {tw}x{th} pixels; its shorter side must be at least "
            f"{settings.min_template_side}. Select a larger region around the symbol.",
        )
    if max(th, tw) > settings.max_template_side:
        raise DetectionError(
            "template_too_large",
            f"Template is {tw}x{th} pixels; its longer side must be at most "
            f"{settings.max_template_side}. Crop tightly around a single symbol.",
        )
    for rotation in settings.rotations:
        rw, rh = (th, tw) if rotation in (90, 270) else (tw, th)
        if rw > region.width or rh > region.height:
            raise DetectionError(
                "template_too_large",
                f"Template rotated {rotation} degrees is {rw}x{rh}, which does not fit "
                f"in the {region.width}x{region.height} search area. Crop a smaller "
                "template, enlarge search_region, or drop that rotation.",
            )
    std = float(np.std(template_gray))
    if not np.isfinite(std) or std < settings.min_template_stddev:
        raise DetectionError(
            "template_blank",
            f"Template is blank or near-uniform (grayscale std {std:.2f} < "
            f"{settings.min_template_stddev}). Select a region containing the symbol's "
            "line work.",
        )


def _to_gray(image: np.ndarray) -> np.ndarray:
    """Grayscale uint8, applied identically to page and template. Assumes RGB(A)
    channel order; since both inputs go through the same conversion, a BGR
    raster only changes luminance weights, not match consistency."""
    if image.ndim == 2:
        gray = image
    elif image.shape[2] == 1:
        gray = image[:, :, 0]
    elif image.shape[2] == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
    return np.ascontiguousarray(gray)
