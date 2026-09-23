# Viewer and pin review (`pinny/viewer/`, `web/`)

A plain local web app for one reviewer:
upload a PDF → choose a page → drag a box around one receptacle → scan →
approve, reject or add pins → export the report.

```
python -m pinny.viewer [--data-dir DIR] [--port 8765]
# open http://127.0.0.1:8765/
```

Data goes to `$PINNY_DATA_DIR` (default `~/.local/share/pinny`), never the repo.

> **Stub render service.** Until the foundation pushes `pinny.render` /
> `pinny.pdf`, the viewer uses `pinny/viewer/render_stub.py`. It reads page
> sizes and `/Rotate` from the PDF but draws a **synthetic** raster (grid,
> red alignment markers, fake receptacle symbols). The UI shows a yellow
> banner and every frame carries `"render_service": "stub"`. See
> "Integration" below for how to switch it.

## Using it

| Mode (key) | Left-drag | Left-click |
|---|---|---|
| Pan / pick pin (V) | pans | selects the nearest pin within 10 px |
| Template box (T) | draws the template box | nothing |
| Add pin (P) | nothing | adds a manual pin (inside the page only) |

In every mode, the wheel zooms about the cursor, and Space+drag or a
middle-button drag pans. So a pan can never place a pin or change the
template.
Other keys: `+`/`-` zoom, `0` fit, `R` rotate the view 90°, `H` hide pins,
`A` approve, `X`/`Delete` reject or delete, `N` next unreviewed, `Esc` cancel
or deselect.

Pin colours: orange = unreviewed, green = approved, magenta = added
manually, grey × = rejected, grey dashed = removed. A dashed ring means
the change is still saving, and a red outer ring means it did not save.
Rejected and removed pins are hidden unless you tick "Show rejected and
removed pins".

## Coordinates

* Everything stored or sent is in **canonical raster px** (contracts §2):
  the template box as whole pixels, and pins as floats. The frame comes from
  `page_frame()`, and the viewer refuses a raster whose size differs from it.
* A single view matrix `s = R(rot)·zoom·p + pan` (`web/transform.js`) draws
  the page image *and* every overlay. Pointer positions go back through its
  exact inverse. Pins are re-projected on every frame, so they stay aligned
  after pan, zoom, rotation, a viewport resize or a DPR change.
* The canvas backing store is `round(css × devicePixelRatio)`, and the
  drawing scale uses the exact ratio `canvas.width / cssWidth`.
* Template boxes are clipped to the page and snapped outward to whole
  pixels. Manual pins are clamped to `[0, width] × [0, height]`, and the
  server enforces the same bounds.
* View, document, page and scan are kept in the URL hash, so a reload
  restores the same view. Pins reload from the store.

## Saving (contracts §5)

* Every action is queued in `localStorage` with a fresh `request_id`, then
  sent in order, one at a time. `expected_version` is sent when known.
* The status line shows "Saving N edits…", "All changes saved" or
  "N edits not saved". Network and 5xx errors are retried 3 times with
  backoff. After that, and for any 4xx, the edit stays in the list with
  **Retry** / **Discard**, and a 409/404 reloads the scan. An edit leaves the
  queue only when the server confirms it or the reviewer discards it.
* Edits survive a reload. One that was in flight is re-sent with the same
  `request_id`, and the store applies it at most once.
* "Delete" sends `delete_pin`: a machine pin becomes `rejected`, a manual pin
  becomes `removed`. Approve applies to machine pins only.
* Report export and rescanning are disabled while this scan has unsaved or
  failed edits.

## Stale responses

Page loads, scan loads and scan requests each carry a sequence number. A
response is applied only if its number is still current **and** its
document version, page index and frame size match what is on screen. So a
slow scan from a page you left, or an older scan picked from the list, never
replaces the current view. The slow scan is still saved, and appears in
that page's scan list. Pin lists are merged by `version`, so a stale
reload can't roll back a saved edit.

## Rescanning

A scan is immutable (contracts §4). Scanning again creates a **new** scan
with new unreviewed pins. The previous scan and all its reviews stay in the
store unchanged and can be reopened from the Scan list; the UI asks for
confirmation when the current scan has reviewed pins. Reviews are **not**
copied onto the new scan (see "Contract gaps").

## HTTP API (local, viewer-internal)

| Route | Purpose |
|---|---|
| `POST /api/documents?filename=` (PDF body) | upload; returns `document_id`, `document_version`, `page_count` |
| `GET /api/documents` | uploaded documents |
| `GET /api/documents/{version}/pages/{i}/frame` | frame descriptor (§2) |
| `GET /api/documents/{version}/pages/{i}/raster.png` | canonical raster |
| `GET /api/documents/{version}/pages/{i}/scans` | scans of the page, oldest first |
| `POST /api/scans` `{document_version, page_index, template_box, request_id, threshold?}` | run and record a scan; the scan id is derived from `request_id`, so a retry returns the same scan |
| `GET /api/scans/{id}` | scan plus current pins |
| `POST /api/scans/{id}/actions` `{action, request_id, pin_id?, x?, y?, expected_version?}` | review action |
| `GET /api/scans/{id}/report` | report download |

Errors are `{"error": {"code", "message"}}` (§7).

The report (`pinny.viewer.report` v1) has two separate parts:
`original_detections` is exactly `pinny.detections` v1 plus
`provenance: original_detector_output`, and the evaluator's `pending`
accepts it. `corrected_pins` has `provenance: reviewed_corrections`, all
pins with their states, and `final_pins` (approved + added). Both parts
carry the document identity and the frame.

## Tests

```
python -m pytest tests/viewer                       # service + HTTP (21)
node --test tests/viewer/test_transform.mjs tests/viewer/test_edits.mjs   # (15)
node tests/viewer/browser_e2e.mjs                   # Chromium end-to-end (90 checks)
```

`browser_e2e.mjs` runs the real server and measures the pixels drawn on the
canvas. It checks red raster markers against the transform, and the magenta
pin rings against the red markers, independently of the app's own math. It
covers zoom 0.25–8, all four view rotations, a `/Rotate 90` page, DPR 1
and 2, mouse pan, wheel zoom, resize and reload. The pass bound is ≤ 1
canonical px (check I4). The test also covers approve, reject, add and
delete, save failures, retry, reload with unsaved edits, and stale scan
responses. It fails as expected if the image is drawn 1.5 px off.

## Integration

* **Render service.** `ViewerService(render=...)` takes any object with
  `register_upload`, `list_documents`, `document_info`, `page_frame`,
  `render_page`, `render_page_png` and `crop_renderer`. Replace the default
  in `service.py` (`StubRenderService`) with the foundation's service when it
  lands, then delete `render_stub.py`. Tests that rely on the stub's markers
  and symbols will need a fixture page from the foundation's test factory.
* **Learning store.** Imported from `pinny.learning`, falling back to
  `pinny_learning` until the move. All store calls run on one worker thread,
  because the store's SQLite connection is thread-bound.
* **Errors.** `ViewerError` subclasses `pinny.errors.PinnyError` once that
  exists.

## Contract gaps (for the coordinator)

1. **Rescan carry-over is unspecified.** Right now reviews stay with their
   scan. Should approvals, rejections and manual pins carry over to a new
   scan of the same page, for example by matching centres within a
   tolerance?
2. The learning store's crop spec is still v1 in PDF points (§6 says v2 in
   canonical px). The viewer passes the frame's pixel size as
   `page_width`/`page_height`, so crops come out in px units until the store
   aligns.
3. The upload/versioning call signatures (`register_upload`,
   `list_documents`, `document_info`, `render_page_png`) are the viewer's
   assumption. §9 lists only `render_page`, `page_frame` and `crop_renderer`.
