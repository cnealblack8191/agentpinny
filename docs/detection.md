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
    c.score, c.box.x, c.box.y, c.box.width, c.box.height, c.center, c.rotation
result.to_dict()                                  # JSON-serialisable
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

## Algorithm

1. Validate settings, page, template and search region.
2. Convert page (the search-region crop) and template with the **same**
   grayscale conversion (`cv2.cvtColor` RGB→GRAY, uint8). No other
   preprocessing.
3. For each requested rotation, `cv2.rotate` the template and run
   `cv2.matchTemplate(..., TM_CCOEFF_NORMED)`.
4. Mask page windows that are flat (grayscale range < 2). Correlation is
   undefined there and OpenCV can return arbitrary values, including 1.0.
   Non-finite or out-of-range scores are also masked and reported in `warnings`.
5. Take local maxima ≥ `threshold` (neighbourhood ≈ half the template),
   capped at `max_candidates_per_rotation` best.
6. Pool candidates from **all** rotations and apply greedy duplicate
   suppression. A lower-scored candidate is dropped if its IoU with a kept one
   is > `nms_iou_threshold` (0.5), **or** their centers are closer than
   `duplicate_center_ratio` (0.5) × the smaller short side. The center rule
   catches the same symbol matched at a different rotation, whose box has a
   different shape and low IoU. Adjacent or touching symbols have IoU 0 and
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

## Bounds

| Setting | Default | Purpose |
|---|---|---|
| `max_page_pixels` | 60,000,000 | Rejects oversized search areas before allocation |
| `min_template_side` / `max_template_side` | 8 / 1024 px | Rejects tiny or oversized templates |
| `min_template_stddev` | 4.0 (0–255 scale) | Rejects blank or near-uniform templates |
| `max_candidates_per_rotation` | 2000 | Bounds pre-suppression work |
| `max_candidates` | 500 | Bounds output; sets `truncated=True` and a warning |
| `max_runtime_seconds` | 60 | Checked after each rotation; one OpenCV call is not interrupted |

Memory is roughly 13–14 bytes per search-area pixel at peak. The page is
converted to grayscale once. Each rotation then allocates a float32 score
map, a float32 local-max map, two uint8 min/max window maps and boolean masks.
On a 7200×4800 synthetic page (Arch D at 200 DPI), all four rotations took
about 11 s with a peak RSS of about 760 MB on the development container.

## Limitations (this phase)

* Exact scale only: no scale search. The template must be cropped from a
  raster at the same resolution as the page being searched.
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
