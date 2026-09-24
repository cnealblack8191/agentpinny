# Pinny corpus evaluation: corpus-mini-v1

**Real-drawing accuracy:** Not measured — verified reference pending

> Synthetic fixture. These numbers validate the evaluator's behaviour; they say nothing about how the detector performs on real drawings.

- Manifest: `fixtures/corpus_mini/manifest.json`
- Pages: 4 across 2 documents; verification statuses: synthetic 4
- Detectors: `synthetic-fixture frozen-mini-v1`
- Tolerance: 10 canonical raster pixels (Euclidean, inclusive)

## Pooled results

| Measure | Value |
|---|---|
| Predictions | 50 |
| Reference receptacles | 48 |
| True positives | 39 |
| False positives | 11 |
| False negatives (misses) | 9 |
| Micro precision | 0.7800 (39/50) |
| Micro recall | 0.8125 (39/48) |
| Micro F1 | 0.7959 (78/98) |
| Macro precision | 0.6034 (over 4 pages) |
| Macro recall | 0.8045 (over 3 pages; 1 undefined) |
| Macro f1 | 0.6034 (over 4 pages) |

_micro = pooled TP/FP/FN then divided (the headline); macro = unweighted mean of per-page values over pages where the value is defined._

## Pooled score curve

- AP: **0.8057** (all-point interpolated (PASCAL VOC 2010+), recall from 0; not COCO 101-point)
- Best F1: threshold 0.768: P 0.9744, R 0.7917, F1 0.8736 (TP 38, FP 1, FN 10)
- Precision at recall ≥ 0.95: not reached
- Recall at precision ≥ 0.95: threshold 0.768: P 0.9744, R 0.7917, F1 0.8736 (TP 38, FP 1, FN 10)
- 47 distinct thresholds over 50 detections and 48 references (all points in the JSON report)

| Threshold | Detections | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| 0.988 | 1 | 1 | 0 | 47 | 1.0000 | 0.0208 | 0.0408 |
| 0.944 | 6 | 6 | 0 | 42 | 1.0000 | 0.1250 | 0.2222 |
| 0.911 | 11 | 11 | 0 | 37 | 1.0000 | 0.2292 | 0.3729 |
| 0.868 | 16 | 16 | 0 | 32 | 1.0000 | 0.3333 | 0.5000 |
| 0.848 | 21 | 21 | 0 | 27 | 1.0000 | 0.4375 | 0.6087 |
| 0.81 | 29 | 28 | 1 | 20 | 0.9655 | 0.5833 | 0.7273 |
| 0.779 | 35 | 34 | 1 | 14 | 0.9714 | 0.7083 | 0.8193 |
| 0.766 | 40 | 38 | 2 | 10 | 0.9500 | 0.7917 | 0.8636 |
| 0.66 | 45 | 39 | 6 | 9 | 0.8667 | 0.8125 | 0.8387 |
| 0.602 | 50 | 39 | 11 | 9 | 0.7800 | 0.8125 | 0.7959 |

## Baseline gate

**PASSED** against `fixtures/corpus_mini/baseline.json` (max drop 0.01)

| Metric | Baseline | Current | Drop | Result |
|---|---|---|---|---|
| ap | 0.8057 | 0.8057 | +0.0000 | pass |
| recall | 0.8125 | 0.8125 | +0.0000 | pass |
| f1 | 0.7959 | 0.7959 | +0.0000 | pass |

## Localization (matched pairs)

| Measure | Value |
|---|---|
| Matched pairs | 39 |
| Mean distance px | 3.781 |
| Median distance px | 3.734 |
| p95 distance px | 5.826 |
| Max distance px | 6.325 |
| Mean dx px (prediction − reference) | +0.354 |
| Mean dy px (prediction − reference, y down) | -0.303 |

## Runtime

- p50 1.763 s, p95 2.147 s, mean 1.742 s, total 6.968 s over 4 pages

## Worst pages

| Page | F1 | FP | FN | Detections file |
|---|---|---|---|---|
| doc-beta p2 | 0.0000 | 2 | 0 | `pages/doc-beta_p2.detections.json` |
| doc-beta p0 | 0.6667 | 4 | 4 | `pages/doc-beta_p0.detections.json` |
| doc-alpha p1 | 0.8182 | 4 | 4 | `pages/doc-alpha_p1.detections.json` |
| doc-alpha p0 | 0.9286 | 1 | 1 | `pages/doc-alpha_p0.detections.json` |

## Per page

| Page | Status | Tags | Pred | Ref | TP | FP | FN | Precision | Recall | F1 | AP | Runtime s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| doc-alpha p0 | synthetic | vector | 14 | 14 | 13 | 1 | 1 | 0.9286 | 0.9286 | 0.9286 | 0.9235 | 1.72 |
| doc-alpha p1 | synthetic | vector, dense | 22 | 22 | 18 | 4 | 4 | 0.8182 | 0.8182 | 0.8182 | 0.8182 | 2.21 |
| doc-beta p0 | synthetic | scanned | 12 | 12 | 8 | 4 | 4 | 0.6667 | 0.6667 | 0.6667 | 0.6667 | 1.24 |
| doc-beta p2 | synthetic | scanned | 2 | 0 | 0 | 2 | 0 | 0.0000 | — | 0.0000 | — | 1.8 |

## Per document

| Document | Pages | TP | FP | FN | Precision | Recall | F1 | Runtime s |
|---|---|---|---|---|---|---|---|---|
| doc-alpha | 2 | 31 | 5 | 5 | 0.8611 | 0.8611 | 0.8611 | 3.93 |
| doc-beta | 2 | 8 | 6 | 4 | 0.5714 | 0.6667 | 0.6154 | 3.04 |

## Per tag

| Tag | Pages | TP | FP | FN | Precision | Recall | F1 | Runtime s |
|---|---|---|---|---|---|---|---|---|
| dense | 1 | 18 | 4 | 4 | 0.8182 | 0.8182 | 0.8182 | 2.21 |
| scanned | 2 | 8 | 6 | 4 | 0.5714 | 0.6667 | 0.6154 | 3.04 |
| vector | 2 | 31 | 5 | 5 | 0.8611 | 0.8611 | 0.8611 | 3.93 |
