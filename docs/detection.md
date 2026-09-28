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
    c.score, c.box.x, c.box.y, c.box.width, c.box.height, c.center, c.rotation, c.scale
result.to_dict()                                  # JSON-serialisable

# Symbols drawn up to 20% smaller or 25% larger than the template:
result = detector.detect(page, template, ScanSettings(scales=scale_range(0.8, 1.25)))
```

* `Detector` (`interface.py`) is a `typing.Protocol` with `name` and
  `detect(page, template, settings) -> DetectionResult`. Callers should
  depend on it and the types in `types.py`, not on OpenCV, so another
  implementation can replace `OpenCVTemplateDetector` later.
* `page`: `uint8` numpy array, `(H, W)` or `(H, W, 1|3|4)`, assumed RGB(A).
* `template`: a `Template` or a bare array with the page's channel count.
* Invalid input raises `DetectionError` with a stable `.code` and an
  actionable message. `DetectionTimeout` is a subclass (`code="timeout"`).

## Coordinates

All returned values are in the **original canonical page raster**, never in a
cropped or rotated intermediate. The `search_region` offset is added back.

* Origin top-left, `x` right, `y` down. Pixel `(i, j)` covers `[i, i+1) × [j, j+1)`.
* `box` = `(x, y, width, height)`, integer pixels, `x2 = x + width` exclusive.
* `center` = `(x + width/2, y + height/2)`, continuous.
* `rotation` ∈ {0, 90, 180, 270}: the clockwise quarter-turn applied to the
  template (in the y-down raster) to produce the match. At 90° and 270° the
  box's `width`/`height` are the template's `height`/`width`.
* `scale`: the template scale factor that produced the match (1.0 = the
  template as cropped). The box is the scaled template's footprint, so its
  size is `round(template side * scale)`, swapped at 90°/270°.

## Algorithm

1. Validate settings, page, template and search region.
2. Convert page (the search-region crop) and template with the **same**
   grayscale conversion (`cv2.cvtColor` RGB→GRAY, uint8). No other
   preprocessing.
3. For each requested scale, resize the template (`INTER_AREA` when
   shrinking, `INTER_LINEAR` when enlarging). The page is never resized, so
   coordinates stay in canonical px. Scales that round to the same template
   size are searched once, keeping the scale closest to 1.0, with a warning.
   Then for each requested rotation, `cv2.rotate` the scaled template and run
   `cv2.matchTemplate(..., TM_CCOEFF_NORMED)`.
4. Mask page windows that are flat (grayscale range < 2). Correlation is
   undefined there and OpenCV can return arbitrary values, including 1.0.
   Non-finite or out-of-range scores are also masked and reported in `warnings`.
5. Take local maxima ≥ `threshold` (neighbourhood ≈ half the template),
   capped at `max_candidates_per_rotation` best.
6. Pool candidates from **all** scales and rotations and apply greedy duplicate
   suppression. A lower-scored candidate is dropped if its IoU with a kept one
   is > `nms_iou_threshold` (0.5), **or** their centers are closer than
   `duplicate_center_ratio` (0.5) × the smaller short side. The center rule
   catches the same symbol matched at a different rotation, whose box has a
   different shape and low IoU. The same symbol matched at neighbouring scales
   overlaps heavily and keeps only its best-scoring scale. Adjacent or touching symbols have IoU 0 and
   centers at least one short side apart, so they stay separate.
7. Keep at most `max_candidates`; sort by score, then y, then x.

## Score and threshold

The score is the normalized correlation coefficient in [-1, 1]. A
pixel-perfect match scores 1.0. **It is a matching score, not a calibrated
probability.** It is only meaningful for ranking candidates of one template
on one page.

Initial default `threshold = 0.80` (`DEFAULT_SCORE_THRESHOLD`). In synthetic
tests, a symbol matched against the wrong quarter-turn of itself scores about
0.6–0.7, so 0.80 separates those cleanly. Real drawings (anti-aliasing,
overlapping line work, rendering differences) have not been evaluated. Tune
the threshold against the evaluation set.

Thresholds well below the default (about ≤ 0.4) can report weak partial
matches offset from a real symbol. These overlap it too little to count as
duplicates.

## Scale search

`ScanSettings.scales` is a tuple of template scale factors in
[`MIN_SCALE`, `MAX_SCALE`] = [0.25, 4.0]. The default `(1.0,)` is the
original exact-scale search, so existing callers are unchanged.
`scale_range(low, high, step=DEFAULT_SCALE_STEP)` builds a geometric range
that always contains 1.0 and both ends, with neighbours at most `step` apart.

Why `DEFAULT_SCALE_STEP = 1.04`: on synthetic line-art symbols redrawn at a
different size with a fixed 2 px line weight, the score for a size mismatch
was about 0.93 at 2%, 0.89–0.91 at 4%, 0.81–0.83 at 6% and 0.58–0.80 at 10%.
A 4% step keeps every size within about 2% of a searched scale, which stays
well above the 0.80 threshold. A wider step saves time but loses matches on
imperfect real drawings sooner.

In the synthetic stress test (40 symbols per page, threshold 0.80), exact
scale found none of the symbols drawn at 0.85, 0.90, 1.10 or 1.20×.
`scale_range(0.8, 1.25)` found all 40 at every size, and still did not match
a different symbol (quad receptacle). A different line weight at the same
size is still missed; the scale search does not address that.

**Cost** grows linearly with the number of scales: each scale is a full pass
per rotation. On a blank 7200×4800 page with a 40 px template and four
rotations, one scale took about 9 s and `scale_range(0.8, 1.25)` (13 scales)
about 87 s, which is above the default `max_runtime_seconds = 60`. For
full-sheet multi-scale scans, narrow the range when the drawing scale is
known, use a `search_region`, drop rotations that can't occur, or raise
`max_runtime_seconds`. The deadline is checked after every
(scale, rotation) pass.

## Bounds

| Setting | Default | Purpose |
|---|---|---|
| `max_page_pixels` | 60,000,000 | Rejects oversized search areas before allocation |
| `min_template_side` / `max_template_side` | 8 / 1024 px | Rejects tiny or oversized templates |
| `min_template_stddev` | 4.0 (0–255 scale) | Rejects blank or near-uniform templates |
| `max_candidates_per_rotation` | 2000 | Bounds pre-suppression work, per (scale, rotation) pass |
| `max_candidates` | 500 | Bounds output; sets `truncated=True` and a warning |
| `max_runtime_seconds` | 60 | Checked after each (scale, rotation) pass; one OpenCV call is not interrupted |

Memory is roughly 13–14 bytes per search-area pixel at peak. The page is
converted to grayscale once. Each rotation then allocates a float32 score
map, a float32 local-max map, two uint8 min/max window maps and boolean masks.
On a 7200×4800 synthetic page (Arch D at 200 DPI), all four rotations took
about 11 s with a peak RSS of about 760 MB on the development container.

## Limitations (this phase)

* Discrete scales only: sizes between searched scales rely on the score's
  tolerance (see "Scale search"). Scales are the same in x and y, so a
  symbol stretched on one axis only is not matched. Every scaled template
  must be at least `min_template_side`, so a small template can't be shrunk
  far.
* Quarter-turn rotations only: no arbitrary-angle search. Mirrored symbols are
  not matched.
* No YOLO or other learned detector, no OCR, no training.
* Symbols partly outside the page (or search region) are not detected. The
  whole rotated template must fit.
* One symbol type per call. Different templates' scores are not comparable.
* Synthetic tests prove implementation behaviour (geometry, rotation,
  suppression, validation), not real-world accuracy.

## Tests

```
python -m pytest tests/detection
```

Requires `numpy`, `opencv-python-headless` (or `opencv-python`) and `pytest`.
