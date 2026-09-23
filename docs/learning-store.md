# Learning store (`pinny.learning`)

Local SQLite persistence for scans, pin review state and training-example
capture. It follows `docs/contracts.md` v1, sections 1 and 3–6. It
**captures examples only**. It never trains or adjusts the detector. It uses
only the standard library.

## Wiring

| Caller | Calls |
|---|---|
| Scan service, after detection | `record_scan(Scan, [Detection])` or `record_scan_result(section4_dict)` |
| Viewer API handlers | `approve`, `reject`, `add_manual`, `remove_manual`, `delete_pin` with `request_id=<client uuid4>`, `expected_version=pin.version`, `source="viewer"` |
| Viewer reload | `list_scans(document_version, page_index)`, `load_scan(scan_id)`, `get_pin`, `scan_result(scan_id)` |
| Render service (foundation) | provides `crop_renderer(spec: CropSpec) -> PNG bytes`, which cuts `spec.box` from `render_page(spec.document_version, spec.page_index)` |

```python
from pinny.learning import LearningStore
store = LearningStore(crop_renderer=render_service.crop_renderer)  # data dir: $PINNY_DATA_DIR
store.record_scan_result(scan_result_dict)
res = store.approve(scan_id, pin_id, request_id=req_uuid4, source="viewer", expected_version=v)
res.pin.version, res.replayed
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

## Storage layout (outside Git, covered by `.gitignore`)

```
$PINNY_DATA_DIR  (default: $XDG_DATA_HOME/pinny or ~/.local/share/pinny)
├── pinny.sqlite3          # scans, detections, pins, review_events, crops (WAL)
├── crops/<k[:2]>/<k>.png  # k = sha256 of the crop spec (deterministic)
└── exports/*.json         # versioned metadata exports
```

## Tables (store schema 2)

| table | mutability | contents |
|---|---|---|
| `scans` | insert-once (idempotent on `scan_id`) | `document_id`, `document_version`, `page_index`, `canonical_page_id`, frame size and DPI, template box and sha256, detector name/version/settings and settings sha256 |
| `detections` | immutable (trigger) | original boxes, point, score, rotation, source, extra keys |
| `pins` | current state, `version` counter | origin `machine`/`manual`, state `unreviewed`/`approved`/`rejected`/`added`/`removed` |
| `review_events` | append-only (triggers) | `event_id`, unique `request_id`, action, prior/new pin snapshot, source, reviewer, timestamp, crop_key |
| `crops` | status only | full `CropSpec`, `pending`/`written`/`failed`, path, sha256, attempts, last_error |

A store created before the contract (schema 1, PDF points) is refused with
`schema_mismatch`. Move it aside, because it is not migrated.

## Semantics

* Each action runs in one `BEGIN IMMEDIATE` transaction that updates the pin,
  appends the event and records the crop spec.
* Idempotency: repeating a `request_id` returns the stored result
  (`replayed=True`), writes nothing and ignores `expected_version`.
  Reusing a `request_id` for a different payload raises
  `idempotency_conflict`. Manual pin ids and event ids are uuid5 values
  derived from the `request_id`, so retries converge on the same ids.
* `delete_pin` rejects a machine pin and records `remove_manual` for a
  manual pin.
* Crop images are produced after commit and written atomically
  (tmp + fsync + rename). A render failure, or no renderer at all, leaves
  the crop `failed` or `pending`. `process_pending_crops()` regenerates it,
  including when a written file has gone missing.
* Labels are derived at export time from the raw events (`interpret_pin`):

  | events | label |
  |---|---|
  | `approve` | positive |
  | `reject` | negative |
  | `add_manual` | positive |
  | none (unreviewed) | unlabeled |
  | several approve/reject | the latest one wins |
  | manual pin later removed | **no label** (`manual_removed`), never a negative |

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
  `document_version`, `page_index`, `canonical_page_id` and the frame size.
  That is enough to cut the crop again from the canonical raster.

## CLI

```
python -m pinny.learning [--data-dir DIR] status
python -m pinny.learning [--data-dir DIR] export [--document-version sha256:...] [--labeled-only] [--out FILE]
```

## Tests

```
python3 -m unittest discover -s tests/learning -t .
```
