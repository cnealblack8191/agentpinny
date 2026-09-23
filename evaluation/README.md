# Pinny evaluation

Independent measurement tools for Pinny's receptacle detector. This directory
**measures** detector output. It does not modify, tune, or depend on the detector.

## Current status

| Question | Answer |
|---|---|
| Real-drawing precision/recall | **Not measured — verified reference pending** |
| Evaluator correctness | Checked by synthetic fixtures and a brute-force cross-check (`tests/`) |
| App integration (upload → export) | Awaiting integration; see [INTEGRATION_CHECKS.md](INTEGRATION_CHECKS.md) |

No verified receptacle locations exist yet for any real drawing. Every number
in `samples/` and `fixtures/` comes from hand-authored synthetic points. Those
numbers show that the evaluator counts correctly. They say nothing about how
the detector performs on real drawings.

> **Contract caveat.** `docs/contracts.md` and `docs/agent-ownership.md` did not
> exist when this was written (the repository was empty). The two input formats
> below are the evaluator's own. Once the scan-result contract is defined, the
> app's output needs an adapter into `pinny.detections` v1, or these loaders
> need updating to match. Treat both formats as provisional until then.

## Requirements and tests

Python 3.9+, standard library only.

```sh
cd evaluation
python3 -m unittest discover -s tests -v
```

## CLI

Run it from `evaluation/`:

```sh
python3 -m pinny_eval score \
  --detections   path/to/detections.json \
  --ground-truth path/to/ground_truth.json \
  --tolerance-px 10 \
  --document-id DOC --document-version sha256:... --page 0 \
  --width 7200 --height 4800 \
  --json-out report.json --md-out report.md
```

* `score` matches original detector results against a separately supplied reference.
* `pending` records detector provenance for a page that has no reference yet.
  Its report states **Not measured — verified reference pending**.
* `validate --ground-truth FILE` checks a labelled file before you use it.

The identity arguments (`--document-id`, `--document-version`, `--page`,
`--width`, `--height`) and `--tolerance-px` are all required and have no
defaults. Both input files must match the identity arguments and each other.
Otherwise the run is rejected.

Exit codes: `0` for a report written, `2` for rejected input (wrong document,
version, page, frame or raster size; malformed data; corrected pins), and `64`
for a usage error.

## Matching rules

* Distance is Euclidean, measured in canonical raster pixels. A prediction and
  a reference are eligible to pair when `distance <= tolerance` (the bound is
  inclusive).
* Matching is one-to-one. Each prediction and each reference can be used at
  most once.
* The objective is **maximum cardinality first** (as many matches as possible),
  then **minimum total distance** among the matchings of that size. Greedy
  nearest-first matching can under-count clustered receptacles; see
  `fixtures/synthetic/ambiguous_nearby`.
* When a second prediction lands on an already-matched receptacle, it counts
  as a false positive. The report gives the reason `duplicate_or_crowded` and
  names the prediction that took the credit.
* `TP` = matched pairs, `FP` = predictions − TP, `FN` = references − TP.
* Precision = TP / (TP + FP). Recall = TP / (TP + FN).

### Zero denominators

When a denominator is 0, the metric's `value` is `null` and `undefined_reason`
explains why. The value is never shown as 0 or 1.

| Case | Precision | Recall |
|---|---|---|
| No predictions, some references | undefined (`no predictions`) | 0.0 |
| Some predictions, no references | 0.0 | undefined (`no reference receptacles`) |
| Both empty | undefined | undefined |

When you aggregate across pages, sum TP/FP/FN first and then divide. Don't
average per-page ratios.

## Detector results vs. corrections

A detector's accuracy is measured only on what the detector itself produced
for a specific scan. The evaluator enforces this:

* A detections file must declare `"provenance": "original_detector_output"`.
  If it declares anything else, it is rejected, including a corrected pin set.
* Every detection must have `source` equal to `"detector"` (the default when
  the field is omitted). Items marked `manual`, `user_added`, `user_moved`,
  `corrected` or `edited` cause the run to be rejected.
* A reference file must declare `verification.independent_of_detector: true`.
  Don't build a reference from the user's corrected pins: the corrections
  started from the detector's output, so scoring against them would count
  the detector's own output as correct.

The final corrected pin set is the user's deliverable, not a measurement.
Report it separately, for example as the number of pins added, removed or
moved. Never report it as detector precision or recall.

## Formats (v1, provisional)

Coordinates are in the page's **canonical raster** (`canonical_raster_px`),
with the origin at the top-left and y increasing downward. Points must lie
within `[0, width] × [0, height]`. IDs must be unique within a file.

### `pinny.detections`

```json
{
  "format": "pinny.detections",
  "format_version": 1,
  "provenance": "original_detector_output",
  "scan_id": "scan-2026-09-23T10:00:00Z-abc",
  "document": { "document_id": "doc-123", "document_version": "sha256:…", "page_index": 0 },
  "coordinate_frame": { "space": "canonical_raster_px", "width": 7200, "height": 4800,
                        "origin": "top-left", "y_axis": "down" },
  "detector": { "name": "pinny-detector", "version": "git:abc1234", "settings": { "threshold": 0.6 } },
  "detections": [ { "id": "det-1", "x": 1234.5, "y": 678.0, "confidence": 0.91, "source": "detector" } ]
}
```

The report copies the `detector` block (name, version, settings) verbatim and
also records the SHA-256 of the results file.

### `pinny.ground_truth`

Start from [`templates/ground_truth.template.json`](templates/ground_truth.template.json).
It deliberately fails `validate` until you fill it in, because `width` and
`height` are 0.

`dataset.verification.status` is one of the following:

| Status | Meaning | How the report labels it |
|---|---|---|
| `verified` | Labelled by hand, independently of the detector, and checked by a second person | "Measured against verified reference" (applies to that dataset only) |
| `unverified` | Labelled, but not yet reviewed | "Not measured — verified reference pending"; counts show agreement only |
| `synthetic` | Hand-authored test points | "Not measured — verified reference pending"; evaluator validation only |

## Labelling a real page

1. **Freeze the input.** Record the exact uploaded file's content hash
   (`document_version`), the `document_id`, the 0-based `page_index`, and the
   canonical raster `width` × `height` that Pinny renders for that page.
   Export the canonical raster image at that size.
2. **Label blind.** Open the raster in any image tool that shows pixel
   coordinates (for example GIMP's pointer panel or a simple HTML canvas).
   **Keep Pinny's detections and overlay hidden** while you label.
3. **Decide what counts before you start.** Write it in `dataset.notes`: which
   symbols count as receptacles (duplex, GFCI, quad, floor, ...), what to do
   with legend or key symbols and with symbols in detail callouts, and which
   exact point on the symbol to mark (for example the centre of the symbol's
   circle). Mark that same point every time.
4. **Place one point per receptacle** in the `receptacles` list, with a unique
   `id` and pixel `x`, `y`. Use `note` for anything unclear.
5. **Leave `status` as `"unverified"`.** A second person reviews every point
   against the drawing. Record them in `verification.reviewed_by`, then set
   `status` to `"verified"`.
6. **Validate** the file with
   `python3 -m pinny_eval validate --ground-truth FILE`.
7. **Choose the tolerance before you score**, based on symbol size at the
   canonical resolution (for example half the symbol's diameter). Record the
   reason. Don't adjust the tolerance after seeing results.
8. **Score** a detections file exported from one specific scan of the same
   document version, page and raster. Keep the report together with both
   input files; the report records their hashes.

A single labelled page gives a result for that page only. Claims about the
detector in general need several labelled pages from different drawing sets.

## Layout

```
pinny_eval/        matching, input validation, report, CLI
tests/             evaluator self-tests
fixtures/synthetic/<case>/{detections,ground_truth,expected}.json
templates/         blank ground-truth file
samples/           example reports generated from synthetic fixtures
INTEGRATION_CHECKS.md
```

In each fixture directory, `expected.json` was worked out by hand
independently of the evaluator. The distances are noted in each file.
