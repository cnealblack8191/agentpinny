# Learning store (`pinny.learning`)

Local SQLite persistence for scans, pin review state and training-example
capture. It follows `docs/contracts.md` v1, sections 1 and 3–6. It
**captures examples and serves them back** (thresholds, review order,
template-bank crops, datasets). It never trains or adjusts the detector
itself: every suggestion is applied by a human or by the calling module. It
uses only the standard library.

## Wiring

| Caller | Calls |
|---|---|
| Scan service, after detection | `record_scan(Scan, [Detection])` or `record_scan_result(section4_dict)` |
| Viewer API handlers | `approve`, `reject`, `add_manual`, `remove_manual`, `delete_pin` with `request_id=<client uuid4>`, `expected_version=pin.version`, `source="viewer"` |
| Viewer reload | `list_scans(document_version, page_index)`, `load_scan(scan_id)`, `get_pin`, `scan_result(scan_id)` |
| Viewer review flow | `review_queue(scan_id)` (uncertain first), `mark_page_complete(canonical_page_id)`, `page_review_status` |
| Detection (template bank, tuning) | `register_template`, `template_bank_crops`, `negative_crops`, `review_stats` + `suggest_threshold` |
| Training / bench | `set_split`, `export_dataset(out_dir, split=...)` |
| Render service (foundation) | provides `crop_renderer(spec: CropSpec) -> PNG bytes` (optionally with a `renderer_version` attribute) and the page rasters that datasets reference |

```python
from pinny.learning import LearningStore, suggest_threshold
store = LearningStore(crop_renderer=render_service.crop_renderer)  # data dir: $PINNY_DATA_DIR
store.record_scan_result(scan_result_dict)
res = store.approve(scan_id, pin_id, request_id=req_uuid4, source="viewer", expected_version=v)
res.pin.version, res.replayed, res.noop
```

* `reviewer` defaults to `local_reviewer_identity()`: `$PINNY_REVIEWER`,
  then the OS user, then `None`. Passing `reviewer=` overrides it.
* `source` must be `"viewer"`, `"cli"` or `"test"`.
* Errors subclass `pinny.errors.PinnyError` once the foundation lands.
  Until then they use a local shim with the same `(code, message)` shape.

  | error | code | suggested HTTP status |
  |---|---|---|
  | `NotFound` | `not_found` | 404 |
  | `InvalidArgument` | `invalid_argument` | 400 |
  | `InvalidTransition` | `invalid_transition` | 409 |
  | `StaleVersion` | `stale_version` | 409 |
  | `IdempotencyConflict` | `idempotency_conflict` | 409 |
  | `SchemaMismatch` | `schema_mismatch` | 500 |
  | `WrongThread` | `wrong_thread` | 500 (programming error) |

## Threads

**One `LearningStore` per thread.** Each store owns one SQLite connection;
calling it from another thread raises `WrongThread` with an actionable
message instead of SQLite's generic `ProgrammingError`. A threaded server
keeps a store in a `threading.local()` (or opens one per request). SQLite in
WAL mode with a 30 s busy timeout coordinates the connections: readers never
block, and writers serialise on `BEGIN IMMEDIATE`.

This was chosen over one shared connection with `check_same_thread=False`
and a lock because a shared connection shares transaction state: a missed
lock anywhere (including the post-commit crop rendering) would let one
thread's statements run inside another thread's transaction. Separate
connections cannot interfere that way.

## Storage layout (outside Git, covered by `.gitignore`)

```
$PINNY_DATA_DIR  (default: $XDG_DATA_HOME/pinny or ~/.local/share/pinny)
├── pinny.sqlite3          # all tables below (WAL)
├── crops/<k[:2]>/<k>.png  # k = crop key (sha256 of the crop spec)
└── exports/*.json         # versioned metadata exports
```

## Tables (store schema 3)

| table | mutability | contents |
|---|---|---|
| `scans` | immutable (trigger; insert-once, idempotent on `scan_id`) | `document_id`, `document_version`, `page_index`, `canonical_page_id`, frame size and DPI, template box and sha256, optional `template_id`, detector name/version/settings and settings sha256 |
| `detections` | immutable (no update, no delete triggers) | original boxes, point, score, rotation, source, extra keys |
| `pins` | current state, `version` counter | origin `machine`/`manual`, state, point, box (machine: detection box; manual: optional), `rotation`, `class_label` |
| `review_events` | append-only (triggers) | `event_id`, unique `request_id`, action, prior/new pin snapshot, source, reviewer, timestamp, crop_key |
| `review_noops` | insert-only | requests that changed nothing (re-approve), so their retries replay |
| `crops` | status only | full `CropSpec`, `pending`/`written`/`failed`, path, sha256, attempts, last_error |
| `templates` | insert-once | `template_id`, pixel `sha256`, width, height, optional `png` BLOB and/or `path`, `class_label`, metadata |
| `page_reviews` | current state | `canonical_page_id`, status `in_progress`/`complete`, `completed_at`, reviewer |
| `splits` | current state | `document_id` → `train`/`val`/`test`/`eval` |
| `dataset_exports` | append-only (trigger) | one row per `export_dataset` call with its manifest and sha256 |

## Schema migrations

`store_meta.schema_version` holds the version. `schema.py` keeps the frozen
schema-2 baseline and a list of migrations (`MIGRATIONS[n]` turns `n` into
`n + 1`). Opening a store:

* creates a new database from the baseline and runs every migration, so new
  and upgraded stores go through the same DDL;
* upgrades an older store **in place, in one `BEGIN IMMEDIATE`
  transaction**: either every step applies or the file is left unchanged.
  Concurrent openers re-check the version inside the transaction;
* refuses (`schema_mismatch`) a store newer than the code, and schema 1 (the
  pre-contract prototype in PDF points), which cannot be converted.

Schema 2 → 3 adds `templates`, `page_reviews`, `splits`, `review_noops`,
`dataset_exports`, `scans.template_id`, `pins.class_label`, `pins.rotation`
(backfilled from detections), and the `scans_no_update` /
`detections_no_delete` triggers. `python -m pinny.learning migrate` runs it
explicitly. To add a migration, append a step and bump
`contract.STORE_SCHEMA_VERSION`; never edit the baseline or an old step.

## Semantics

* Each action runs in one `BEGIN IMMEDIATE` transaction that updates the pin,
  appends the event and records the crop spec. If `COMMIT` itself fails the
  store rolls back and re-raises, so the connection stays usable.
* Multi-table reads (`export`, `load_scan`, `review_queue`, crop lists,
  `export_dataset`) run in one `BEGIN DEFERRED` read transaction and see one
  snapshot, with a fixed number of queries however many scans there are.
* Idempotency: repeating a `request_id` returns the result **as recorded**,
  rebuilt from the event's `new_state` (`replayed=True`), even if the pin
  has changed since. It writes nothing and ignores `expected_version`.
  Reusing a `request_id` for a different payload raises
  `idempotency_conflict`. The payload is action, scan, pin, point, box,
  rotation, class label, source and event id. `reviewer` is recorded but is
  **not** part of it, so a retry after the identity changed still replays
  (events written by schema 2 are matched with their recorded reviewer).
  Manual pin ids and event ids are uuid5 values derived from the
  `request_id`, so retries converge on the same ids.
* Approving an approved pin (without a new `class_label`), or rejecting a
  rejected one, is a **no-op**: no event, no version bump, `noop=True`, and
  `event` is the latest event that set the state. The request id is kept in
  `review_noops`, so a retry after later changes replays instead of
  re-applying.
* `approve(..., class_label=)` sets the symbol class (`None` keeps it).
  `add_manual(..., box=, rotation=, class_label=)` records an optional
  symbol box and rotation. A manual pin's training crop is still the
  section-6 128 px square; the box is for annotation (datasets, template
  bank).
* `delete_pin` rejects a machine pin and records `remove_manual` for a
  manual pin.
* Crop images are produced after commit and written atomically
  (tmp + fsync + rename + directory fsync). A render failure, or no renderer
  at all, leaves the crop `failed` or `pending`. `process_pending_crops()`
  regenerates it when the file is missing **or its sha256 no longer matches
  the recorded one**. If a re-render of the same spec gives different bytes,
  the crop is rewritten and `last_error` notes the drift.
* Labels are derived at export time from the raw events (`interpret_pin`):

  | events | label |
  |---|---|
  | `approve` | positive |
  | `reject` | negative |
  | `add_manual` | positive |
  | none (unreviewed) | unlabeled |
  | several approve/reject | the latest one wins |
  | manual pin later removed | **no label** (`manual_removed`), never a negative |

* A pin's effective class label is its own `class_label`, else the class
  label of its scan's registered template.

### Page review and splits

* `mark_page_complete(canonical_page_id, allow_unreviewed=False)` records
  that every receptacle on the page is pinned, so the rest of the page is
  safe background. It refuses while any scan of the page has unreviewed
  machine pins. It is idempotent; `mark_page_in_progress` reopens a page.
* `set_split(document_id, split)` assigns a whole document (never single
  pages, so evaluation pages cannot leak). Moving a document out of `test`
  or `eval` needs `force=True`.
* `export()`, `review_stats()`, `template_bank_crops()` and
  `negative_crops()` leave out `test`/`eval` documents unless
  `include_heldout=True`. Documents without a split are included.

## Loop APIs (read-only, no detector dependencies)

* `review_stats(document_id=None, template_sha256=None,
  detector_settings_sha=None)` → `[(score, label)]` for reviewed machine pins
  (approved 1, rejected 0), highest score first. `detector_settings_sha` is
  the scan's `settings_sha256` (`detector_settings_sha(settings)` computes
  it). Pins the detector never proposed are absent.
* `suggest_threshold(stats, target_precision=0.95, min_labels=30)` →
  `ThresholdSuggestion(value, precision, recall_proxy, n, reason)`: the
  lowest observed score whose precision is at least the target. `value` is
  `None` with reason `insufficient_labels`, `no_positives` or
  `target_not_reached`. `recall_proxy` counts only reviewed approved pins and
  overstates true recall. The result is a suggestion only.
* `review_queue(scan_id, strategy="margin", threshold=None, limit=50)` →
  unreviewed machine pin ids by ascending `|score - threshold|`. The
  threshold defaults to the scan's `threshold`/`score_threshold`/`min_score`
  setting, else the lowest score. `strategy="lowest_score"` sorts by score.
* `template_bank_crops(document_id=None, class_label=None)` (approved machine
  and added manual pins) and `negative_crops(...)` (rejected machine pins)
  return `CropRecord(crop_key, path, sha256, label, class_label, status,
  scan_id, pin_id, origin, canonical_page_id, document_id, box, crop_box,
  rotation, score)`. Only written crops by default; each crop key once; crops
  labelled both ways across scans are left out of both lists (they appear
  in `export()["label_conflicts"]`). `box - crop_box` origin locates the
  symbol inside the crop image.

## Metadata export

`export(document_version=None, include_unlabeled=True, out_path=None,
include_heldout=False)` returns (and optionally writes, fsynced) the
`pinny.learning.export` v2 document: `scans`, `examples` (now with
`class_label` and `rotation`), `crops`, raw `events`, plus `label_conflicts`
(crop key → example ids per label), `page_reviews`, `splits` and
`templates`.

## Dataset export

`export_dataset(out_dir, fmt="coco", split="train", require_page_complete=True,
tile=None, include_unassigned=None, manual_box_px=None)` writes
`<split>.coco.json` and `manifest.json` and records a `dataset_exports` row.

* Documents: those in `split`. Documents without a split count as `train`
  (`include_unassigned`, default only for `train`). Held-out splits appear
  only when requested by name.
* Pages: only pages marked complete, unless `require_page_complete=False`.
  Skipped pages are listed in `excluded_incomplete_pages`.
* Annotations: approved machine pins (detection box) and added manual pins
  (their box, else a box the size of the scan's template, the registered
  template, or `manual_box_px`, centred on the point; otherwise a warning).
  Overlapping boxes from several scans of a page (IoU > 0.5) are merged.
  Categories come from effective class labels (default `receptacle`).
* `tile=size` or `(size, overlap)` splits pages into tiles; boxes are
  clipped and kept when at least half is visible.
* **No images are written**: the store has no rasters. Each COCO image
  carries `canonical_page_id`, `document_version`, `page_index` (and `tile`)
  and a `file_name` of `<document_version with ':' as '-'>_p<page>[_x<x>_y<y>].png`.
  The render service supplies the pixels (`render_page`, canonical raster
  at 200 DPI).
* The manifest has the annotation file's `sha256`, pages, documents, counts
  per category, warnings, and the settings used. The COCO file is
  deterministic for the same data.

## Crop spec v2 (contracts section 6)

Units are canonical raster px at 200 DPI. Boxes are `{x, y, width, height}`
with an exclusive right/bottom edge.

* Manual pin: a 128 × 128 window. `x0 = floor(x - 64 + 0.5)` and
  `x1 = x0 + 128`, and the same for y. This is the whole-pixel window whose
  centre is nearest the pin.
* Machine detection: `floor(x) - 24 .. ceil(x + width) + 24`, and the same
  for y.
* Both are clipped to `[0, width) × [0, height)`. The spec records
  `unclipped_box`, `box` and the per-edge `clipped_*` flags, plus
  `document_version`, `page_index`, `canonical_page_id`, the frame size and
  **`renderer_version`** (from `LearningStore(renderer_version=...)` or the
  renderer's `renderer_version` attribute; default `"unknown"`). That is
  enough to cut the crop again from the canonical raster.
* The crop key is the sha256 of the canonical spec JSON. A spec with
  `renderer_version == "unknown"` hashes without the field, so keys written
  by schema 2 stay valid. A new renderer version gives new keys instead of
  overwriting crops with different pixels.

## CLI

```
python -m pinny.learning [--data-dir DIR] status
python -m pinny.learning [--data-dir DIR] migrate
python -m pinny.learning [--data-dir DIR] export [--document-version sha256:...] [--labeled-only] [--include-heldout] [--out FILE]
python -m pinny.learning [--data-dir DIR] export-dataset OUT_DIR [--split train|val|test|eval] [--allow-incomplete] [--tile PX [--overlap PX]]
python -m pinny.learning [--data-dir DIR] suggest-threshold [--document-id ID] [--template-sha256 HEX] [--settings-sha256 HEX] [--target-precision 0.95] [--min-labels 30]
python -m pinny.learning [--data-dir DIR] split DOCUMENT_ID [train|val|test|eval] [--force]
python -m pinny.learning [--data-dir DIR] page-complete CANONICAL_PAGE_ID [--allow-unreviewed]
```

## Tests

```
python -m pytest tests/learning
python3 -m unittest discover -s tests/learning -t .
```

`tests/learning/fixtures/store_v2.sql` is a database written by the schema-2
code; the migration tests upgrade it.
