# Phase 2 evaluation: benchmark and promotion gate

Owner: chat E (`pinny/benchmark/`, `evaluation/`, `tests/benchmark/`).
Contracts: `docs/phase2-contracts.md` P4 (dataset), P6 (model interfaces),
P7 (scan modes) and P9 (evaluation and the promotion gate).

The benchmark measures each scan mode on the **test split** of a
`pinny.dataset` v1 directory. It scores every page with the Phase 1
evaluator in `evaluation/pinny_eval`. The promotion report then decides,
by the P9 rules, whether a model may replace the template baseline.

## Commands

```sh
# 1. Benchmark all three modes on the test split.
python -m pinny.benchmark run \
  --dataset "$PINNY_DATA_DIR/datasets/<dataset_id>" \
  --modes template,template+verifier,model \
  --verifier "$PINNY_DATA_DIR/models/<verifier_model_id>" \
  --detector "$PINNY_DATA_DIR/models/<detector_model_id>" \
  --out bench/<run-name>

# 2. Apply the promotion gate to one candidate.
python -m pinny.benchmark promote-report \
  --benchmark bench/<run-name> \
  --candidate template+verifier \
  --out bench/<run-name>/promotion-verifier.json \
  --md-out bench/<run-name>/promotion-verifier.md
```

`run` options:

| Option | Meaning |
|---|---|
| `--modes` | A comma list of `template`, `template+verifier` and `model`. The gate needs `template` plus the candidate. |
| `--verifier` / `--detector` | Model directories (P5). They are loaded through the P6 classes (`Verifier.load`, `PointDetector.load`), so torch is only imported when you pass them. |
| `--template-threshold` | The template matching threshold. The default is the detector's default (0.80). It is recorded in the summary. |
| `--reference-status` | `unverified` (default) or `verified`. See "Reference status" below. It is ignored for synthetic datasets, which are always `synthetic`. |
| `--overwrite` | Allow writing into a non-empty `--out`. |

Exit codes: `0` when a report is written (including `"promote": false`),
`2` for rejected input (with a stable error code), and `64` for a usage error.

## What `run` does

1. It reads `manifest.json` and keeps only the `detector` entries whose
   `split` is `test`. These are complete pages (P3). Train and val pages
   are never touched.
2. It writes one `pinny.ground_truth` v1 file per page from the page's
   `points`, under `reference/`.
3. For each page and each mode, it builds a contracts §4 scan result with
   the P7 `mode` and extras. It then converts that result into
   `pinny.detections` v1 with the evaluator's own `adapt-scan` code.
   - `template`: the OpenCV template matcher, unchanged from Phase 1.
   - `template+verifier`: the same template candidates, rescored by
     `Verifier.score`. Candidates below `verifier.threshold` are dropped and
     recorded under `suppressed`. Each kept detection has `template_score`
     and `verifier_score`, and `score` equals `verifier_score`.
   - `model`: `PointDetector.detect_points` with the model's own threshold
     and `max_points=500`. Each point gets a 40 × 40 px box centred on it
     and `rotation = 0`. The box is kept in float px so its centre is
     exactly the model point.
4. It scores each detections file against its page reference at **12 px**,
   using the same loaders and report builder as `pinny_eval score`.
   Suppressed candidates are never scored.
5. It **sums** TP/FP/FN over pages and then divides. It does not average
   per-page ratios. It writes `summary.json` (`pinny.benchmark` v1) and
   `summary.md`.

Output layout:

```
<out>/
  summary.json, summary.md
  reference/<page_key>.ground_truth.json
  template/<page_key>.detections.json, <page_key>.report.json
  template+verifier/...
  model/...
```

`summary.json` records the dataset id and path, `synthetic_only`, split,
tolerance, page, document and reference point counts, the evaluator
version, the model ids and model metadata for each mode, the template
settings, and the template box used on each page.

### The template choice

Template mode needs a template on every page, which a person would
normally drag. The benchmark uses a **40 × 40 px box centred on the first
test point of each page**, in manifest order, shifted to lie inside the
page. This rule and every box are written to `template_choice` in the
summary and printed in `summary.md`.

Two consequences, both stated in the report:

* The template always matches its own location, so the baseline gets that
  hit on nearly every page. This makes the baseline slightly optimistic,
  which makes the gate stricter on candidates, not looser.
* A test page with no points has no template. Both template modes report
  no detections there. The model mode can still produce false positives
  there, as it would in the app.

### Reference status

The page references come from reviews in Pinny: approved and manually
added pins on complete pages. Approved machine pins were first proposed by
the template detector, so one careless review would favour the template
baseline. The evaluator labels the result to match:

| Dataset | Reference status | Report label |
|---|---|---|
| `source.synthetic: true` | `synthetic` | "synthetic — not real-drawing accuracy" |
| real, reviewed by one person | `unverified` (default) | agreement with that review, not yet real-drawing accuracy |
| real, every test page checked by a second person | `verified` (pass `--reference-status verified`) | measured against a verified reference |

The promotion gate uses the P9 conditions only. The reference status is
recorded in the summary so that anyone who reads a promotion can see what
it was measured against.

## The promotion report (`pinny.promotion` v1)

`promote-report` compares the candidate mode with the `template` baseline
on the same pages of one benchmark run. It refuses to compare runs that
used different pages or a tolerance other than 12 px. The report is
recommended for promotion (`"promote": true`) only when **every**
condition holds:

| Condition | Requirement |
|---|---|
| `recall_not_below_baseline` | candidate recall ≥ baseline recall |
| `precision_within_margin` | candidate precision ≥ baseline precision − 0.02 |
| `meaningful_improvement` | recall or precision improves by ≥ 0.02 |
| `min_test_documents` | the test split has ≥ 3 documents |
| `min_reference_points` | the test split has ≥ 100 reference points |
| `not_synthetic_only` | the dataset is not synthetic (`source.synthetic`) |

Precision and recall are computed from the summed counts as exact
fractions. A change of exactly 0.02 passes, and 0.0199… fails. If either
metric is undefined (a zero denominator), the metric conditions fail.

Every condition is listed with `passed`, `value` and `requirement`, and the
names of failing conditions are listed in `failed_conditions`. The report
also carries `model_id` and `kind` (the verifier for `template+verifier`,
the detector for `model`), the dataset id, split, tolerance, both modes'
counts and metrics, and the sha256 of the `summary.json` it was built from.
`pinny.models.registry.promote(model_id, evidence_path)` (P8) accepts it
as evidence only when `model_id` matches and `promote` is `true`.

A synthetic dataset never promotes, whatever its scores.

## Labelling real pages to create the test split

The gate needs **≥ 3 test documents and ≥ 100 reference points** from
real drawings. The split is fixed by `sha256(document_id)` (P4), so you
don't choose which documents are test documents. You find out, and then
label those documents completely.

1. **Upload several drawing sets.** Each drawing set is one `document_id`,
   and about 15 % of documents land in `test`. Upload around 20 or more
   drawing sets to get at least 3 test documents. Re-uploading a new
   version keeps its `document_id` and its split.
2. **Find the test documents.** Review a few pins on each document, then
   build a dataset (`python -m pinny.training ...`, see
   `docs/phase2-dataset.md`). In `manifest.json`, each `verifier` entry
   carries its `document_id` and `split`. The documents whose entries say
   `"split": "test"` are your test documents.
3. **Decide what counts before you review.** Write down which symbols count
   as receptacles (duplex, GFCI, quad, floor, …), how to treat legends and
   detail callouts, and which point on the symbol to mark. Use the same
   rules on every test page. `evaluation/README.md` ("Labelling a real
   page") has the full checklist.
4. **Make every test page complete.** For each page of a test document:
   scan it with a template, then approve or reject **every** pin. Add a
   manual pin for **every** receptacle the scan missed, and look at the
   whole sheet, not only near the machine pins. A page counts only when its
   latest scan has zero unreviewed pins (P3). A page you skip is simply left
   out, and it does no harm.
5. **Guard against baseline bias.** Machine pins come from the template
   detector. Reject any pin that isn't exactly a receptacle, even a close
   one, and hunt for misses as carefully as you check hits. If you can,
   have a second person check every test page against the drawing. Then
   run the benchmark with `--reference-status verified`.
6. **Rebuild the dataset and check the counts.** `counts.detector.test`
   in the manifest should show at least 3 documents' pages and at least 100
   points. `python -m pinny.benchmark run` prints the page, document and
   reference point counts, and `promote-report` shows them as conditions.
7. **Don't tune on test.** Choose model thresholds on `val` (P5). Never
   change a threshold, the template rule or the tolerance after seeing test
   results. If you do change one, the next run is a new experiment and
   needs new test pages.

## Evaluator changes (still stdlib-only, no `pinny` import)

* A top-level `mode` in a detections file is validated and recorded in the
  report's `detector_provenance`.
* `suppressed` must be a list if present. It is never scored, and only
  `suppressed_count` is recorded.
* Per-detection P7 extras (`template_score`, `verifier_score`) were already
  kept but ignored. `evaluation/tests/test_scan_modes.py` now covers all of
  this.

## Tests

```sh
python -m pytest tests/benchmark
cd evaluation && python -m unittest discover -s tests
```

* `test_gate.py`: every condition alone blocks promotion; boundaries are
  exact at 0.02; undefined metrics block; synthetic data never promotes.
* `test_aggregate.py`: counts are summed, not averaged.
* `test_end_to_end.py`: a tiny synthetic manifest (three test documents and
  one train document) with fake P6 models, run through the CLI, then
  `promote-report`. No torch is needed.
