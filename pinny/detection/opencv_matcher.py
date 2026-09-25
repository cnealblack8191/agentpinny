"""Conventional OpenCV template matching for receptacle symbols.

Scope: one page, one symbol type, quarter-turn rotations (optionally
mirrored). Tolerance to sub-pixel shifts and small scale differences comes
from a Gaussian blur applied identically to page and template; there is no
explicit scale search, arbitrary-angle search, OCR or learned model.

Pipeline per scan (see ``docs/detection.md``):

1. Grayscale, then the same Gaussian blur on page and template.
2. Build the requested orientations (rotation, optionally after a
   horizontal flip) and drop those the template is symmetric under.
3. Per orientation: a half-resolution pre-pass whose peaks are refined at
   full resolution (large templates), or a full-resolution search in
   horizontal strips (optionally threaded).
4. Validity checks (flat / low-variance windows, out-of-range scores) and the
   local-maximum test run only at scores >= threshold.
5. Cross-orientation duplicate suppression.
"""

from __future__ import annotations

import math
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

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
    SkippedOrientation,
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

#: Page windows whose grayscale variance is below this fraction of the
#: (preprocessed) template's variance are treated as flat too. This rejects
#: near-blank windows (e.g. the faint blur halo beside a line) where the
#: correlation is dominated by noise.
_FLAT_VARIANCE_RATIO = 0.05

#: Scores may exceed 1 by floating-point error; beyond this it is a fault.
_SCORE_TOLERANCE = 1e-3

_EXCLUDED_SCORE = -2.0

#: Two orientations of the template with at least this normalized
#: correlation are treated as the same orientation (symmetric symbol).
_SYMMETRY_NCC = 0.97

#: Coarse-to-fine applies only if the oriented template's shorter side is at
#: least this many pixels (at half resolution smaller symbols lose detail).
_COARSE_MIN_SIDE = 24

#: Half-size, in full-resolution pixels, of the window searched around each
#: coarse peak's projected position.
_REFINE_RADIUS = 4

#: Estimated cost of refining one coarse peak, in full-resolution score-map
#: pixels (mostly per-call overhead). If refining all coarse peaks would cost
#: more than the full-resolution search, that search is used instead.
_REFINE_COST_PIXELS = 2000

#: The local-maximum test loops over candidates (~10 us each) unless there
#: are more than max(this, score-map pixels / _LOCAL_MAX_PIXELS_PER_LOOP),
#: when one dilation of the map (~30 ns per pixel) is cheaper.
_LOCAL_MAX_LOOP_LIMIT = 256
_LOCAL_MAX_PIXELS_PER_LOOP = 300

#: Result pixels per full-resolution strip (bounds per-strip memory to about
#: 4 bytes x this for the score map, plus the page strip and OpenCV's DFT
#: buffers, which dominate: measured ~25 bytes x this per worker).
_STRIP_RESULT_PIXELS = 1_000_000

#: If more than this many scores in one score map pass the threshold, the
#: validity checks run as dense full-map passes instead of per candidate.
_DENSE_MIN_CANDIDATES = 50_000
_DENSE_CANDIDATE_FRACTION = 0.05

#: Elements (candidates x template pixels) gathered per chunk when checking
#: candidate windows for flatness.
_GATHER_CHUNK_ELEMENTS = 4_000_000

Orientation = Tuple[bool, int]  # (mirrored, clockwise rotation)
_Peaks = Tuple[np.ndarray, np.ndarray, np.ndarray, int]  # ys, xs, scores, bad


class OpenCVTemplateDetector:
    """Normalized cross-correlation (``cv2.TM_CCOEFF_NORMED``) detector.

    Scores are correlation coefficients in [-1, 1] between the *blurred*
    page window and the *blurred* template: 1.0 is a pixel-perfect match.
    They rank candidates for one template on one page and are not calibrated
    probabilities or comparable across templates.
    """

    name = "opencv-template"

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

        template_gray = _to_gray(template_image)
        _validate_template(template_gray, region, settings)
        # Identical preprocessing for page and template: grayscale uint8, then
        # the same Gaussian blur.
        page_prep = _prepare_page(page, region, settings.blur_sigma)
        template_prep = _blur(template_gray, settings.blur_sigma, _border_median(template_gray))

        orientations = [(False, r) for r in sorted(settings.rotations)]
        if settings.include_mirrored:
            orientations += [(True, r) for r in sorted(settings.rotations)]
        oriented = {o: _orient(template_prep, o) for o in orientations}
        searched, skipped = _canonical_orientations(orientations, oriented)

        warnings: List[str] = []
        for s in skipped:
            warnings.append(
                f"{_label(s.mirrored, s.rotation)} not searched: the template is symmetric "
                f"under it (similarity {s.similarity:.3f}); its matches are reported as "
                f"{_label(s.reported_mirrored, s.reported_rotation)}."
            )

        n_threads = settings.num_threads or min(4, os.cpu_count() or 1)
        executor = ThreadPoolExecutor(n_threads) if n_threads > 1 else None
        truncated = False
        all_boxes: List[np.ndarray] = []
        all_scores: List[np.ndarray] = []
        all_orient: List[Tuple[int, int, int]] = []  # (count, rotation, mirrored)
        try:
            search = _Search(self, start, settings, page_prep, executor, n_threads)
            for i, orientation in enumerate(searched):
                if i > 0:
                    self._check_deadline(
                        start, settings, f"before {_label(*orientation)}"
                    )
                rotated = oriented[orientation]
                th, tw = rotated.shape
                ys, xs, scores, bad = search.orientation(rotated)
                if bad:
                    warnings.append(
                        f"{_label(*orientation)}: ignored {bad} non-finite or out-of-range "
                        "score(s)."
                    )
                if len(scores) > settings.max_candidates_per_rotation:
                    truncated = True
                    order = np.lexsort((xs, ys, -scores))[: settings.max_candidates_per_rotation]
                    ys, xs, scores = ys[order], xs[order], scores[order]
                if len(scores):
                    boxes = np.empty((len(scores), 4), dtype=np.int64)
                    # Map back from the search-region crop to canonical page space.
                    boxes[:, 0] = xs + region.x
                    boxes[:, 1] = ys + region.y
                    boxes[:, 2] = tw
                    boxes[:, 3] = th
                    all_boxes.append(boxes)
                    all_scores.append(scores)
                    all_orient.append((len(scores), orientation[1], int(orientation[0])))
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)

        candidates: Tuple[Candidate, ...] = ()
        if all_scores:
            boxes = np.concatenate(all_boxes)
            scores = np.concatenate(all_scores)
            rotations = np.concatenate([np.full(n, r, dtype=np.int64) for n, r, _ in all_orient])
            mirrored = np.concatenate([np.full(n, m, dtype=bool) for n, _, m in all_orient])
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
                    mirrored=bool(mirrored[i]),
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
            include_mirrored=settings.include_mirrored,
            skipped_orientations=tuple(skipped),
        )

    def _check_deadline(self, start: float, settings: ScanSettings, stage: str) -> None:
        elapsed = self._clock() - start
        if elapsed > settings.max_runtime_seconds:
            raise DetectionTimeout(
                f"Scan exceeded max_runtime_seconds={settings.max_runtime_seconds} "
                f"({elapsed:.2f}s, {stage}). Use a smaller search_region, fewer rotations, "
                "a smaller template, or a lower-resolution page."
            )


class _Search:
    """Per-scan search state shared by all orientations."""

    def __init__(
        self,
        detector: OpenCVTemplateDetector,
        start: float,
        settings: ScanSettings,
        page: np.ndarray,
        executor: Optional[ThreadPoolExecutor],
        n_threads: int,
    ) -> None:
        self.detector = detector
        self.start = start
        self.settings = settings
        self.page = page
        self.executor = executor
        self.n_threads = n_threads
        self._page_half: Optional[np.ndarray] = None

    def orientation(self, template: np.ndarray) -> _Peaks:
        """Peaks (ys, xs, scores, bad) of one oriented, preprocessed template,
        in search-region coordinates of the template's top-left corner."""
        s = self.settings
        th, tw = template.shape
        var_min = _FLAT_VARIANCE_RATIO * float(template.var())
        ry, rx = th // 4, tw // 4
        if s.coarse_to_fine and min(th, tw) >= _COARSE_MIN_SIDE:
            peaks = self._coarse_to_fine(template, var_min, ry, rx)
            if peaks is not None:
                return peaks
        return self._strips(self.page, template, s.threshold, var_min, ry, rx, "full resolution")

    def _strips(
        self,
        page: np.ndarray,
        template: np.ndarray,
        threshold: float,
        var_min: float,
        ry: int,
        rx: int,
        stage: str,
    ) -> _Peaks:
        """Full search of ``page`` in horizontal strips. Each strip's score map
        is extended by the local-maximum radius so peaks near strip borders see
        their whole neighbourhood; results equal an untiled search."""
        th, tw = template.shape
        rh = page.shape[0] - th + 1
        rw = page.shape[1] - tw + 1
        rows = max(1, _STRIP_RESULT_PIXELS // rw)

        def job(r0: int, r1: int) -> _Peaks:
            e0, e1 = max(0, r0 - ry), min(rh, r1 + ry)
            strip = page[e0 : e1 + th - 1]
            result = cv2.matchTemplate(strip, template, cv2.TM_CCOEFF_NORMED)
            ys, xs, scores, bad = _find_peaks(
                result, strip, template.shape, threshold, var_min, ry, rx,
                (r0 - e0, r1 - e0, 0, rw),
            )
            return ys + e0, xs, scores, bad

        jobs = [
            (lambda r0=r0: job(r0, min(rh, r0 + rows))) for r0 in range(0, rh, rows)
        ]
        return _concat(self._run(jobs, stage))

    def _run(self, jobs: Sequence[Callable[[], _Peaks]], stage: str) -> List[_Peaks]:
        """Run jobs in order (threaded if configured), checking the deadline
        before each job after the first."""
        check = self.detector._check_deadline
        if self.executor is None or len(jobs) == 1:
            out = []
            for i, job in enumerate(jobs):
                if i:
                    check(self.start, self.settings, f"{stage}, strip {i + 1}/{len(jobs)}")
                out.append(job())
            return out
        futures = []
        try:
            for i, job in enumerate(jobs):
                if i:
                    check(self.start, self.settings, f"{stage}, strip {i + 1}/{len(jobs)}")
                running = [f for f in futures if not f.done()]
                if len(running) >= self.n_threads:
                    wait(running, return_when=FIRST_COMPLETED)
                futures.append(self.executor.submit(job))
            return [f.result() for f in futures]
        except BaseException:
            for f in futures:
                f.cancel()
            raise

    def _coarse_to_fine(
        self, template: np.ndarray, var_min: float, ry: int, rx: int
    ) -> Optional[_Peaks]:
        """Half-resolution pre-pass, then full-resolution refinement around each
        coarse peak. Returns None to request the full-resolution search."""
        s = self.settings
        if self._page_half is None:
            self._page_half = cv2.pyrDown(self.page)
        page_half = self._page_half
        template_half = cv2.pyrDown(template)
        hh, hw = template_half.shape
        if hh > page_half.shape[0] or hw > page_half.shape[1]:
            return None
        coarse_threshold = max(-1.0, s.threshold - s.coarse_slack)
        cys, cxs, _, _ = self._strips(
            page_half,
            template_half,
            coarse_threshold,
            _FLAT_VARIANCE_RATIO * float(template_half.var()),
            max(1, hh // 8),
            max(1, hw // 8),
            "coarse pass",
        )
        page = self.page
        th, tw = template.shape
        rh, rw = page.shape[0] - th + 1, page.shape[1] - tw + 1
        if len(cys) > s.max_coarse_peaks or len(cys) * _REFINE_COST_PIXELS > rh * rw:
            return None
        found: Dict[Tuple[int, int], float] = {}
        bad = 0
        for cy, cx in zip((2 * cys).tolist(), (2 * cxs).tolist()):
            y0, y1 = max(0, cy - _REFINE_RADIUS), min(rh, cy + _REFINE_RADIUS + 1)
            x0, x1 = max(0, cx - _REFINE_RADIUS), min(rw, cx + _REFINE_RADIUS + 1)
            if y0 >= y1 or x0 >= x1:
                continue
            ey0, ey1 = max(0, y0 - ry), min(rh, y1 + ry)
            ex0, ex1 = max(0, x0 - rx), min(rw, x1 + rx)
            roi = page[ey0 : ey1 + th - 1, ex0 : ex1 + tw - 1]
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            ys, xs, scores, b = _find_peaks(
                result, roi, template.shape, s.threshold, var_min, ry, rx,
                (y0 - ey0, y1 - ey0, x0 - ex0, x1 - ex0),
            )
            bad += b
            for y, x, v in zip((ys + ey0).tolist(), (xs + ex0).tolist(), scores.tolist()):
                found[(y, x)] = v
        if not found:
            return _empty_peaks(bad)
        keys = np.array(list(found.keys()), dtype=np.int64)
        return keys[:, 0], keys[:, 1], np.array(list(found.values()), dtype=np.float64), bad


def _find_peaks(
    result: np.ndarray,
    page: np.ndarray,
    template_shape: Tuple[int, int],
    threshold: float,
    var_min: float,
    ry: int,
    rx: int,
    core: Tuple[int, int, int, int],
) -> _Peaks:
    """Valid local maxima >= threshold of ``result`` inside ``core`` (row0,
    row1, col0, col1; exclusive ends), where ``result[y, x]`` is the score of
    the window ``page[y:y+th, x:x+tw]``. Scores outside the core are used only
    as neighbours for the local-maximum test.

    Candidates are selected first (``result >= threshold``, which NaN fails),
    then validity and the local-maximum test run only at those positions.
    """
    cy0, cy1, cx0, cx1 = core
    rh, rw = result.shape
    nonfinite = 0
    if not cv2.checkRange(result, quiet=True)[0]:  # rare: NaN or inf present
        finite = np.isfinite(result)
        if not finite.any():
            raise DetectionError(
                "invalid_result",
                "Template matching produced no finite scores. Check that the page and "
                "template are valid 8-bit images.",
            )
        core_finite = finite[cy0:cy1, cx0:cx1]
        nonfinite = core_finite.size - int(np.count_nonzero(core_finite))

    flat_idx = np.flatnonzero(result >= threshold)
    if len(flat_idx) > max(_DENSE_MIN_CANDIDATES, _DENSE_CANDIDATE_FRACTION * result.size):
        return _find_peaks_dense(
            result, page, template_shape, threshold, var_min, ry, rx, core, nonfinite
        )
    if not len(flat_idx):
        return _empty_peaks(nonfinite)

    ys, xs = np.divmod(flat_idx, rw)
    values = result.ravel()[flat_idx]
    out_of_range = values > 1.0 + _SCORE_TOLERANCE
    flat = _flat_windows(page, ys, xs, template_shape, var_min)
    invalid = flat | out_of_range
    in_core = (ys >= cy0) & (ys < cy1) & (xs >= cx0) & (xs < cx1)
    bad = nonfinite + int(np.count_nonzero(out_of_range & ~flat & in_core))

    clipped = np.minimum(values, 1.0)
    selected = np.flatnonzero(~invalid & in_core)
    if len(selected) > max(_LOCAL_MAX_LOOP_LIMIT, result.size // _LOCAL_MAX_PIXELS_PER_LOOP):
        # Many peaks to test: one dilation of a masked copy is cheaper than a
        # Python loop. Same semantics: invalid and non-finite scores never win.
        work = result.copy()
        work[ys[invalid], xs[invalid]] = _EXCLUDED_SCORE
        if nonfinite:
            work[~np.isfinite(work)] = _EXCLUDED_SCORE
        np.minimum(work, 1.0, out=work)
        peak_kernel = np.ones((2 * ry + 1, 2 * rx + 1), dtype=np.uint8)
        neighbourhood_max = cv2.dilate(work, peak_kernel, borderType=cv2.BORDER_REPLICATE)
        keep_idx = selected[clipped[selected] >= neighbourhood_max[ys[selected], xs[selected]]]
        return ys[keep_idx], xs[keep_idx], clipped[keep_idx].astype(np.float64), bad

    invalid_map = None
    if invalid.any():
        # Lazily zeroed (calloc), so only touched pages cost memory.
        invalid_map = np.zeros(result.shape, dtype=bool)
        invalid_map[ys[invalid], xs[invalid]] = True
    keep = []
    for j in selected.tolist():
        y, x = int(ys[j]), int(xs[j])
        a, b, c, d = max(0, y - ry), min(rh, y + ry + 1), max(0, x - rx), min(rw, x + rx + 1)
        window = result[a:b, c:d]
        raw_max = window.max()
        if clipped[j] >= min(raw_max, 1.0):
            keep.append(j)  # no neighbour, valid or not, beats it
            continue
        if invalid_map is None and math.isfinite(raw_max):
            continue  # a higher neighbour exists and every candidate is valid
        usable = window >= threshold
        if invalid_map is not None:
            usable &= ~invalid_map[a:b, c:d]
        if clipped[j] >= min(float(window[usable].max()), 1.0):
            keep.append(j)
    keep_idx = np.asarray(keep, dtype=np.intp)
    return ys[keep_idx], xs[keep_idx], clipped[keep_idx].astype(np.float64), bad


def _find_peaks_dense(
    result: np.ndarray,
    page: np.ndarray,
    template_shape: Tuple[int, int],
    threshold: float,
    var_min: float,
    ry: int,
    rx: int,
    core: Tuple[int, int, int, int],
    nonfinite: int,
) -> _Peaks:
    """Same semantics as :func:`_find_peaks` using full-map passes; used when
    so many scores pass the threshold that per-candidate work would be slower."""
    th, tw = template_shape
    cy0, cy1, cx0, cx1 = core
    rh, rw = result.shape
    kernel = np.ones((th, tw), dtype=np.uint8)
    win_min = cv2.erode(page, kernel, anchor=(0, 0), borderType=cv2.BORDER_REPLICATE)
    win_max = cv2.dilate(page, kernel, anchor=(0, 0), borderType=cv2.BORDER_REPLICATE)
    flat = (win_max[:rh, :rw].astype(np.int16) - win_min[:rh, :rw]) < _FLAT_WINDOW_RANGE
    del win_min, win_max
    mean = cv2.boxFilter(page, cv2.CV_64F, (tw, th), anchor=(0, 0), normalize=True,
                         borderType=cv2.BORDER_REPLICATE)[:rh, :rw]
    sq = cv2.sqrBoxFilter(page, cv2.CV_64F, (tw, th), anchor=(0, 0), normalize=True,
                          borderType=cv2.BORDER_REPLICATE)[:rh, :rw]
    flat |= (sq - mean * mean) < var_min
    del mean, sq

    finite = np.isfinite(result)
    out_of_range = finite & (result > 1.0 + _SCORE_TOLERANCE)
    bad = nonfinite + int(np.count_nonzero((out_of_range & ~flat)[cy0:cy1, cx0:cx1]))
    work = result.copy()
    work[flat | out_of_range | ~finite] = _EXCLUDED_SCORE
    np.minimum(work, 1.0, out=work)
    peak_kernel = np.ones((2 * ry + 1, 2 * rx + 1), dtype=np.uint8)
    neighbourhood_max = cv2.dilate(work, peak_kernel, borderType=cv2.BORDER_REPLICATE)
    peaks = (work >= threshold) & (work >= neighbourhood_max) & (work > _EXCLUDED_SCORE)
    core_mask = np.zeros_like(peaks)
    core_mask[cy0:cy1, cx0:cx1] = True
    ys, xs = np.nonzero(peaks & core_mask)
    return ys, xs, work[ys, xs].astype(np.float64), bad


def _flat_windows(
    page: np.ndarray,
    ys: np.ndarray,
    xs: np.ndarray,
    template_shape: Tuple[int, int],
    var_min: float,
) -> np.ndarray:
    """True where the page window at (y, x) is flat: grayscale range below
    ``_FLAT_WINDOW_RANGE`` or variance below ``var_min``. Windows are gathered
    in bounded chunks, so memory is independent of the page size."""
    th, tw = template_shape
    view = np.lib.stride_tricks.sliding_window_view(page, (th, tw))
    out = np.empty(len(ys), dtype=bool)
    chunk = max(1, _GATHER_CHUNK_ELEMENTS // (th * tw))
    for s in range(0, len(ys), chunk):
        e = min(len(ys), s + chunk)
        windows = view[ys[s:e], xs[s:e]].reshape(e - s, -1)
        value_range = windows.max(axis=1).astype(np.int16) - windows.min(axis=1)
        out[s:e] = (value_range < _FLAT_WINDOW_RANGE) | (windows.var(axis=1) < var_min)
    return out


def _empty_peaks(bad: int = 0) -> _Peaks:
    empty = np.empty(0, dtype=np.int64)
    return empty, empty, np.empty(0, dtype=np.float64), bad


def _concat(parts: List[_Peaks]) -> _Peaks:
    if not parts:
        return _empty_peaks()
    return (
        np.concatenate([p[0] for p in parts]).astype(np.int64),
        np.concatenate([p[1] for p in parts]).astype(np.int64),
        np.concatenate([p[2] for p in parts]).astype(np.float64),
        sum(p[3] for p in parts),
    )


# --- preprocessing and orientations --------------------------------------


def _blur_pad(sigma: float) -> int:
    # OpenCV's kernel radius for uint8 is round(3 * sigma); pad a little more.
    return int(math.ceil(4 * sigma)) + 1


def _border_median(gray: np.ndarray) -> int:
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    return int(np.median(border))


def _blur(gray: np.ndarray, sigma: float, background: int) -> np.ndarray:
    """Gaussian blur; pixels beyond the image are treated as ``background``
    (the paper colour), so a symbol touching the crop or page edge blurs the
    same way in page and template."""
    if sigma <= 0:
        return gray
    pad = _blur_pad(sigma)
    padded = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=background)
    # In place, to avoid a third page-sized buffer.
    cv2.GaussianBlur(padded, (0, 0), sigma, dst=padded, borderType=cv2.BORDER_REPLICATE)
    # A strided view: OpenCV and numpy accept it, and it saves a copy.
    return padded[pad:-pad, pad:-pad]


def _prepare_page(page: np.ndarray, region: BoundingBox, sigma: float) -> np.ndarray:
    """Grayscale + blur of the search region. The blur reads real page pixels
    around the region (so results do not depend on where the region is cut)
    and treats pixels beyond the page as background."""
    if sigma <= 0:
        return _to_gray(page[region.y : region.y2, region.x : region.x2])
    pad = _blur_pad(sigma)
    page_h, page_w = page.shape[:2]
    x0, y0 = max(0, region.x - pad), max(0, region.y - pad)
    x1, y1 = min(page_w, region.x2 + pad), min(page_h, region.y2 + pad)
    gray = _to_gray(page[y0:y1, x0:x1])
    blurred = _blur(gray, sigma, _border_median(gray))
    del gray
    return blurred[region.y - y0 : region.y2 - y0, region.x - x0 : region.x2 - x0]


def _orient(template: np.ndarray, orientation: Orientation) -> np.ndarray:
    """Mirror (horizontal flip) first, then rotate clockwise."""
    mirrored, rotation = orientation
    image = cv2.flip(template, 1) if mirrored else template
    if rotation:
        image = cv2.rotate(image, _CV_ROTATE[rotation])
    return np.ascontiguousarray(image)


def _canonical_orientations(
    orientations: Sequence[Orientation], oriented: Dict[Orientation, np.ndarray]
) -> Tuple[List[Orientation], List[SkippedOrientation]]:
    """Split orientations into those to search and those equivalent (by
    template symmetry) to an earlier one. ``orientations`` is in canonical
    order (unmirrored before mirrored, then ascending rotation), so each
    equivalence class is represented by its smallest label."""
    searched: List[Orientation] = []
    skipped: List[SkippedOrientation] = []
    for o in orientations:
        image = oriented[o]
        for k in searched:
            other = oriented[k]
            if other.shape != image.shape:
                continue
            similarity = float(cv2.matchTemplate(image, other, cv2.TM_CCOEFF_NORMED)[0, 0])
            if similarity >= _SYMMETRY_NCC:
                skipped.append(
                    SkippedOrientation(
                        rotation=o[1],
                        mirrored=o[0],
                        reported_rotation=k[1],
                        reported_mirrored=k[0],
                        similarity=similarity,
                    )
                )
                break
        else:
            searched.append(o)
    return searched, skipped


def _label(mirrored: bool, rotation: int) -> str:
    return f"{'mirrored ' if mirrored else ''}rotation {rotation}"


# --- validation -----------------------------------------------------------


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
