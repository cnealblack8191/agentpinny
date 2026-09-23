# Pinny detector evaluation report

**Real-drawing accuracy:** Not measured — verified reference pending

> Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing about how the detector performs on real drawings.

## Identity

- Document: `0f8e2c1a-5b7d-4e3a-9c61-2d4b8f0a7e15` version `sha256:5d41402abc4b2a76b9719d911017c592aaf1e1b2c3d4e5f60718293a4b5c6d7e`, page index 2
- Coordinate frame: canonical_raster_px at 200 DPI, 1000x800, origin top-left, y down
- Scan: `7c1d0e2a-4b5f-4a9e-8d13-6f2b9c0a1e47`
- Detector: `opencv-template` version `git:0000000`
- Detector settings: `{"rotations":[0,90,180,270],"threshold":0.6}`
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
