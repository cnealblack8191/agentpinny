# Pinny detector evaluation report

**Real-drawing accuracy:** Not measured — verified reference pending

> Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing about how the detector performs on real drawings.

## Identity

- Document: `0f8e2c1a-5b7d-4e3a-9c61-2d4b8f0a7e15` version `sha256:5d41402abc4b2a76b9719d911017c592aaf1e1b2c3d4e5f60718293a4b5c6d7e`, page index 2
- Coordinate frame: canonical_raster_px at 200 DPI, 1000x800, origin top-left, y down
- DPI check: dpi not declared by ground_truth (legacy file); the other input declares 200
- Scan: `7c1d0e2a-4b5f-4a9e-8d13-6f2b9c0a1e47`
- Detector: `opencv-template` version `git:0000000`
- Detector settings: `{"rotations":[0,90,180,270],"threshold":0.6}`
- Detector runtime: not recorded
- Template: box `{"height":40,"width":40,"x":100,"y":200}`, sha256 `9f86d081884c7d65…`
- Scan created: 2026-09-23T12:00:00Z
- Detector results: `samples/contract_scan_detections.json` (sha256 `46c883878d7bda17…`), original detector output only (manual corrections are not scored)
- Reference dataset: `synthetic-contract-scan` labelled by evaluation agent (synthetic, hand-authored) on 2026-09-23; verification status **synthetic**
- Reference file: `fixtures/contracts/scan_result/ground_truth.json` (sha256 `f5b994d60b659673…`)

## Matching

- Tolerance: 10 canonical raster pixels (Euclidean, inclusive)
- Method: maximum-cardinality one-to-one matching; minimum total distance as tie-breaker

## Results

| Measure | Value |
|---|---|
| Predictions | 3 |
| Reference receptacles | 3 |
| True positives | 2 |
| False positives | 1 |
| False negatives (misses) | 1 |
| Precision | 0.6667 (2/3) |
| Recall | 0.6667 (2/3) |
| F1 | 0.6667 (4/6) |

## Localization (matched pairs)

| Measure | Value |
|---|---|
| Matched pairs | 2 |
| Mean distance px | 5.368 |
| Median distance px | 5.368 |
| p95 distance px | 8.187 |
| Max distance px | 8.500 |
| Mean dx px (prediction − reference) | -0.500 |
| Mean dy px (prediction − reference, y down) | +3.250 |

## Score curve

- AP: **0.6667** (all-point interpolated (PASCAL VOC 2010+), recall from 0; not COCO 101-point)
- Best F1: threshold 0.81: P 1.0000, R 0.6667, F1 0.8000 (TP 2, FP 0, FN 1)
- Precision at recall ≥ 0.95: not reached
- Recall at precision ≥ 0.95: threshold 0.81: P 1.0000, R 0.6667, F1 0.8000 (TP 2, FP 0, FN 1)
- 3 distinct thresholds over 3 detections and 3 references (all points in the JSON report)

| Threshold | Detections | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| 0.93 | 1 | 1 | 0 | 2 | 1.0000 | 0.3333 | 0.5000 |
| 0.81 | 2 | 2 | 0 | 1 | 1.0000 | 0.6667 | 0.8000 |
| 0.62 | 3 | 2 | 1 | 1 | 0.6667 | 0.6667 | 0.6667 |

### Matched

| Prediction | At | Reference | At | Distance px |
|---|---|---|---|---|
| det-1 | (120, 220) | g1 | (121, 222) | 2.236 |
| det-2 | (321.5, 168.5) | g2 | (321.5, 160) | 8.500 |

### False positives (unmatched predictions)

| Prediction | At | Reason |
|---|---|---|
| det-3 | (620, 520) | no_reference_within_tolerance |

### False negatives (missed reference receptacles)

| Reference | At | Reason |
|---|---|---|
| g3 | (800, 700) | no_prediction_within_tolerance |
