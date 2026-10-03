# Viewer and pin review (`pinny/viewer/`, `web/`)

A plain local web app for one reviewer:
upload a PDF → choose a page → drag a box around one receptacle → scan →
approve, reject or add pins → export the report.

```
python -m pinny.viewer [--data-dir DIR] [--port 8765]
# open http://127.0.0.1:8765/
```

Data goes to `$PINNY_DATA_DIR` (default `~/.local/share/pinny`), never the repo.

Pages are rendered from the uploaded PDF by the foundation's
`pinny.render.RenderService` (contracts §9) at 200 DPI.

## Template threshold

The viewer starts at a threshold of **0.65**, below the detector's library
default of 0.80. This was tuned on one real electrical plan (E1-02C, 42 × 30 in).
Every match at 0.64 or above was a real receptacle, including grey existing
devices, variant symbols and symbols crossed by circuit wiring. The false
matches (title-block text and room tags) all scored 0.63 or lower. At 0.80,
16 real receptacles were missed. This is evidence from one page only.
Re-check the threshold against labelled pages from other drawing sets, and
raise it if a sheet shows text false matches.

## Using it

| Mode (key) | Left-drag | Left-click |
|---|---|---|
| Pan / pick pin (V) | pans | selects the nearest pin within 10 px |
| Template box (T) | draws the template box | nothing |
| Mark missed (P) | nothing | adds a manual pin for a device the scan did not find (inside the page only); stays on for several clicks |

**Correct, Wrong, Missed.** The popup beside a selected pin has **Correct**
(A) and **Wrong** (X). A device the scan did not find has no pin to click,
so **+ Missed** is a button on the drawing itself (bottom left, shown once
the page is scanned; also M and the side panel's **+ Missed device**): press
it, click the device, and the viewer marks it (a manual pin, counted as a
correct example for training) and goes back to reviewing with the new pin
selected. Pressing it again, or Esc, cancels. "Mark missed (P)" in the toolbar stays on for several clicks.

In every mode, the wheel zooms about the cursor, and Space+drag or a
middle-button drag pans. So a pan can never place a pin or change the
template.

On a touch screen, one finger acts like the left button in the current
mode (a tap selects a pin within 22 px). Two fingers pan and pinch-zoom in
every mode; a box or drag the first finger had started is dropped when the
second lands, and the finger left after a pinch keeps panning. In Pan mode,
a double tap on the page zooms in 2x there.
Selecting a pin also shows **Correct**, **Wrong** (**Delete** for a pin
added by hand) and **Next** right beside it: the same actions as the side
panel's Approve, Reject and Next unreviewed buttons. A reviewed pin stays
selected with its state shown, so a mistake can be undone at once.
Other keys: `+`/`-` zoom, `0` fit, `R` rotate the view 90°, `H` hide pins,
`A` approve, `X`/`Delete` reject or delete, `M` mark a missed device, `N` next unreviewed, `Esc` cancel
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

## Batch scans (contracts §5a)

`ViewerService.start_batch(...)` scans many pages with one template: the
estimator boxes the symbol once on `template_page_index` (default: the
first page scanned) and every page in `page_indexes` (default: all) is
scanned in the background. All three scan modes work.

* Batches run one at a time, one page at a time, on a single worker thread,
  so memory stays at one page's worth. Detection is about 11 s per Arch D
  sheet with four rotations, so a 60-sheet set takes roughly ten minutes.
* Each page's scan is recorded as soon as it finishes; review can start on
  the first pages while later ones are still scanning.
* A page that fails (for example, smaller than the template) is recorded as
  `failed` with its error and the batch moves on. `resume_batch(id,
  retry_failed=True)` retries it.
* Shutting the viewer down lets the current page finish and leaves the rest
  `pending`. Repeating the `POST /api/batches` request (same `request_id`)
  or calling resume picks up where it stopped. Pages are not resumed
  automatically at startup.
* `batch_queue(id)` is the estimator's review list across every finished
  page. Each item carries `scan_id`, `pin_id` and `version`; review it with
  the normal `POST /api/scans/{scan_id}/actions`, so the learning data is
  the same as for single-page review.

In the browser:

1. Open the document, pick a page, and draw the template box as usual.
2. Leave **Pages to scan** blank for every page, or list pages (`1-5, 8`,
   numbered from 1), then press **Scan all pages** / **Scan listed pages**.
3. The **Batch** section shows progress, a per-page table (click a row to
   open that page and its batch scan) and each page button gets a dot:
   blue scanning, orange marks to review (or a blank page to check for
   misses), green reviewed, red failed.
4. **Next mark to review** (key **B**) jumps to the most uncertain
   unreviewed mark on any page, opening that page if needed. Review it with
   **A** / **X** as usual, then press **B** again. Marks with unsaved edits
   are skipped.
5. **Stop** skips pages not yet started; **Retry failed pages** runs failed
   pages again. The batch shown is kept in the URL, so a reload returns to it.

## Legend and whole set

For a vector drawing set with a symbol legend, Pinny can count every legend
symbol on every sheet without anyone drawing a template
(`docs/legend-reader.md`, `docs/set-scanning.md`). The section **Legend:
count the whole set** (web/legend.js) sits under Document:

1. **Find legend.** Pinny looks for the legend page (a heading such as
   LEGEND or SYMBOLS), opens it, and lists its rows: a picture of each
   symbol (cut from the legend page's raster in the browser), its tag, name
   and group, and a **Count** box. Rows that need a look are marked **!**
   with the reason: two drawings in one row (Split, or Looks right), a
   description that ran onto two lines, a tag used twice, a tag Pinny made
   up, or two symbols drawn the same. If Pinny picked the wrong page, open
   the legend page and press **Read legend on page N**. A drawing without a
   legend, or with a scanned (image) legend, gets a plain message pointing
   to the template workflow.
2. **Check it.** Click a row to bring its symbol to the middle of the view
   and edit it: name, tag, group, Looks right, Split, Delete, or "Same
   symbol as ... Merge". Tick or untick Count. **Add a missing symbol**,
   then drag a box around it on the legend page (**Add anyway** if its row
   was read already). Every change is saved on the server with who made it;
   two people editing at once get "Someone else changed the legend".
3. **Confirm legend** once nothing needs a look. Any later change needs a
   new confirmation. The symbol table then folds away.
4. **Scan whole set.** Scans every sheet except the legend page on the
   server; the progress bar counts sheets searched. Each sheet gets one
   ordinary scan (mode `legend`), so everything below works as for any scan.
5. **Review sheet by sheet.** The **sheets** list shows each sheet's count,
   what is left to review and its status (to review, ready to mark reviewed,
   reviewed); click a row or **Next sheet to review**. Each pin carries a
   small label with its tag in the tag's colour; the key, a **Show** filter
   by tag, the **Tag for new pins** (last used, else the sheet's most common
   tag) and **Approve as ...** (the pin is another symbol) are under Review.
   The pin list has a Tag column. Approve, reject, add and "Mark page fully
   reviewed" work as usual, per sheet.
6. **Counts** per tag for the whole set or one sheet: found by Pinny,
   approved, rejected, added by hand, total (approved + added) and left to
   review, with **Download counts (CSV)**. "Found" stays under the tag
   Pinny gave a pin; approving it as another tag moves the approval.

**Engineer's standard legend** (folded under the legend): save the
confirmed legend as an engineer's standard; for a later set, enter the
engineer and press Compare to see which symbols match, are new, changed or
missing.

Template scans are unchanged: the tag tools appear only on legend scans, and
pins added to a template scan have no tag.

## Sheet info (optional OCR)

OCR is off until an admin turns it on under **OCR (admin)**. It is one
switch for the whole site, and `$PINNY_OCR_ENGINE` on the server overrides
it. When it is on, **Read sheet info** under *2. Page* reads the sheet
number, title and revision from the title-block corner of the open page and
saves the result for that page. See `docs/ocr.md`.

## Sign-in and security

The HTTP layer is Starlette on uvicorn (`server.py`). In development it is
a single local admin with no sign-in, on 127.0.0.1 only. In production it
verifies the AWS load balancer's signed sign-in header (Cognito), checks the
members table and each route's
role, enforces Origin and Content-Type on state-changing requests, sends
security headers, streams uploads to disk, and hides internal errors
behind a request id. `docs/training-site.md` is the contract; the page
shows who is signed in, a sign-out link, an admin-only Members panel and a
Delete button for drawings (uploader or admin).

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
| `POST /api/scans/{id}/actions` `{action, request_id, pin_id?, x?, y?, expected_version?, class_label?}` | review action; `class_label` (a tag of the document's legend) with `add_manual` or `approve` |
| `GET /api/scans/{id}/report` | report download |
| `POST /api/batches` `{document_version, request_id, template_box, template_page_index?, page_indexes?, mode?, threshold?, model_threshold?}` | start a batch scan (202); see "Batch scans" |
| `GET /api/batches/{id}` | batch status, per-page status and pin counts |
| `GET /api/batches/{id}/queue?strategy=margin\|lowest_score&limit=N` | unreviewed pins across the batch, most uncertain first |
| `POST /api/batches/{id}/cancel` | skip pages not yet started |
| `POST /api/batches/{id}/resume` `{retry_failed?}` | resume interrupted pages, optionally retry failed ones |
| `GET /api/documents/{version}/batches` | batches of a document version |
| `GET /api/documents/{version}/legend` | `{legend, set_scan}`: the legend (or null) and the latest whole-set scan |
| `POST /api/documents/{version}/legend/read` `{page_index?}` | find (or read from that page) and store the legend |
| `POST /api/documents/{version}/legend/edit` `{op, entry_id?, expected_version?, ...}` | one legend edit: `rename {name}`, `set_tag {tag}`, `set_group {group}`, `set_count {count}`, `confirm`, `split`, `merge {into}`, `delete`, `add {box, force?}`, `set_engineer {engineer}` |
| `POST /api/documents/{version}/legend/confirm` `{expected_version?}` | confirm the legend (409 `legend_needs_review`) |
| `POST /api/documents/{version}/legend/save-standard` `{engineer}` | save as the engineer's standard legend |
| `GET /api/legend-library` | saved standard legends |
| `POST /api/documents/{version}/set-scans` `{request_id}` | scan every sheet with the confirmed legend (202); the run id is derived from `request_id` |
| `GET /api/documents/{version}/set-scans` | whole-set scans, oldest first, with progress and per-sheet scan ids |
| `GET /api/documents/{version}/counts` | counts per tag, per sheet and for the set, from the latest whole-set scan |
| `GET /api/documents/{version}/counts.csv` | the same as a CSV download |

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
node --test tests/viewer/test_transform.mjs tests/viewer/test_edits.mjs tests/viewer/test_batch.mjs tests/viewer/test_legend.mjs
node tests/viewer/test_batch_browser.mjs            # batch scans in Chromium
node tests/viewer/test_legend_browser.mjs           # legend and whole-set scan in Chromium
node tests/viewer/test_site_browser.mjs             # signed-in site in Chromium
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

* **Render service.** `ViewerService(render=...)` defaults to
  `pinny.render.RenderService(data_dir)`. It uses `ingest_pdf`, `get_version`,
  `list_versions`, `page_frame`, `render_page`, `render_page_png` and
  `crop_renderer`. Tests use real vector PDFs from `tests/factory.py`, with
  receptacle glyphs at known positions (`tests/viewer/pdfgen.py`).
* **Learning store.** Imported from `pinny.learning`. All store calls run on one worker thread,
  because the store's SQLite connection is thread-bound.
* **Errors.** `ViewerError` subclasses `pinny.errors.PinnyError`. Other modules'
  `PinnyError`s reach the client with their own `code` and `http_status`.

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
