# Detection module (`pinny/detection/`)

Conventional OpenCV template matching: find every instance of one receptacle
symbol type on one canonical page raster.

## Interface

```python
from pinny.detection import OpenCVTemplateDetector, ScanSettings, Template, BoundingBox

detector = OpenCVTemplateDetector()               # satisfies the `Detector` protocol
template = Template.from_page_crop(page, BoundingBox(x, y, w, h))
result = detector.detect(page, template, ScanSettings())
for c in result.candidates:                       # best score first
    c.score, c.box.x, c.box.y, c.box.width, c.box.height, c.center, c.rotation, c.mirrored
result.skipped_orientations                       # orientations covered by symmetry
result.to_dict()                                  # JSON-serialisable
ScanSettings().to_dict()                          # for the scan's detector.settings
```

* `Detector` (`interface.py`) is a `typing.Protocol` with `name` and
  `detect(page, template, settings) -> DetectionResult`. Callers should
  depend on it and the types in `types.py`, not on OpenCV, so another
  implementation can replace `OpenCVTemplateDetector` later.
* `page`: `uint8` numpy array, `(H, W)` or `(H, W, 1|3|4)`, assumed RGB(A).
* `template`: a `Template` or a bare array with the page's channel count.
* Invalid input raises `DetectionError` with a stable `.code` and an
  actionable message. `DetectionTimeout` is a subclass (`code="timeout"`).

### v1.1 additions (all additive, defaults keep v1 output shapes)

* `Candidate.mirrored: bool = False`, included in `Candidate.to_dict()`.
* `DetectionResult.include_mirrored` and
  `DetectionResult.skipped_orientations` (tuple of `SkippedOrientation`),
  both included in `to_dict()`.
* New `ScanSettings` fields: `blur_sigma`, `include_mirrored`,
  `coarse_to_fine`, `coarse_slack`, `max_coarse_peaks` and `num_threads`
  (see Settings), plus `ScanSettings.to_dict()`.
* The default `blur_sigma=1.0` changes scores slightly compared with v1.
  Exact copies still score about 1.0. Off-grid, rescaled or crossed symbols
  score higher.

## Coordinates

All returned values are in the **original canonical page raster**, never in a
cropped or rotated intermediate. The `search_region` offset is added back.

* Origin top-left, `x` right, `y` down. Pixel `(i, j)` covers `[i, i+1) × [j, j+1)`.
* `box` = `(x, y, width, height)`, integer pixels, `x2 = x + width` exclusive.
* `center` = `(x + width/2, y + height/2)`, continuous.
* `rotation` ∈ {0, 90, 180, 270}: the clockwise quarter-turn applied to the
  template (in the y-down raster) to produce the match. At 90° and 270° the
  box's `width`/`height` are the template's `height`/`width`.
* `mirrored` (v1.1, default `False`): the template was flipped horizontally
  (left-right) **before** the clockwise rotation. So `mirrored=True,
  rotation=180` is a vertical flip of the template.
* For a template that is symmetric under some orientations, the reported
  label is the **canonical** one (see step 3 below). For example, a
  180°-symmetric symbol drawn upside down is reported as `rotation=0`.

## Algorithm

1. Validate settings, page, template and search region.
2. **Preprocess identically.** Page (the search-region crop) and template get
   the same grayscale conversion (`cv2.cvtColor` RGB→GRAY, uint8), then the
   same `cv2.GaussianBlur(ksize=(0, 0), sigma=blur_sigma)` (default 1.0;
   0 disables it). Pixels beyond the image are treated as paper: the image
   is padded with the median of its border pixels before blurring. The page
   blur reads real page pixels around the search region, so scores do not
   depend on where the region is cut. The blur makes the score tolerant of
   sub-pixel offsets, ±5 % scale and anti-aliasing (measurements below).
3. **Orientations.** Take the requested rotations, plus the same rotations of
   the horizontally flipped template when `include_mirrored` is set, in
   canonical order: unmirrored first, then ascending rotation. An
   orientation is **not searched** if its preprocessed template has
   normalized correlation ≥ 0.97 with an earlier *searched* orientation of
   the same shape. Those pairs are 180 vs 0 and 270 vs 90, plus 90 vs 0 and
   the like for square templates, and mirrored vs unmirrored. The equivalent
   orientation finds its matches, which are reported with that canonical
   label. Each skip is listed in `DetectionResult.skipped_orientations` and
   in `warnings`. `rotations_searched` still echoes the requested rotations.
4. **Search each orientation** with `cv2.matchTemplate(..., TM_CCOEFF_NORMED)`:
   * **Coarse-to-fine** runs when `coarse_to_fine=True` and the oriented
     template's shorter side is ≥ 24 px. Both blurred images are reduced
     with `cv2.pyrDown` and searched at half resolution with threshold
     `threshold − coarse_slack` (0.15). Coarse local maxima (radius ⅛ of the
     half-size template) are kept. Each coarse peak is refined with a
     full-resolution `matchTemplate` on a small ROI. The ROI covers ±4 px
     around the projected position, plus the local-maximum radius, so the
     normal peak test sees its whole neighbourhood. The normal threshold and
     validity checks apply. The orientation falls back to full resolution if
     there are more than `max_coarse_peaks` coarse peaks, or if refining them
     would cost more than a full search (about 2000 score-map pixels per
     peak).
   * **Full resolution** runs in horizontal strips of about 1 M score-map
     pixels. Each strip's page rows overlap the next by `template height − 1`,
     plus the local-maximum radius on both sides, so peaks at strip borders
     are tested against their whole neighbourhood. Results are identical to
     an untiled search (scores agree to ~1e-6 float rounding). Strips run on a
     `ThreadPoolExecutor` (OpenCV releases the GIL) with `num_threads`
     workers (0 = `min(4, os.cpu_count())`, 1 = calling thread only).
5. **Sparse validity and peaks.** Candidates are the scores ≥ threshold
   (NaN fails the comparison). These checks run only at those positions:
   * The page window is **flat** if its grayscale range is < 2, or if its
     variance is < 5 % of the preprocessed template's variance. Correlation
     is undefined or dominated by noise there, and OpenCV can return
     arbitrary values up to 1.0. The variance rule also rejects faint ghosts
     of the symbol and blur halos beside lines.
   * Scores > 1 + 1e-3 are faults. They are masked and reported in
     `warnings`. Non-finite scores anywhere are counted too, via a fast
     `cv2.checkRange`.
   * A candidate is kept if no valid candidate within ±(h/4, w/4) scores
     higher. This runs as a per-candidate loop, or as one dilation of a
     masked copy when there are many candidates.

   Very low thresholds can let more than max(50 000, 5 %) of a score map
   pass. The same checks then run as dense full-map passes.
6. Cap each orientation at `max_candidates_per_rotation` best. Mirrored
   orientations count separately.
7. Pool candidates from **all** orientations and apply greedy duplicate
   suppression. A lower-scored candidate is dropped if its IoU with a kept one
   is > `nms_iou_threshold` (0.5), **or** their centers are closer than
   `duplicate_center_ratio` (0.5) × the smaller short side. The center rule
   catches the same symbol matched at a different rotation or mirroring,
   whose box has a different shape and low IoU. Adjacent or touching symbols
   have IoU 0 and centers at least one short side apart, so they stay
   separate.
8. Keep at most `max_candidates`; sort by score, then y, then x.

The runtime deadline is checked **before** each orientation after the first
and before each strip after the first. It is never checked after the last
unit of work, so a scan that has finished always returns its results. One
OpenCV call is not interrupted.

## Score and threshold

The score is the normalized correlation coefficient in [-1, 1] between the
*blurred* page window and the *blurred* template. A pixel-perfect match
scores 1.0. **It is a matching score, not a calibrated probability.** It is
only meaningful for ranking candidates of one template on one page.

The default `threshold = 0.80` (`DEFAULT_SCORE_THRESHOLD`) is unchanged with
the blur. Synthetic measurements (`tests/detection/test_core_matching.py`,
the 24×36 test glyph rendered at 8× and area-downsampled):

| Case | `blur_sigma=0` | `blur_sigma=1` (default) |
|---|---|---|
| Worst of 16 sub-pixel offsets (¼ px steps) | 0.859 | 0.947 |
| Symbol scaled 0.95 / 1.05 | 0.821 / 0.829 | 0.933 / 0.933 |
| 1 px horizontal wire across the symbol | 0.942 | 0.973 |
| 1 px anti-aliased diagonal wire | 0.918 | 0.941 |
| 2 px wire along the symbol's axis | 0.765 | 0.767 |
| Max false score, generic clutter (lines, text, circles, boxes; 20 pages) | 0.698 | 0.726 |
| Max false score, near-variant symbols (circle + stem only, circle + one prong) | 0.783 | 0.785 |
| Mirror image of the glyph, with `include_mirrored=False` | 0.845 | 0.817 |

The margin under 0.80 is about 0.07 for generic clutter and only about
0.015 for near-variant symbols. A mirror image of an asymmetric symbol
scores **above** 0.80 as the unmirrored symbol. The review's suggested 0.85
was not adopted: it would eat into the ±5 % scale margin (0.93) and would
drop the heavily cluttered symbols described next. With blur, symbols heavily
overlapped by text or thick parallel lines lose about 0.01–0.02. On the
benchmark page, 5 of 300 such symbols fell from 0.80–0.82 to 0.78–0.80.
Tune against the evaluation set; nothing here has been validated on real
drawings.

Thresholds well below the default (about ≤ 0.4) can report weak partial
matches offset from a real symbol. These overlap it too little to count as
duplicates.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `threshold` | 0.80 | Minimum score to report |
| `rotations` | (0, 90, 180, 270) | Quarter turns to search |
| `search_region` | `None` | Page-space sub-region; results stay in page coordinates |
| `nms_iou_threshold` / `duplicate_center_ratio` | 0.5 / 0.5 | Duplicate suppression |
| `blur_sigma` | 1.0 | Gaussian σ (px) on page and template; 0 disables; max 10 |
| `include_mirrored` | `False` | Also search the left-right flipped template |
| `coarse_to_fine` | `True` | Half-resolution pre-pass for templates ≥ 24 px |
| `coarse_slack` | 0.15 | Coarse pass keeps peaks ≥ `threshold − coarse_slack` |
| `max_coarse_peaks` | 5000 | More coarse peaks than this → full-resolution fallback |
| `num_threads` | 0 | Strip workers; 0 = `min(4, cpus)`, 1 = no threads |
| `max_page_pixels` | 120,000,000 | Rejects oversized search areas before allocation. Covers Arch E (48×36 in = 9600×7200 px) and oversize sheets up to about 60×42 in at 200 DPI; a 60×42 in page measured ~650 MB peak RSS including the page itself |
| `min_template_side` / `max_template_side` | 8 / 1024 px | Rejects tiny or oversized templates |
| `min_template_stddev` | 4.0 (0–255 scale) | Rejects blank or near-uniform templates (measured before blur) |
| `max_candidates_per_rotation` | 2000 | Bounds pre-suppression work, per orientation |
| `max_candidates` | 500 | Bounds output; sets `truncated=True` and a warning |
| `max_runtime_seconds` | 60 | Checked before each orientation and strip after the first |

## Runtime and memory

The benchmark is a synthetic 7200×4800 RGB page (Arch D at 200 DPI) with 300
glyphs, 400 long lines and 300 text labels. It uses the 24×36 template and
all four rotations, on a 4-core development container shared with other
jobs. Times are the best of 3 runs. Memory is peak RSS above the ~190 MB
the inputs take.

| Configuration | Time | Peak memory |
|---|---|---|
| Before (dense validity passes, no blur, untiled) | 8.0–9.3 s | +664 MB |
| Sparse validity only (`blur_sigma=0`, untiled, 1 thread, no coarse pass) | 5.0 s | +664 MB |
| + blur and strips, 1 thread, no coarse pass | 5.0 s | +97 MB |
| + 4 threads, no coarse pass | 1.8 s | +116 MB |
| Coarse-to-fine, 1 thread | 1.5 s | +85 MB |
| **Default** (blur, coarse-to-fine, strips, 4 threads) | **0.7 s** | **+100 MB** |

* Sparse validity saves about 40 % of the old runtime. The old full-map
  erode, dilate and mask passes took more than 3 s of it.
* An untiled `matchTemplate` still needs about 660 MB, mostly OpenCV's DFT
  buffers (roughly 20 bytes per score-map pixel). Strips cap that at about
  25 MB per worker.
* On this page the coarse-to-fine and full-resolution paths returned
  identical detections: 283 at threshold 0.80 and 298 at 0.60, with scores
  within 3e-6.
* Steady-state memory is the grayscale page, its blurred copy and a
  quarter-size copy, about 1.3 bytes per page pixel.

## Limitations

* No scale search. The blur gives about ±5 % scale tolerance, so the
  template must come from a raster at about the same resolution as the page.
* Quarter-turn rotations and mirroring only. There is no arbitrary-angle
  search.
* A mirror image of an asymmetric symbol scores about 0.82 against the
  unmirrored template even with `include_mirrored=False`, so it is usually
  reported as unmirrored. With `include_mirrored=True` it gets the right
  label.
* Symmetry skipping treats orientations with similarity ≥ 0.97 as equal. A
  nearly symmetric symbol (for example one with a small asymmetric tick)
  loses up to ~3 % score at the skipped orientation and gets the canonical
  label.
* Coarse-to-fine equivalence is tested on synthetic pages but not
  guaranteed. A full-resolution peak whose half-resolution score is more
  than `coarse_slack` lower would be missed. Set `coarse_to_fine=False` for
  an exhaustive search.
* The deadline is not checked inside one OpenCV call, or during the
  coarse-to-fine refinement of one orientation.
* No YOLO or other learned detector, no OCR, no training.
* Symbols partly outside the page (or search region) are not detected. The
  whole rotated template must fit.
* One symbol type per call. Different templates' scores are not comparable.
* Synthetic tests prove implementation behaviour (geometry, rotation,
  suppression, validation, path equivalence), not real-world accuracy.

## Tests

```
python -m pytest tests/detection
```

Requires `numpy`, `opencv-python-headless` (or `opencv-python`) and `pytest`.

* `test_opencv_matcher.py`: geometry, rotations, suppression and validation
  (v1 behaviour).
* `test_core_matching.py`: blur gains, the clutter margin, sparse vs dense
  validity, the variance gate, symmetry, mirroring and the new settings.
* `test_core_search.py`: coarse-to-fine vs full-resolution equivalence,
  strips vs untiled, thread count, the deadline regression.
