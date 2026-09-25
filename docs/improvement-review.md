# Pinny improvement review (2026-09-24)

Four parallel reviews: detection, learning store, evaluator, and a survey of
current models. No module code was changed. This file is a roadmap for the
owning sessions listed in `docs/agent-ownership.md`.

## Summary

* **Pinny doesn't learn yet.** The learning store records corrections well,
  but nothing reads them back into detection (`pinny/learning/store.py:3-4`).
  Closing that loop is the biggest "smarter" win.
* **Most drawings are vector PDFs, and Pinny ignores that.** Matching PDF
  drawing primitives or reused Form XObjects finds repeated symbols with near
  perfect precision in well under a second per sheet. Raster matching should
  be the fallback.
* **The raster matcher can be about 2–3× faster with much better recall** from
  three small changes (blur, sparse validity checks, symmetry-aware
  rotations).
* **The evaluator ignores scores**, so it can't pick a threshold, and it only
  handles one page per run.
* **Claude is useful as an optional layer.** It can read the drawing legend so
  the user doesn't draw a template box, and it can judge uncertain crops in
  batches.

All test suites pass: detection 53, learning 103, evaluation 15. The evaluator
uses `unittest`.

## Recommended architecture

```
0. Classify page      vector / scanned / mixed (images covering page, no paths)
1. Vector match       Form XObject reuse → primitive-hash match (rot + flip)
2. Raster match       OpenCV NCC: blur, pyramid, tiles, template bank
3. Learned verifier   DINOv2-S kNN vs approved/rejected crops → later RF-DETR
4. Claude (opt-in)    legend reading, batch adjudication of uncertain crops
5. Human review       approve / reject / add → feeds 1–4
```

Default to permissively licensed parts: pypdfium2, pikepdf, OpenCV, DINOv2,
SigLIP 2, OWLv2, RF-DETR (N–L), SAHI. **PyMuPDF, Ultralytics YOLO and YOLOE
are AGPL-3.0.** Avoid them unless a commercial licence is bought.

## Phase 1: quick wins (about 1–3 days, no new dependencies)

| # | Change | Owner | Impact | Effort |
|---|---|---|---|---|
| 1 | Gaussian blur page and template the same way (σ≈1.0) before `matchTemplate`; new `ScanSettings.blur_sigma`. Measured: worst sub-pixel score 0.82→0.96, ±5% scale 0.82→0.96, a wire drawn through the symbol 0.73→0.84. Retune threshold (≈0.85). | detection | high recall | S |
| 2 | Check validity only at pixels ≥ threshold, not over the whole score map (`opencv_matcher.py:183-203`). About 2.4 s of 7.5 s saved. | detection | ~30% faster | S |
| 3 | Skip rotations the template is symmetric under (NCC(t, rot180 t) ≥ 0.97). Report a canonical rotation label. | detection | 2× on duplex symbols; stable labels | S |
| 4 | **Bug:** the deadline is checked *after* the last rotation (`opencv_matcher.py:118`), so a finished scan can raise a timeout and lose its results. Check it before each rotation after the first instead. | detection | correctness | S |
| 5 | Evaluator: PR curve, AP, best-F1 threshold, precision at recall 0.95, using the `confidence` it already reads (`inputs.py:138`). | evaluation | makes tuning possible | S |
| 6 | Evaluator: `score-corpus --manifest` that sums results across pages, with per-page and worst-page tables, runtime p50/p95, and localization error stats. | evaluation | high | M |
| 7 | Store: `suggest_threshold(review_stats, target_precision=0.95)`, suggested only and never applied silently. `review_queue(scan_id, strategy="margin")` to show uncertain pins first. | learning | first closed loop | S |

## Phase 2: learn from corrections (about 1–2 weeks)

* **Store prerequisites** (and a schema-migration path, since any version bump
  is currently refused at `store.py:444`):
  * `page_reviews(canonical_page_id, status)`: a "page fully reviewed" flag,
    without which there are no safe background or negative examples.
  * `templates(template_id, sha256, png, class_label)`.
  * `splits(document_id, split)`: split by *document* so evaluation pages
    never leak into training.
  * Box and rotation on manual pins.
* **Template bank:** cluster approved and added crops into K templates per
  class and match them all. Drop a candidate that correlates better with a
  rejected crop than with any positive template. Needs a `contracts.md` change
  so a scan can record a template list.
* **Embedding kNN verifier:** DINOv2 ViT-S/14 on ONNX, about 10–20 ms per crop
  on CPU. Store vectors per `crop_key`. Re-rank candidates by a
  similarity-weighted vote of their labelled neighbours. Then stage 2 can run
  at a low threshold (~0.6) for recall while the verifier restores precision.
* **Coarse-to-fine pyramid and tiling:** half-resolution pre-pass (0.18 s vs
  1.2 s per rotation), full-resolution refinement around peaks, strips run in
  a thread pool. About 4–6× faster, memory ~900 MB → ~150 MB, and a timeout
  that actually interrupts the scan.
* **Mirrored symbols** as extra "rotations", with duplicates dropped by the
  symmetry test.
* **Bootstrap real labels:** allow a `detector_assisted_reviewed` reference
  status (with a recall caveat and a 10% from-scratch audit). The evaluator
  currently refuses any reference not labelled independently of the detector
  (`inputs.py:247`). FloorPlanCAD has socket classes that can be used for
  pretraining or stress tests.

## Phase 3: vector-first and AI layers

* **Vector matcher:** walk content streams (pikepdf) and group `Do` calls by
  XObject for block-based exports. For flattened exports, pull paths
  (pypdfium2 or pdfminer), group them into connected parts, normalise them,
  and hash them across the 8 rotations and flips. This is the largest
  speed and precision gain for CAD-exported sheets.
* **Claude, opt-in and behind an API key.** Send crops only, and cache verdicts
  in the store:
  * *Legend reader:* crop the legend or symbol schedule and ask
    `claude-sonnet-5` or `claude-opus-5-5` for structured output
    `{symbol_bbox, meaning, variant}`. Each entry becomes a template. That
    removes the manual box, at about one call per drawing set.
  * *Adjudicator:* grids of 20–40 numbered uncertain crops, plus approved and
    rejected examples, sent to `claude-haiku-4-5-20251001` or Sonnet 5 for
    per-crop JSON verdicts. Use the Batch API (half price) and prompt caching
    for the exemplars.
  * *Tags and circuit numbers:* on vector PDFs, take the text straight from
    the PDF; use OCR only for scanned sheets.
* **One-shot models for scanned sheets:** OWLv2 image-guided detection
  (Apache, runs on CPU) with SAHI tiling. SAM 3 exemplar prompts are stronger
  but realistically need a GPU.
* **Trained detector:** once there are about 300–500 labels across 20 or more
  fully reviewed pages, fine-tune RF-DETR (Apache) on 1024 px tiles exported as
  COCO or YOLO from the store, with a recorded dataset manifest. Plug it in
  through the `Detector` protocol and score it only on held-out documents.

## Learning store integrity issues

1. `export()` reads each scan in separate SELECTs with no enclosing
   transaction (`store.py:815-861`), so a review that commits mid-export can
   pair a pin with a mismatched event list. Wrap the export in one read
   transaction.
2. Idempotent replay rebuilds the pin from its current row, not from the
   event's `new_state` (`store.py:658`).
3. `reviewer` is part of the idempotency hash (`store.py:647-649`). A retry
   after the reviewer identity changes raises `IdempotencyConflict`.
4. `_Tx.__exit__` doesn't roll back if COMMIT fails (`store.py:459`), which
   leaves the connection unusable.
5. A single connection with `check_same_thread=True` (`store.py:413`) means a
   threaded viewer server must open one store per thread.
6. `crop_key` ignores the renderer version, and the stored sha256 is never
   re-checked (`store.py:740,790`).

## Evaluation and CI

* Add a GitHub workflow that runs all three suites, plus `score-corpus` on a
  frozen mini-corpus with a `--baseline` gate that fails if AP or recall drops.
* A `bench/` runner (outside `evaluation/`, which must not import `pinny`) that
  sweeps `ScanSettings` and templates and writes a leaderboard ranked by AP,
  then F1, then runtime.

## Caveats

The detection measurements come from synthetic pages. None of this has been
validated on real drawings yet. Phase 1 items 5–6 plus a small real labelled
set should come before any default setting changes.

## Implementation status (2026-09-24)

Implemented on `claude/intelligent-wozniak-3kf3wk`. The LLM/Claude layers are
deliberately left out of the app for now.

| Area | Done | Docs |
|---|---|---|
| Raster detection | Deadline bug fix; `blur_sigma`; sparse validity and variance gate; symmetry-aware rotations; opt-in mirrored search; coarse-to-fine pre-pass; threaded strips. Synthetic Arch D page: ~8–9 s / +664 MB → ~0.7 s / +100 MB with defaults. | `detection.md` |
| Learning (detection side) | Template bank (k-medoids), `detect_multi` with negative veto, kNN verifier (HOG + intensity), optional ONNX embedding verifier, isotonic/Platt calibration, `suggest_threshold`. | `learning-loop.md` |
| Learning store | All six integrity fixes; schema migrations (v3); page review status, templates, class labels, manual boxes, document splits; `review_stats`, `review_queue`, crop lists for the bank, COCO `export_dataset`. | `learning-store.md` |
| Evaluator | F1, localization stats, relative tolerance, PR curve / AP / best-F1 threshold, `score-corpus`, dpi validation, runtime, `detector_assisted_reviewed` labels, `--baseline` gate, mini corpus, CSV converter. | `evaluation/README.md` |
| Vector matcher | Page classification, exact PDF→canonical mapping for all `/Rotate`, Form XObject reuse matcher, flattened-path matcher tolerant of crossing wires. 500 symbols + 20k clutter segments: 2.2 s (paths), 0.6 s (XObject). | `vector-matching.md` |
| Model packages | `pinny/model`: `.pinny` file format (template bank, negative veto, kNN or ONNX verifier, calibration, decision rule), integrity hashes, optional HMAC signing, `train_model`, `promotion_check`, CLI. | `model-package.md` |
| CI | `.github/workflows/tests.yml`: all suites + mini-corpus regression gate. | — |

Still open:

* Connect the pieces in the app. The viewer or scan service should try
  `pinny.vector` first, fall back to raster, and feed store crops into
  `build_template_bank` and `KnnVerifier`.
* A scan record that lists several templates.
* The foundation's `pyproject.toml` and render service.
* A `bench/` settings sweep.
* A real, labelled drawing set to confirm the synthetic results and retune
  the 0.80 threshold. Blur leaves only about 0.015 margin to near-identical
  symbol variants.
