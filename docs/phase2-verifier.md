# Phase 2 verifier

The verifier rescores each template-match candidate and returns
`p(receptacle)` (docs/phase2-contracts.md P1, P6). It is a small CNN that
trains from scratch on CPU in minutes. No pretrained weights are used or
downloaded.

| File | What it holds |
|---|---|
| `pinny/models/artifact.py` | `pinny.model` v1 save/load (P5). Shared with the point detector. |
| `pinny/models/verifier.py` | The crop rule, the network, and `Verifier.load` / `Verifier.score` (P6). |
| `pinny/training/verifier_train.py` | The trainer and the `train-verifier` CLI command. |

## Usage

```
python -m pinny.training train-verifier --dataset $PINNY_DATA_DIR/datasets/<id> [--epochs 30]
    [--batch-size 64] [--lr 0.001] [--seed 0] [--patience 5] [--models-dir DIR]
```

This writes `$PINNY_DATA_DIR/models/<model_id>/` and prints a JSON summary
with the model dir, the threshold and the val and test scores.

```python
from pinny.models.verifier import Verifier
v = Verifier.load(model_dir)
probs = v.score(render_service.render_page(version, 0), [(x, y), ...])
keep = [p >= v.threshold for p in probs]
```

## Crop rule (shared with the dataset)

`to_gray` and `crop_patch` in `pinny/models/verifier.py` are the reference
implementation of the P4 verifier crop. They need no torch, so the dataset
builder can import them and produce byte-identical crops.

* Grayscale is `cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)`, which uses the
  ITU-R 601 weights. RGBA drops alpha first, and 2-D input passes through
  unchanged.
* A pin at `(x, y)` gets the 96 × 96 window `[x − 48, x + 48)` (pixel `i`
  covers `[i, i+1)`). Snapped to the pixel grid, its top-left pixel is
  `(floor(x + 0.5) − 48, floor(y + 0.5) − 48)`.
* Pixels outside the page are white (255).

`Verifier.score` crops this way on the page it is given. The tests check
this crop against an independent pad-and-slice version, and check that
`score()` on the rendered page sees exactly the PNG crops stored in a
dataset.

## Architecture: `verifier-cnn-v1`

The input is 1 × 96 × 96 "ink" `1 − gray/255`. White paper and the
padding are 0.

| Layer | Output |
|---|---|
| 4 × [conv 3×3 (no bias), BatchNorm, ReLU, max-pool 2] with 16, 32, 64, 64 channels | 64 × 6 × 6 |
| adaptive avg-pool to 3 × 3, flatten | 576 |
| linear 576→64, ReLU, dropout 0.3 | 64 |
| linear 64→1 (logit), then sigmoid | 1 |

The model has **97,393 parameters**, and its weights file is about 400 KB.
The 3 × 3 pooling keeps coarse position, so a symbol centred in the crop
reads differently from one near the edge. That separates "this candidate
is the symbol" from "a symbol is nearby".

## Training

* **Initialisation:** from scratch. `torch.manual_seed(seed)` and one
  seeded `torch.Generator` drive the weight init, the sampling and the
  augmentation, so the same dataset and seed give the same weights (and
  `weights_sha256`) on the same machine and torch build. The default seed
  is 0.
* **Sampling:** class-balanced. A `WeightedRandomSampler` with weight
  `1 / class_count` draws `len(train)` samples per epoch with replacement.
  This keeps a negative-heavy review set from collapsing into "always
  reject".
* **Augmentation** (train only, on the batch):
  - a random quarter rotation (0/90/180/270)
  - random horizontal and vertical flips
  - contrast × U(0.75, 1.25) and brightness + U(−0.1, 0.1) on the ink,
    clamped to [0, 1]
  - with probability 0.3, a 3 × 3 Gaussian blur with σ ~ U(0.4, 1.0)
* **Optimiser:** AdamW, with lr 1e-3, weight decay 1e-4, batch 64 and
  binary cross-entropy on the logit.
* **Early stopping:** on val loss (plain BCE, no augmentation). Patience is
  5 epochs, min_delta is 1e-4, and the maximum is `--epochs` (default 30).
  The weights from the best epoch are restored before saving.
* **Operating point:** the threshold is chosen on **val** as the value
  that maximises F1 for the rule `p >= threshold`. The candidates are the
  distinct val probabilities, and ties go to the higher threshold. The
  threshold is saved in `operating_point` (`chosen_on: "val"`,
  `criterion: "max_f1"`).
* **Test** is scored **once**, at the val threshold, after training. It
  is never used for tuning.

The trainer refuses to run when the train split lacks either class, when
val is empty, or when val has no positives (`TrainingError`, with codes
`insufficient_data` and `no_val_positives`).

### Training time (measured)

These runs used a CPU container with 4 torch threads and the synthetic
fixture from the tests, scaled up to 40 documents (672 train, 144 val and
144 test crops):

* **26.5 s** in total for 11 epochs. Early stopping picked epoch 6.
  That is about 2.4 s per epoch, or about 3.5 ms per training crop per
  epoch.
* Time grows linearly with sample count. About 5,000 labelled pins would
  take about 20 s per epoch, or at most about 10 minutes for 30 epochs.

The synthetic task is easy: val and test scored F1 1.0 and ROC AUC 1.0.
Those numbers are **synthetic, not real-drawing accuracy**.

### Inference time (measured)

On a 3400 × 2200 page, scoring 500 points took **0.94 s** on 4 threads
and **2.7 s** on 1 thread. The P6 budget is 5 s. Crops are cut in NumPy,
then run through the network in batches of 256 under
`torch.inference_mode()`. A test enforces the 5 s budget.

## Metrics in `model.json`

`metrics.val` and `metrics.test` have the same keys. Every
threshold-dependent number uses the val threshold.

| Key | Meaning |
|---|---|
| `n`, `pos`, `neg` | Sample counts in the split. |
| `threshold` | The operating threshold (the same value in both splits). |
| `tp`, `fp`, `fn`, `tn` | Confusion counts at the threshold. A positive is a human-approved or manually added pin. A negative is a rejected machine pin. |
| `precision` | `tp / (tp + fp)`: the share of candidates the verifier keeps that are real receptacles. |
| `recall` | `tp / (tp + fn)`: the share of real receptacles the verifier keeps. A missed one is a receptacle that template+verifier mode would drop. |
| `f1` | The harmonic mean of precision and recall (what the threshold maximises on val). |
| `accuracy` | `(tp + tn) / n`. It is less informative when the classes are imbalanced. |
| `loss` | The mean binary cross-entropy of the probabilities. Early stopping uses this on val. |
| `roc_auc` | The probability that a random positive scores above a random negative. It does not depend on the threshold and is `null` if a class is missing. |
| `average_precision` | The area under the precision–recall curve. It does not depend on the threshold. |

`metrics.train` records the run: sample counts, `seconds`, `epochs_run`,
`best_epoch`, `steps`, `best_val_loss`, `parameters`, and a per-epoch
`history` of train and val loss.

These are **crop-level** scores on labelled pins. Page-level precision and
recall of the full `template+verifier` pipeline, at 12 px tolerance
against complete pages, come from the benchmark (P9). Only the benchmark
feeds promotion. Probabilities are not calibrated across models (P6).
When `synthetic_only` is true, none of these numbers are real-drawing
accuracy.

## Artifact (`pinny.model` v1)

`save_artifact(models_dir, kind=, arch=, state_dict=, metadata=)` does the
following:

* It serialises the state_dict (CPU tensors) and computes its sha256.
* It derives `model_id = <kind>-<UTC yyyymmddThhmmssZ>-<sha[:8]>` and
  fills in the reserved fields (`format`, `model_id`, `kind`, `arch`,
  `weights_sha256`, `created_at`, and by default `code_version`).
* It writes into a temporary directory, then renames it into place.
* It refuses to overwrite an existing `model_id`, because artifacts are
  immutable.

`load_artifact(model_dir, kind=)` does the following:

* It validates `model.json`.
* It checks the sha256 of `weights.pt` (error `model_weights_mismatch`).
* It calls `torch.load(..., weights_only=True, map_location="cpu")` on the
  same bytes it hashed. Anything that is not a plain state_dict is
  rejected with `invalid_model_weights`.

`read_metadata` works without torch. Every error is a `ModelArtifactError`
(a `PinnyError`).

The verifier's `input` also records `pad_value: 255`. Its `train_config`
records the optimiser, the sampling, the early-stopping settings and the
full augmentation config.

## Tests

`tests/training/test_verifier_fixture.py` builds a tiny P4 dataset from
`tests/factory.py` glyphs:

* **Positives:** receptacle glyphs with jitter.
* **Negatives:** filled squares, blank paper and off-centre glyphs.
* **Documents:** 4, split into train, train, val and test.

The other test files cover the following:

* `tests/training/test_verifier_train.py`: few-step training, artifact
  fields, seed determinism, that the model learns the fixture, early
  stopping, threshold and AUC maths, augmentation range, bad-dataset
  refusals, and the CLI.
* `tests/models/test_verifier_artifact.py`: round trip, immutability, a
  tampered weights file, a pickle-bomb weights file with a matching sha,
  and metadata validation.
* `tests/models/test_verifier.py`: the crop against the reference, the
  centring convention, the grayscale layouts, `score()` crops matching
  the stored dataset crops exactly, and 500 points in under 5 s.

Tests that need torch skip cleanly without it. The whole set runs in
about 12 s.

## Notes for other chats

* **Dataset (A):** please cut verifier crops with
  `pinny.models.verifier.to_gray` + `crop_patch`, or match the rule above
  exactly. The dispatcher `pinny/training/__main__.py` and
  `pinny/training/__init__.py` did not exist yet. This branch adds a
  minimal version: a `COMMANDS` table where each module exposes
  `main(argv) -> int`. Its only entry is `"train-verifier"`. When merging,
  keep A's dispatcher and add that one line.
* **Detector (C):** use `save_artifact(..., kind="detector")` and
  `load_artifact(model_dir, kind="detector")`.
* **Integration (D):** `Verifier.load(model_dir)` then `.score(page_rgb,
  points)` and `.threshold`, `.model_id`. Import it lazily, because it
  pulls in torch.
