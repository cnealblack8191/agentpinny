# Pinny detector evaluation report

**Real-drawing accuracy:** Not measured — verified reference pending

> Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing about how the detector performs on real drawings.

## Identity

- Document: `synthetic-doc-001` version `sha256:synthetic-v1`, page index 0
- Coordinate frame: canonical_raster_px, 1000x800, origin top-left, y down, dpi 200
- DPI check: all inputs declare 200 DPI
- Scan: `synthetic-scan-scored_curve`
- Detector: `synthetic-fixture` version `n/a`
- Detector settings: `{"note":"hand-placed points; no detector was run"}`
- Detector runtime: 1.25 s (`elapsed_seconds`)
- Detector results: `fixtures/synthetic/scored_curve/detections.json` (sha256 `9c6250447cf82c90…`), original detector output only (manual corrections are not scored)
- Reference dataset: `synthetic-scored_curve` labelled by evaluation agent (synthetic, hand-authored) on 2026-09-24; verification status **synthetic**
- Reference file: `fixtures/synthetic/scored_curve/ground_truth.json` (sha256 `9ad11f209c9fadec…`)

## Matching

- Tolerance: 10 canonical raster pixels (Euclidean, inclusive)
- Method: maximum-cardinality one-to-one matching; minimum total distance as tie-breaker

## Results

| Measure | Value |
|---|---|
| Predictions | 6 |
| Reference receptacles | 4 |
| True positives | 3 |
| False positives | 3 |
| False negatives (misses) | 1 |
| Precision | 0.5000 (3/6) |
| Recall | 0.7500 (3/4) |
| F1 | 0.6000 (6/10) |

## Localization (matched pairs)

| Measure | Value |
|---|---|
| Matched pairs | 3 |
| Mean distance px | 2.333 |
| Median distance px | 2.000 |
| p95 distance px | 3.800 |
| Max distance px | 4.000 |
| Mean dx px (prediction − reference) | +1.000 |
| Mean dy px (prediction − reference, y down) | +1.333 |

## Score curve

- AP: **0.5417** (all-point interpolated (PASCAL VOC 2010+), recall from 0; not COCO 101-point)
- Best F1: threshold 0.5: P 0.5000, R 0.7500, F1 0.6000 (TP 3, FP 3, FN 1)
- Precision at recall ≥ 0.95: not reached
- Recall at precision ≥ 0.95: threshold 0.9: P 1.0000, R 0.2500, F1 0.4000 (TP 1, FP 0, FN 3)
- 5 distinct thresholds over 6 detections and 4 references (all points in the JSON report)

| Threshold | Detections | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| 0.9 | 1 | 1 | 0 | 3 | 1.0000 | 0.2500 | 0.4000 |
| 0.8 | 2 | 1 | 1 | 3 | 0.5000 | 0.2500 | 0.3333 |
| 0.7 | 3 | 2 | 1 | 2 | 0.6667 | 0.5000 | 0.5714 |
| 0.6 | 4 | 2 | 2 | 2 | 0.5000 | 0.5000 | 0.5000 |
| 0.5 | 6 | 3 | 3 | 1 | 0.5000 | 0.7500 | 0.6000 |

### Matched

| Prediction | At | Reference | At | Distance px |
|---|---|---|---|---|
| d1 | (101, 100) | g1 | (100, 100) | 1.000 |
| d3 | (202, 100) | g2 | (200, 100) | 2.000 |
| d5 | (300, 104) | g3 | (300, 100) | 4.000 |

### False positives (unmatched predictions)

| Prediction | At | Reason |
|---|---|---|
| d2 | (500, 500) | no_reference_within_tolerance |
| d4 | (100, 103) | duplicate_or_crowded: every reference within tolerance is matched to another prediction [g1 @ 3.000px → d1] |
| d6 | (700, 700) | no_reference_within_tolerance |

### False negatives (missed reference receptacles)

| Reference | At | Reason |
|---|---|---|
| g4 | (400, 100) | no_prediction_within_tolerance |
