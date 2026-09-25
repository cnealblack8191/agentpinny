# Pinny detector evaluation report

**Real-drawing accuracy:** Not measured — verified reference pending

> Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing about how the detector performs on real drawings.

## Identity

- Document: `synthetic-doc-001` version `sha256:synthetic-v1`, page index 0
- Coordinate frame: canonical_raster_px, 1000x800, origin top-left, y down, dpi not declared
- DPI check: dpi not declared (legacy file); contracts v1 assumes 200
- Scan: `synthetic-scan-mixed_sample`
- Detector: `synthetic-fixture` version `n/a`
- Detector settings: `{"note":"hand-placed points; no detector was run"}`
- Detector runtime: not recorded
- Detector results: `fixtures/synthetic/mixed_sample/detections.json` (sha256 `1e375b29b96321cd…`), original detector output only (manual corrections are not scored)
- Reference dataset: `synthetic-mixed_sample` labelled by evaluation agent (synthetic, hand-authored) on 2026-09-23; verification status **synthetic**
- Reference file: `fixtures/synthetic/mixed_sample/ground_truth.json` (sha256 `247186155dc97a36…`)

## Matching

- Tolerance: 10 canonical raster pixels (Euclidean, inclusive)
- Method: maximum-cardinality one-to-one matching; minimum total distance as tie-breaker

## Results

| Measure | Value |
|---|---|
| Predictions | 6 |
| Reference receptacles | 5 |
| True positives | 3 |
| False positives | 3 |
| False negatives (misses) | 2 |
| Precision | 0.5000 (3/6) |
| Recall | 0.6000 (3/5) |
| F1 | 0.5455 (6/11) |

## Localization (matched pairs)

| Measure | Value |
|---|---|
| Matched pairs | 3 |
| Mean distance px | 5.745 |
| Median distance px | 5.000 |
| p95 distance px | 9.500 |
| Max distance px | 10.000 |
| Mean dx px (prediction − reference) | +2.667 |
| Mean dy px (prediction − reference, y down) | +1.333 |

## Score curve

curve omitted: 6 of 6 detections have no confidence/score, so detections cannot be ranked

### Matched

| Prediction | At | Reference | At | Distance px |
|---|---|---|---|---|
| d2 | (118, 149) | g1 | (120, 150) | 2.236 |
| d3 | (344, 147) | g2 | (340, 150) | 5.000 |
| d4 | (566, 158) | g3 | (560, 150) | 10.000 |

### False positives (unmatched predictions)

| Prediction | At | Reason |
|---|---|---|
| d1 | (123, 152) | duplicate_or_crowded: every reference within tolerance is matched to another prediction [g1 @ 3.606px → d2] |
| d5 | (700, 600) | no_reference_within_tolerance |
| d6 | (340, 431) | no_reference_within_tolerance |

### False negatives (missed reference receptacles)

| Reference | At | Reason |
|---|---|---|
| g4 | (120, 420) | no_prediction_within_tolerance |
| g5 | (340, 420) | no_prediction_within_tolerance |
