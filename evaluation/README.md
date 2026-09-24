# Pinny evaluation

Independent measurement tools for Pinny's receptacle detector. This directory
**measures** detector output. It does not modify, tune, or depend on the detector
(it never imports `pinny`).

## Current status

| Question | Answer |
|---|---|
| Real-drawing precision/recall | **Not measured — verified reference pending** |
| Evaluator correctness | Checked by synthetic fixtures, a brute-force matcher cross-check and a re-match cross-check of the score curve (`tests/`) |
| App integration (upload → export) | Awaiting integration; see [INTEGRATION_CHECKS.md](INTEGRATION_CHECKS.md) |

No verified receptacle locations exist yet for any real drawing. Every number
in `samples/` and `fixtures/` (including `fixtures/corpus_mini/`) comes from
hand-authored or seeded synthetic points. Those numbers show that the
evaluator counts correctly. They say nothing about how the detector performs
on real drawings.

The input formats follow `docs/contracts.md` v1: a `pinny.detections` file is
the scan result (section 4) plus `format`, `format_version` and `provenance`.

## Requirements and tests

Python 3.9+, standard library only. From the repository root, either:

```sh
python3 -m unittest discover -s evaluation/tests -v
python3 -m pytest evaluation/tests          # optional; pip install pytest
```

## CLI

Run it from `evaluation/`, or from anywhere with `PYTHONPATH=evaluation`:

```sh
python3 -m pinny_eval score \
  --detections   path/to/detections.json \
  --ground-truth path/to/ground_truth.json \
  --tolerance-px 10 \
  --document-id DOC --document-version sha256:... --page 0 \
  --width 7200 --height 4800 \
  --json-out report.json --md-out report.md
```

| Command | Purpose |
|---|---|
| `score` | Match one page's original detector results against a separately supplied reference. |
| `score-corpus --manifest FILE` | Score every page in a manifest and pool the results (see [Corpus scoring](#corpus-scoring)). |
| `pending` | Record detector provenance for a page with no reference yet. Its report states **Not measured — verified reference pending**. |
| `validate --ground-truth FILE` | Check a labelled file before you use it. |
| `from-points --csv FILE --out-dir DIR ...` | Convert a generic `x,y,page,document` CSV into ground-truth files (see [Bootstrapping labels](#bootstrapping-labels-from-point-csvs)). |

For `score`, the identity arguments (`--document-id`, `--document-version`,
`--page`, `--width`, `--height`) are all required and have no defaults, and at
least one of `--tolerance-px` / `--tolerance-rel` is required. Both input
files must match the identity arguments and each other, or the run is rejected.

Options shared by `score` and `score-corpus`:

| Flag | Meaning |
|---|---|
| `--tolerance-px N` | Match radius in canonical raster pixels. |
| `--tolerance-rel F` | Match radius as a fraction of each detection box's short side (see [Tolerance](#tolerance)). |
| `--baseline FILE` | Regression gate: compare AP, recall and F1 with a baseline (see [Baseline gate](#baseline-gate)). |
| `--max-drop D` | Allowed absolute drop per gated metric; default `0.01`. |
| `--write-baseline FILE` | Write this run's gated metrics as a `pinny.eval_baseline` file. |
| `--json-out`, `--md-out` | Report destinations. With neither, Markdown goes to stdout. |

`score-corpus` also takes `--worst N` (default 5).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Report written; baseline gate passed or not requested |
| `2` | Rejected input: wrong document, version, page, frame, raster size or dpi; malformed data; corrected pins; a reference that breaks the independence rules; tolerance differs from the manifest or baseline |
| `3` | Baseline regression: AP, recall or F1 dropped by more than `--max-drop`. The reports are still written. |
| `64` | Usage error (missing/invalid flags) |

## Matching rules

* Distance is Euclidean, measured in canonical raster pixels. A prediction and
  a reference are eligible to pair when `distance <= tolerance` (the bound is
  inclusive). The tolerance belongs to the **prediction** (see below).
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
  F1 = 2TP / (2TP + FP + FN), the harmonic mean of the two.

### Tolerance

| Flags given | Radius for each detection |
|---|---|
| `--tolerance-px P` | `P` |
| `--tolerance-rel F` | `F × min(box.width, box.height)` of that detection's `box`. A detection without a `box` rejects the run (exit 2). |
| both | `F × short side` when the detection has a `box`; otherwise `P`. The pixel value is a **fallback, not a cap**. |

The report records `tolerance_px`, `tolerance_rel`, `tolerance_mode` and the
radius actually used for each matched pair. Choose the tolerance before you
look at results and don't adjust it afterwards.

### Zero denominators

When a denominator is 0, the metric's `value` is `null` and `undefined_reason`
explains why. The value is never shown as 0 or 1.

| Case | Precision | Recall | F1 |
|---|---|---|---|
| No predictions, some references | undefined (`no predictions`) | 0.0 | 0.0 |
| Some predictions, no references | 0.0 | undefined (`no reference receptacles`) | 0.0 |
| Both empty | undefined | undefined | undefined |

When you aggregate across pages, sum TP/FP/FN first and then divide (micro).
`score-corpus` does this for its headline numbers and reports the per-page
average (macro) separately.

### Localization

For matched pairs the report gives the count, mean, median, p95 and maximum
distance, and the mean `dx`, `dy` (prediction − reference, y down; a
consistent non-zero mean points to a systematic offset such as a wrong symbol
anchor). Percentiles use linear interpolation (numpy's default).

## Score curves

When every detection has a `confidence` (or, per `contracts.md`, a raw
`score`, used when `confidence` is absent), the report includes a
precision/recall curve:

* **Points.** One point per distinct confidence value `t`, highest first:
  detections with `confidence >= t` are matched against all references with
  the same maximum-cardinality rule, giving TP/FP/FN, P, R and F1. Nothing is
  sampled. All points are in the JSON; the Markdown shows up to 10.
* **Efficiency.** Instead of re-running the matcher at each threshold, the
  curve grows one maximum matching incrementally (Kuhn's augmenting paths, one
  search per new detection, confined to its eligibility component). After each
  batch of equal-confidence detections this is exactly the maximum matching of
  that prefix, so the TP counts equal a full re-match. The tests cross-check
  that against re-matching at every threshold. 5,000 detections on a page take
  well under a second.
* **AP** is **all-point interpolated average precision (PASCAL VOC 2010+)**:
  precision is replaced by its running maximum at equal or higher recall, and
  AP is the area under that step function from recall 0. It is not COCO's
  101-point AP and is not averaged over several tolerances.
* **Operating points:** best F1 (ties go to the higher threshold), precision at
  recall ≥ 0.95 and recall at precision ≥ 0.95. Each is `null` ("not reached")
  if no threshold satisfies it.
* **Missing confidences.** If any detection lacks a confidence, the curve is
  omitted with a note giving the count; counts and P/R/F1 are unaffected.
  With no references the curve is omitted (recall undefined).

Thresholds are in the detector's own score units; `contracts.md` says `score`
is not a probability.

## Frame dpi and runtime

* `coordinate_frame.dpi` is optional (older files omit it). When present it
  must be `200`, the canonical DPI in `contracts.md` section 2, or the run is
  rejected. The two files must agree when both declare it. The report records
  the dpi and a `dpi_check` note saying which inputs declared it.
* Detector runtime is optional: top-level `elapsed_seconds` or
  `runtime.elapsed_seconds` (both, if given, must agree; `>= 0`). It is
  copied into the report (`runtime`) and summarised as p50/p95 by
  `score-corpus`.

## Corpus scoring

```sh
python3 -m pinny_eval score-corpus --manifest corpus.json \
  --json-out corpus_report.json --md-out corpus_report.md
```

The manifest (`pinny.eval_manifest` v1) lists detections/reference pairs.
Paths are relative to the manifest:

```json
{
  "format": "pinny.eval_manifest", "format_version": 1,
  "corpus_id": "site-a-v1",
  "tolerance_px": 10,
  "pages": [
    { "detections": "a/E101.det.json", "ground_truth": "a/E101.gt.json",
      "document": "site-a", "tags": ["vector", "dense"] }
  ]
}
```

* Each pair must describe the same document version, page and frame (the same
  checks as `score`, with the reference as the expected identity). A page
  listed twice is rejected.
* `document` groups pages for the per-document table (default: `document_id`).
  `tags` are optional and give a per-tag table.
* Tolerance comes from `--tolerance-px` / `--tolerance-rel`, else from the
  manifest. If both give a value for the same field and they differ, the run
  is rejected.

The corpus report contains pooled TP/FP/FN; micro P/R/F1 (the headline) and
macro P/R/F1 (mean over pages where defined, with the number excluded); a
pooled score curve with AP, best-F1 threshold and the 0.95 operating points;
pooled localization; runtime p50/p95; per-page, per-document and per-tag
tables; and the worst pages by F1. The headline status is "Measured against
verified reference" only when every page is `verified`.

## Baseline gate

`--baseline FILE` compares this run with a baseline and exits `3` if any gated
metric drops by more than `--max-drop` (absolute, default `0.01`):

| Report | Gated metrics |
|---|---|
| `score` | curve AP, recall, F1 |
| `score-corpus` | pooled-curve AP, micro recall, micro F1 |

The baseline is a `pinny.eval_baseline` file written with `--write-baseline`,
or any earlier report JSON. A metric that is `null` in the baseline is
skipped. A metric the baseline has but this run can't compute (for example AP
after confidences disappeared) fails. The tolerance must be identical to the
baseline's; otherwise the run is rejected (exit 2). The result is written into
the report as `baseline_check`.

### CI regression gate

`fixtures/corpus_mini/` is a small frozen synthetic corpus (4 pages, 2
documents, with scores, boxes, runtimes and dpi) with a committed
`baseline.json`. From the repository root:

```sh
PYTHONPATH=evaluation python3 -m pinny_eval score-corpus \
  --manifest evaluation/fixtures/corpus_mini/manifest.json \
  --baseline evaluation/fixtures/corpus_mini/baseline.json --max-drop 0.01
```

It exits `0` today. It gates the evaluator itself (a change in matching or
metrics would move the numbers); once a real labelled mini-corpus exists,
point the same command at detector output regenerated in CI. If a change to
the numbers is intended, regenerate the baseline with `--write-baseline` and
commit it with the reason.

## Detector results vs. corrections

A detector's accuracy is measured only on what the detector itself produced
for a specific scan. The evaluator enforces this:

* A detections file must declare `"provenance": "original_detector_output"`.
  If it declares anything else, it is rejected, including a corrected pin set.
* Every detection must have `source` equal to `"detector"` (the default when
  the field is omitted). Items marked `manual`, `user_added`, `user_moved`,
  `corrected` or `edited` cause the run to be rejected.
* A reference file must declare `verification.independent_of_detector: true`,
  with one exception: status `detector_assisted_reviewed` (below). Don't build
  a reference from the user's corrected pins: the corrections started from the
  detector's output, so scoring against them would count the detector's own
  output as correct.

The final corrected pin set is the user's deliverable, not a measurement.
Report it separately, for example as the number of pins added, removed or
moved. Never report it as detector precision or recall.

## Formats (v1)

Coordinates are in the page's **canonical raster** (`canonical_raster_px`, 200
DPI), with the origin at the top-left and y increasing downward. Points must
lie within `[0, width] × [0, height]`. IDs must be unique within a file.

### `pinny.detections`

```json
{
  "format": "pinny.detections",
  "format_version": 1,
  "provenance": "original_detector_output",
  "scan_id": "scan-2026-09-23T10:00:00Z-abc",
  "document": { "document_id": "doc-123", "document_version": "sha256:…", "page_index": 0 },
  "coordinate_frame": { "space": "canonical_raster_px", "dpi": 200, "width": 7200, "height": 4800,
                        "origin": "top-left", "y_axis": "down" },
  "detector": { "name": "pinny-detector", "version": "git:abc1234", "settings": { "threshold": 0.6 } },
  "elapsed_seconds": 4.2,
  "detections": [
    { "id": "det-1", "x": 1234.5, "y": 678.0, "confidence": 0.91,
      "box": { "x": 1215, "y": 659, "width": 39, "height": 38 }, "source": "detector" }
  ]
}
```

Optional: `dpi`, `elapsed_seconds` (or `runtime.elapsed_seconds`), and per
detection `confidence` (or `score`) and `box`. Other fields from the scan
result (`template`, `created_at`, `rotation`, ...) are ignored. The report
copies the `detector` block verbatim and records the SHA-256 of the file.

### `pinny.ground_truth`

Start from [`templates/ground_truth.template.json`](templates/ground_truth.template.json).
It deliberately fails `validate` until you fill it in, because `width` and
`height` are 0.

`dataset.verification.status` is one of the following:

| Status | Meaning | How the report labels it |
|---|---|---|
| `verified` | Labelled by hand, independently of the detector, and checked by a second person | "Measured against verified reference" (applies to that dataset only) |
| `detector_assisted_reviewed` | Started from detector output, then reviewed and swept for misses (rules below) | "Measured against detector-assisted reviewed reference — recall may be overstated", plus a prominent caveat |
| `unverified` | Labelled, but not yet reviewed | "Not measured — verified reference pending"; counts show agreement only |
| `synthetic` | Hand-authored test points | "Not measured — verified reference pending"; evaluator validation only |

#### Detector-assisted references

Labelling from scratch is slow, so a reference may be bootstrapped from a
detector's output. The risk is that the receptacles the detector missed are
the same ones a reviewer of its output misses, which **overstates recall**.
Such a file is accepted only when `verification` has:

* `"status": "detector_assisted_reviewed"`;
* `"independent_of_detector": false` (saying `true` is rejected as contradictory);
* `"exhaustive_miss_check": true` — the whole page was deliberately swept for
  receptacles with no pin, with the detector overlay hidden. The file is per
  page, so the flag is per page;
* `"reviewed_by"`: the person who did the review and the sweep;
* optionally `"source_scan_id"`: the scan the labels were seeded from. Scoring
  that same scan adds a second caveat, since its misses were never proposed to
  the reviewer.

Every report (page or corpus) that includes such a page carries the caveat
"RECALL MAY BE OVERSTATED" in `status.caveats`, at the top of the Markdown
and on stdout. Precision is still meaningful. Also re-label about 10% of
assisted pages from scratch and compare them as an audit. Detections files
that contain corrected pins are still rejected.

## Labelling a real page

1. **Freeze the input.** Record the exact uploaded file's content hash
   (`document_version`), the `document_id`, the 0-based `page_index`, and the
   canonical raster `width` × `height` that Pinny renders for that page.
   Export the canonical raster image at that size.
2. **Label blind.** Open the raster in any image tool that shows pixel
   coordinates (for example GIMP's pointer panel or a simple HTML canvas).
   **Keep Pinny's detections and overlay hidden** while you label. (For a
   detector-assisted reference, see the rules above instead.)
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
   canonical resolution (for example half the symbol's diameter, or
   `--tolerance-rel 0.5`). Record the reason. Don't adjust the tolerance after
   seeing results.
8. **Score** a detections file exported from one specific scan of the same
   document version, page and raster. Keep the report together with both
   input files; the report records their hashes.

A single labelled page gives a result for that page only. Claims about the
detector in general need several labelled pages from different drawing sets;
list them in a manifest and use `score-corpus`.

## Bootstrapping labels from point CSVs

`from-points` is a small converter for labels that already exist as points,
for example symbol centres exported from a public dataset such as
FloorPlanCAD (socket classes):

```sh
python3 -m pinny_eval from-points --csv sockets.csv --out-dir gt/ \
  --dataset-id floorplancad-sockets --labeled-by "FloorPlanCAD" \
  --document-version sha256:... --width 7200 --height 4800 --scale 1.0
```

The CSV needs a header with `x,y,page,document`; optional columns `id`,
`document_version`, `width`, `height` override the flags per row. `--scale`
converts source units to canonical 200-DPI pixels. One file is written per
(document, page) and re-validated. The output says
`independent_of_detector: true` and `status: unverified` (override with
`--status`): check that the dataset's idea of a receptacle and the point it
marks match your labelling rules before you promote it to `verified`.

## Layout

```
pinny_eval/        matching, tolerance, curves, stats, corpus, baseline, convert, report, CLI
tests/             evaluator self-tests (unittest; pytest-compatible)
fixtures/synthetic/<case>/{detections,ground_truth,expected}.json
fixtures/corpus_mini/  frozen mini-corpus: manifest.json, pages/, baseline.json
templates/         blank ground-truth file
samples/           example reports generated from synthetic fixtures
INTEGRATION_CHECKS.md
```

In each fixture directory, `expected.json` was worked out by hand
independently of the evaluator. The distances (and, for `scored_curve`, the
PR points and AP) are noted in each file.
