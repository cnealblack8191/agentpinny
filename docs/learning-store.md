# Learning store (`pinny_learning`)

Local SQLite persistence for scans, pin review state, and training-example
capture. It **captures examples only**. It never trains or adjusts the detector.
It uses only the standard library, so no dependency changes are needed.

## Storage layout (outside Git)

```
$PINNY_DATA_DIR  (default: ~/.local/share/pinny)
├── pinny.sqlite3          # scans, detections, pins, review_events, crops (WAL mode)
├── crops/<k[:2]>/<k>.png  # k = sha256 of the crop spec (content-addressed, deterministic)
└── exports/*.json         # versioned metadata exports
```

## Tables

| table | mutability | contents |
|---|---|---|
| `scans` | insert-once (idempotent on `scan_id`) | document version, canonical page, page size, template, detector version, matching settings and their sha256 |
| `detections` | immutable (trigger) | original detector boxes, scores, and raw payload |
| `pins` | current state, `version` counter | origin `machine`/`manual`, state `unreviewed`/`approved`/`rejected`/`added`/`removed` |
| `review_events` | append-only (triggers) | `event_id`, unique `request_id`, action, prior/new pin snapshot, source, reviewer, timestamp, crop_key |
| `crops` | status only | the full reproducible `CropSpec`, `pending`/`written`/`failed`, path, sha256, attempts, last_error |

## Semantics

* Each action (`approve`, `reject`, `add_manual`, `remove_manual`, `delete_pin`)
  runs in a single `BEGIN IMMEDIATE` transaction that updates the pin and
  appends the event. The crop spec is committed in the same transaction.
* Idempotency: repeating a `request_id` returns the stored result
  (`replayed=True`) and writes nothing. Reusing it for a different
  payload raises `IdempotencyConflict`. Manual pin ids and event ids are
  derived from the `request_id` (uuid5), so retries converge on the same ids.
* Crop images are rendered after commit by an injected `crop_renderer(spec) -> PNG bytes`.
  Files are written atomically (tmp + fsync + rename). A failure or a missing
  renderer leaves the crop `failed`/`pending`. `process_pending_crops()`
  regenerates it, including crops whose file has since gone missing.
* Labels are derived at export time from the raw events (`interpret_pin`):
  approve → positive, reject → negative, add → positive, unreviewed →
  unlabeled. The latest approve/reject wins. Removing a manual pin yields
  **no label** (`manual_removed`), not a negative.

## Crop bounds (`pinny_learning/contract.py`, spec v1, *assumed*)

Canonical units are PDF points with the origin at the top-left.
* Manual pin: a 48×48 square centred on the pin.
* Machine detection: the detection box plus an 8-unit margin.

Both are clipped to the page, with per-edge `clipped_*` flags and the
`unclipped_box` recorded. The pixel box at 200 DPI uses floor/ceil and is
clamped to the page raster. Geometry is rounded to 3 decimals before hashing.

## CLI

```
python -m pinny_learning [--data-dir DIR] status
python -m pinny_learning [--data-dir DIR] export [--document-version-id ID] [--labeled-only] [--out FILE]
```

## Tests

```
python3 -m unittest discover -s tests -t .
```
