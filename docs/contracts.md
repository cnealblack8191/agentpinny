# Pinny shared contracts (v1)

This is the single source of truth for everything that crosses a module
boundary. Modules may keep private types. Anything another module reads or
writes must follow this file. To change it, open a PR that edits this file
and tell the coordinator. Don't change a shared convention only in your
own module.

Status: **v1, set by the coordinator on 2026-09-23.** It reconciles the
provisional conventions in `pinny/detection` (detection), `pinny_learning`
(learning store) and `evaluation/` (evaluator). The foundation session
owns the PDF/render service and may fill in the sections marked
*foundation*. It must not change the conventions in sections 1–5 without
a coordinator-approved PR.

## 1. Document and page identity

| Field | Type | Definition |
|---|---|---|
| `document_id` | str | Stable id assigned at first upload (uuid4 string). Survives re-uploads of new versions. |
| `document_version` | str | `"sha256:" + hex(sha256(uploaded file bytes))`. Same bytes give the same version. |
| `page_index` | int | 0-based page index within that version. |
| `canonical_page_id` | str | `f"{document_version}#p{page_index}"`. Derived, never assigned. |

The learning store uses the field name `document_version` (renamed from its provisional `document_version_id`).

## 2. Canonical page raster (the coordinate frame)

Every shared coordinate is in **canonical raster pixels**.

* The canonical raster is the page rendered at **`CANONICAL_DPI = 200`**
  after the PDF `/Rotate` is applied, so it matches what a viewer displays
  upright. The render service (*foundation*) is the only code that produces
  it.
* Its size is `width_px = ceil(page_width_pt * 200 / 72)` and
  `height_px = ceil(page_height_pt * 200 / 72)`. The size is recorded
  with every scan and export.
* The origin is the top-left corner, `x` grows right and `y` grows down.
  Pixel `(i, j)` covers `[i, i+1) × [j, j+1)`.
* Pixels are `uint8` **RGB** arrays with shape `(H, W, 3)` and no alpha.
  The detector still accepts 1/3/4 channels, but the app always passes RGB.
* PDF points appear only inside the render service. Convert with
  `px = pt * 200 / 72` there. No other module sees points.

The frame descriptor used in files:

```json
{ "space": "canonical_raster_px", "dpi": 200, "width": 7200, "height": 4800,
  "origin": "top-left", "y_axis": "down" }
```

## 3. Geometry

* **Point / pin**: `x`, `y` as floats in canonical px. Pins are points.
* **Box**: `{x, y, width, height}`. These are integers for detector output
  and may be floats elsewhere. The right edge is exclusive (`x2 = x + width`).
  This matches `pinny.detection.BoundingBox`. Modules that need
  `(x0, y0, x1, y1)` internally convert at their own boundary.
* **Detection center** = `(x + width/2, y + height/2)`. A machine pin's
  point is its detection center.
* **Rotation** ∈ {0, 90, 180, 270}. It is the clockwise quarter-turn, in
  the y-down raster, applied to the template to produce the match. This is
  the convention in `docs/detection.md`.

## 4. Scan result (original detector output)

A scan is immutable once written. Corrections never modify it.

```json
{
  "scan_id": "uuid4",
  "document": { "document_id": "...", "document_version": "sha256:...", "page_index": 0 },
  "coordinate_frame": { "space": "canonical_raster_px", "dpi": 200, "width": 7200, "height": 4800, "origin": "top-left", "y_axis": "down" },
  "template": { "box": {"x":0,"y":0,"width":40,"height":40}, "sha256": "hex of template pixels" },
  "detector": { "name": "opencv-template", "version": "git:<sha>", "settings": { "...": "ScanSettings as dict" } },
  "created_at": "RFC3339 UTC",
  "detections": [
    { "id": "det-<n>", "box": {"x":..,"y":..,"width":..,"height":..}, "x": 0.0, "y": 0.0,
      "score": 0.93, "rotation": 0, "source": "detector" }
  ]
}
```

* `score` is the raw matching score, **not** a probability. Evaluator
  exports copy it into `confidence`.
* The export for the evaluator is exactly `pinny.detections` v1 (see
  `evaluation/README.md`). It is this object plus `"format"`,
  `"format_version": 1` and `"provenance": "original_detector_output"`, with
  `document_version` and the frame copied through.

## 5. Pins and review

* Pin `origin`: `machine` (from a detection) or `manual`.
* Pin `state`: `unreviewed` | `approved` | `rejected` | `added` | `removed`.
* Review actions: `approve`, `reject`, `add_manual`, `remove_manual`,
  `delete_pin`. Every mutating call carries a client-generated `request_id`
  (uuid4) for idempotency and may carry `expected_version` to detect
  concurrent edits.
* `reviewer` is `$PINNY_REVIEWER`, then the OS user, then `null`. This is a
  single-user local app for v1 with no auth. It uses
  `pinny.learning.store.local_reviewer_identity()`.
* `source` records which surface issued the action (`"viewer"`, `"cli"`,
  `"test"`).
* A corrected pin set is **never** a detections file. The evaluator
  rejects it.

## 6. Training crops

These are defined in canonical px (spec v2, which replaces the learning
store's provisional v1 in PDF points):

* Manual pin: a **128 × 128 px** square centred on the pin.
* Machine detection: the detection box plus a **24 px** margin on every
  side.
* Both are clipped to the raster, with the per-edge `clipped_*` flags and
  the `unclipped_box` recorded.
* Crops are cut from the canonical raster at 200 DPI with no re-render.
  The foundation's render service supplies
  `crop_renderer(spec) -> PNG bytes`.

## 7. Errors

* All module errors subclass `pinny.errors.PinnyError(code: str, message: str)`.
  The `code` is a stable snake_case id and the message is actionable. The
  foundation creates `pinny/errors.py`. Until it lands, the detection
  module's `DetectionError(code, message)` is the reference shape.
* HTTP/API layers map a `PinnyError` to 4xx with
  `{"error": {"code", "message"}}`, and anything else to 500.

## 8. Package layout

```
pinny/                 application package (one import root)
  errors.py            foundation
  pdf/ or render/      foundation: upload, versioning, page render, crop_renderer
  detection/           detection session
  learning/            learning store (moved from top-level pinny_learning/)
  viewer/ (+ web/)     viewer session
evaluation/            standalone, stdlib-only, must not import pinny
tests/<module>/        per-module tests
docs/                  contracts.md, agent-ownership.md, per-module docs
```

Tests: **pytest** is the single runner (`python -m pytest`). Every `tests/<module>/`
directory has an `__init__.py`. unittest-style tests are fine; pytest runs them.
`evaluation/` keeps its own stdlib `unittest` suite.

Dependencies are declared once in the root `pyproject.toml`, which the
foundation owns. Other sessions ask the coordinator for additions.
Detection needs `numpy` and `opencv-python-headless`; dev deps include `pytest`.

## 9. Render service interface (*foundation* fills in)

At minimum:

* `render_page(document_version, page_index) -> np.ndarray` returns the
  canonical RGB raster described in section 2.
* `page_frame(document_version, page_index) -> dict` returns the frame
  descriptor described in section 2.
* `crop_renderer(spec) -> bytes` returns the PNG crop described in
  section 6.
