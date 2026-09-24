# Learning loop (detection side)

Pinny learns from review corrections with classical computer vision only.
No LLM, API or network call is involved, and no model is downloaded. Each
stage below is a pure function of the page raster and the reviewed crops
held by the learning store. The raw `score` in a scan result (contracts §4)
is never rewritten. Learned outputs are extra fields or suggestions.

```
learning store                         detection
──────────────                         ─────────
approved + added crops ──► build_template_bank ──► [BankTemplate…] ─┐
user's original template ─┘                                         │
rejected crops ─────────► NegativeBank ─────────────────────────────┤
                                                                    ▼
                               page ──► detect_multi(detector, page, bank, settings, negatives)
                                          │  one detector run per template, pooled,
                                          │  suppress_duplicates across templates,
                                          │  template_index per candidate, negative veto
                                          ▼
approved/added + rejected §6 crops ► KnnVerifier.fit ──► verifier.rerank(page, candidates)
                                                           → (candidate, P(positive))
(score, approved?) review pairs ──► suggest_threshold / IsotonicCalibrator
                                     → ThresholdSuggestion shown to the user
```

## Modules

| Module | Public API |
|---|---|
| `pinny.detection.template_bank` | `build_template_bank(positives, max_templates=8, min_similarity=0.9, *, original=None, size=None) -> list[BankTemplate]`; `BankTemplate(image, support, member_indices, source_index, min_member_similarity, is_original).as_template(channels)`; `NegativeBank.from_crops(crops, max_items=64)`, `.max_similarity(crop)`; helpers `ncc`, `fit_to_size`, `trim_margin`, `k_medoids`, `to_gray`, `convert_channels`, `rotate_quarter` |
| `pinny.detection.multi` | `detect_multi(detector, page, templates, settings=ScanSettings(), negatives=None, *, veto_margin=0.0) -> MultiDetectionResult(result, template_indices, per_template, vetoed)`; `.candidates`, `.pairs()`, `.to_dict()` |
| `pinny.detection.verifier` | `Verifier` protocol (`score(crops) -> np.ndarray` of P(positive)); `KnnVerifier(k=7, laplace=0.1, augment_rotations=True, intensity_weight=0.5, temperature=0.01)` with `.fit(pos, neg)`, `.score(crops)`, `.rerank(page, candidates, crop_margin=24)`; optional `OnnxEmbeddingVerifier(model_path, input_size=224, …)`; `crop_with_margin`, `margin_box`, `hog_descriptor`, `hog_intensity_features` |
| `pinny.detection.calibration` | `background_from_score_map`, `background_from_candidates`, `background_stats`, `normalize_scores`, `BackgroundStats`; `IsotonicCalibrator`, `PlattCalibrator`, `pav`; `suggest_threshold(pairs, target_precision=0.95, min_labels=30) -> ThresholdSuggestion(value, precision, recall_proxy, n, reason, n_positive, n_negative)` |

## Stages

**1. Template bank.** The bank clusters the tight symbol crops of approved
and added pins, together with the original template (always kept and listed
first). Crops are compared at a common size, the original's by default, by
centre-cropping or padding with the paper colour. The distance is 1 − NCC.
Clustering is a deterministic k-medoids. The smallest k whose members all
reach `min_similarity` to their medoid is chosen, capped at `max_templates`.
Medoids are real examples at their native size, so a variant drawn 25 %
larger with heavier lines becomes its own template. The single template
misses that variant (score about 0.46), and the bank finds it
(`test_bank_recovers_scaled_heavier_variant_missed_by_single_template`).

The bank needs *tight* crops, meaning the detection box. The contracts §6
machine crop has a 24 px margin. When the crop is unclipped, remove it with
`trim_margin(crop, 24)`. Otherwise cut the box straight from the canonical
raster. A manual pin's 128 × 128 crop has no box yet, so leave manual pins
out of the bank until the store records a box for them.

**2. detect_multi.** This runs any `Detector` once per template. Grayscale
bank images are converted to the page's channel layout. The candidates are
pooled and passed through `suppress_duplicates` using the same IoU and
centre rule the detector applies across rotations. The runtime budget is
shared across templates. Each result carries `template_index`. Ordering is
deterministic: score, then y, then x, and an exact tie goes to the lower
template index. Raw NCC scores are pooled unchanged.

With `negatives`, each surviving candidate's tight crop is compared by NCC
with its best positive template (under the candidate's rotation and
`mirrored` flag, if the detector sets one) and with every rejected crop at
all four quarter turns. The candidate is dropped, and listed in `vetoed`,
when `neg > pos + veto_margin`.

**3. Verifier.** `KnnVerifier` works on contracts §6 geometry: the box plus
24 px, clipped. It embeds each crop as a unit vector made of HOG and
intensity features:

* The HOG part follows the layout of `cv2.HOGDescriptor((64,64),(16,16),(8,8),(8,8),9)`.
  It is implemented in numpy because some OpenCV 5 wheels no longer ship
  `HOGDescriptor`.
* The intensity part is a zero-mean 16 × 16 downsample of the crop.

`fit` stores every labelled crop at all four quarter turns. `score`
takes the k nearest neighbours by cosine similarity and weights each by
`exp((sim − sim_max)/temperature)`. It returns
`(W_pos + a)/(W_pos + W_neg + 2a)` with Laplace smoothing `a`. The
temperature is needed because crops of one symbol family all have a cosine
similarity of about 0.95. With raw similarities as weights, the vote is
nearly a head count.

In the tests, three positive and three negative crops are enough to push a
look-alike that the matcher scores above 0.8 below p = 0.5.
`OnnxEmbeddingVerifier` takes the path to an ONNX embedding model the user
exports, such as DINOv2 ViT-S/14, and runs the same vote over its
embeddings. It needs `onnxruntime`. If the package is missing, it raises
`DetectionError("onnxruntime_missing")`.

With a verifier in place, the matcher can run at a recall-oriented threshold
(about 0.6). The verifier's probability then orders the review queue and
filters what is shown.

**4. Calibration and threshold.**

* **Unsupervised.** Compute `z = (s − median_bg) / (1.4826·MAD_bg)` per
  template and page. The detector does not expose its score map, so
  `background_from_candidates` reruns it at a low threshold (0.3) and uses
  peaks below the operating threshold. Clutter peaks sit higher than a
  whole-map sample, but they are a consistent reference. It returns `None`
  on clean pages that have fewer than 20 such peaks.
  `background_from_score_map` takes any NCC map when one is available.
* **Supervised.** `suggest_threshold` takes `(raw score, approved?)` pairs
  from reviewed machine detections. It returns the lowest observed score
  whose precision at or above it meets the target, keeping all tied
  scores. If there are fewer than `min_labels` pairs, it returns
  `value=None` with a reason. `recall_proxy` counts only symbols the
  detector proposed. The result is a suggestion: never apply it silently.
  `IsotonicCalibrator` (PAV) or `PlattCalibrator` turn raw score into
  P(approved) for display.

## Store APIs this needs

The learning-store agent is adding these. Names are indicative.

* `store.template_bank(canonical_class, *, split="train") -> {"original": ndarray, "positives": [tight crops of approved + added pins]}`.
  It needs box and rotation on manual pins.
* `store.negative_crops(...) -> [tight crops of rejected machine detections]`.
* `store.training_crops(states=("approved","added","rejected"), split="train")`
  returns §6 crops with their labels, for `KnnVerifier.fit`. Keep machine
  crops, and manual crops once they have a box, in the same geometry.
* `store.review_stats(...) -> [(score, approved: bool)]` over reviewed
  machine pins. Pins still unreviewed are excluded, and so are pages not
  marked fully reviewed. This feeds `suggest_threshold`.
* `page_reviews` (a page-fully-reviewed flag), `templates(template_id, sha256, png)`,
  and document-level `splits`, so evaluation pages never train the bank or
  the verifier.
* Recording the template list for a multi-template scan requires a
  `contracts.md` §4 change. The proposal is `"templates": [{sha256, box?}]`
  plus a per-detection `template_index`.

## Limits

All measurements come from synthetic pages. Before any default changes,
validate the bank, the veto margin, the verifier k and temperature, and the
threshold targets on real reviewed drawings with the evaluator.
