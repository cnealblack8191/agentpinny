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

* **Point / pin**: `x`, `y` as floats in canonical px. Pins are continuous
  points and are valid anywhere in the closed range `[0, width] × [0, height]`,
  edges included.
* **Box**: `{x, y, width, height}`. These are integers for detector output
  and may be floats elsewhere. The right edge is exclusive (`x2 = x + width`).
  This matches `pinny.detection.BoundingBox`. Modules that need
  `(x0, y0, x1, y1)` internally convert at their own boundary.
* **Detection center** = `(x + width/2, y + height/2)`. A machine pin's
  point is its detection center.
* **Rotation** ∈ {0, 90, 180, 270}. It is the clockwise quarter-turn, in
  the y-down raster, applied to the template to produce the match. This is
  the convention in `docs/detection.md`. *v1.1 additive:* an optional
  boolean `mirrored` (default `false`) means the template was flipped
  horizontally (left-right) **before** that clockwise rotation.

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
      "score": 0.93, "rotation": 0, "mirrored": false, "source": "detector" }
  ]
}
```

* `mirrored` is *v1.1 additive* and optional. Readers treat a missing value
  as `false`.
* *v1.1 additive:* `template.template_id` (optional) names a template
  registered in the learning store. `detector.name` may be
  `"opencv-template"` (raster) or `"vector"` (`pinny/vector`, see
  `docs/vector-matching.md`). Vector detections use `source`
  `"vector-xobject"` or `"vector-path"` and may carry `angle` for a
  non-quarter-turn placement.

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
* `reviewer` is recorded but is **not** part of a request's identity: a
  retry with the same `request_id` replays even if the reviewer changed.
  Re-approving an approved pin, or re-rejecting a rejected one, is a no-op.
* *v1.1 additive:* pins may carry `class_label` (for example `duplex`,
  `gfci`), and manual pins may carry an optional `box` and `rotation`.
* *v1.1 additive:* a page can be marked review-`complete`. Only complete
  pages are safe for training or as background in dataset export.
* `source` records which surface issued the action (`"viewer"`, `"cli"`,
  `"test"`).
* **Rescans:** a new scan starts entirely `unreviewed`. Reviews are never
  copied across scans. The viewer may show a *hint* on a new pin that lies
  within the evaluation tolerance of an approved or rejected pin from an
  earlier scan of the same `canonical_page_id` and template. Accepting the
  hint issues a normal `approve` or `reject`, which logs a real review event.
* A corrected pin set is **never** a detections file. The evaluator
  rejects it.

## 5a. Batch scans (*v1.2 additive*)

A batch is one "scan every page" request: one template, many pages, one
ordinary scan per page. It changes nothing in sections 4 or 5.

* **Identity.** `batch_id` is derived from the client's `request_id`
  (uuid5), so a retried request returns the same batch. The same
  `request_id` with different arguments is refused (`request_conflict`).
  Each page's scan id is derived from `batch_id` and `page_index`.
* **Template.** The box is drawn on `template_page_index` and matched,
  pixel for pixel, on every page in the batch. The page scans' `template`
  object (section 4) gains `page_index`, the page it was cut from. It is
  present on batch scans and on single scans that name a template page;
  otherwise the template came from the scanned page itself. A different
  drawing scale on another sheet needs its own batch (exact scale only,
  see `docs/detection.md`).
* **Pages.** A batch lists its pages in request order. Each page is
  `pending` → `running` → `done` (with its `scan_id`) or `failed` (with an
  error `code` and `message`), or `skipped` if the batch was cancelled
  first. One failed page never stops the others. Pages left `running` by a
  crash or shutdown go back to `pending` when the batch is resumed.
* **Status** is derived from the pages: `queued` (nothing started),
  `running`, `complete`, or `cancelled` (cancelled while pages were still
  pending).
* **Scan result.** A page scan carries `"batch": {"batch_id", "page_index"}`.
  It is recorded when its page finishes, so review can start before the
  batch completes.
* **Review.** Pins are reviewed with the normal section 5 actions on the
  page's own scan, so labels and training crops come out exactly as for
  single-page review. The batch review queue lists the unreviewed machine
  pins of every finished page, most uncertain first (the learning store's
  `margin` or `lowest_score` order), each with its `scan_id`, `pin_id` and
  pin `version`.

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
* *v1.1 additive:* the crop spec carries `renderer_version` (default
  `"unknown"`), and it is part of `crop_key`. The render service should
  expose its version so a renderer change produces new crops.

## 6a. Splits and dataset export (*v1.1 additive*)

* Splits are per **document** (`train` | `val` | `test` | `eval`), never per
  page, so near-identical sheets can't leak between training and
  evaluation. `test` and `eval` documents are excluded from training
  exports, review stats and template-bank crops unless explicitly
  requested.
* `export_dataset` writes COCO JSON plus a manifest for review-complete
  pages only. Images are referenced by `canonical_page_id` and supplied by
  the render service (section 9).

## 7. Errors

* All module errors subclass `pinny.errors.PinnyError(code: str, message: str)`.
  The `code` is a stable snake_case id and the message is actionable. The
  foundation creates `pinny/errors.py`. Until it lands, the detection
  module's `DetectionError(code, message)` is the reference shape.
* HTTP/API layers map a `PinnyError` to 4xx with
  `{"error": {"code", "message"}}`, and anything else to 500.
* Learning store: a store instance used from a thread other than the one
  that opened it raises `wrong_thread`. Open one store per thread.

## 8. Package layout

```
pinny/                 application package (one import root)
  errors.py            foundation
  pdf/ or render/      foundation: upload, versioning, page render, crop_renderer
  detection/           detection session
  learning/            learning store (moved from top-level pinny_learning/)
  vector/              vector-first PDF symbol matcher (pikepdf)
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
Detection needs `numpy` and `opencv-python-headless`. The vector matcher
(`pinny.vector`) needs `pikepdf` (MPL-2.0); its tests also use `pypdfium2`.
`onnxruntime` is optional (the ONNX embedding verifier, the `onnx` extra).
Dev deps include `pytest`. Avoid AGPL dependencies (PyMuPDF, Ultralytics).

## 9. Render service interface (`pinny.render.RenderService`)

This is the foundation's real service; see `docs/render-service.md`. Every
consumer calls it directly. There are no parallel stubs.

| Call | Returns |
|---|---|
| `ingest_pdf(...)` | `DocumentVersion`. Upload and versioning, content-addressed, bounded streaming |
| `get_version(document_version)` | `DocumentVersion` |
| `list_versions(document_id=None)` | `list[DocumentVersion]` |
| `page_frame(document_version, page_index)` | the frame descriptor in section 2 |
| `render_page(document_version, page_index)` | uint8 RGB `(H, W, 3)` canonical raster, cached per page |
| `render_page_png(document_version, page_index)` | PNG bytes of that raster, cached (for the browser) |
| `crop_renderer(spec)` | PNG bytes of `spec.pixel_box`, cut from the cached raster |

Detection and learning need nothing beyond `render_page` and `crop_renderer`.
