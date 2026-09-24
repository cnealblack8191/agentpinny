# Phase 2 point detector

The point detector finds receptacle *points* on a canonical page raster
without a template (`docs/phase2-contracts.md` P1, P6). Owned by chat C.

| Path | What it holds |
|---|---|
| `pinny/models/point_detector.py` | the net, tiling, tile merging, peak picking, `PointDetector` (P6) |
| `pinny/training/detector_train.py` | targets, augmentation, sampling, focal loss, P9 threshold choice, artifact writing, CLI |
| `tests/models/test_point_detector*.py`, `tests/training/test_detector*.py` | tests (torch tests skip without torch) |

## Use

```
pip install -e ".[dev,train]"      # CPU wheels: add --index-url https://download.pytorch.org/whl/cpu
python -m pinny.training train-detector --dataset $PINNY_DATA_DIR/datasets/<dataset_id> [--epochs N]
    [--tiles-per-epoch 2048] [--batch-size 8] [--lr 0.002] [--seed 0] [--models-dir DIR]
```

It writes `$PINNY_DATA_DIR/models/<model_id>/{model.json, weights.pt}` (P5)
and prints the model id and the val and test metrics as JSON.

```python
from pinny.models.point_detector import PointDetector
det = PointDetector.load(model_dir)
det.detect_points(page_rgb)          # [{"x", "y", "score"}], canonical px, highest score first
```

## Architecture (`detector-centernet-v1`)

A CenterNet-style fully-convolutional net, 281k parameters, trained from
scratch (no pretrained weights). The input is one channel of "ink",
`(255 - gray) / 255`, so the white page and the zero padding of the
convolutions look the same.

| Stage | Layers (3 x 3 conv + BatchNorm + ReLU) | Stride | Channels |
|---|---|---|---|
| stem | conv s2, conv s2, conv | 4 | 16, 32, 32 |
| down8 | conv s2, conv | 8 | 64 |
| down16 | conv s2, conv (dilation 2) | 16 | 96 |
| top-down | 1 x 1 lateral + nearest upsample + add + conv, twice | 16 → 8 → 4 | 64, 32 |
| heat head | 3 x 3 conv, ReLU, 1 x 1 → 1 channel (bias −2.19, so p = 0.1 at start) | 4 | 1 |
| offset head | 3 x 3 conv, ReLU, 1 x 1 → 2 channels, sigmoid | 4 | 2 |

The receptive field is well over 100 px, which is about three receptacle
diameters at 200 DPI (a receptacle is about 34 px across).

**Output grid.** Output cell `(i, j)` covers page px `[4j, 4j+4) x [4i, 4i+4)`.
A point decodes to `x = 4 (j + offset_x)` and `y = 4 (i + offset_y)`, where
the offset is the sub-cell position in `[0, 1)`.

## Training

| Setting | Value |
|---|---|
| Tiles | 512 x 512 grayscale, grid stride 384, cut from whole dataset pages (P4) |
| Sampling | Pages are visited in shuffled chunks of 16 (so only 16 pages are decoded at a time). Within a chunk, grid tiles are drawn 50/50 from tiles with points and tiles without them. |
| Targets | A Gaussian with sigma 4 output cells (16 px) and a peak of exactly 1 at the cell of each labelled point. The offset target is the sub-cell position at that cell. |
| Loss | CenterNet penalty-reduced focal loss (alpha 2, beta 4), normalised by the number of peaks, plus an L1 offset loss at the peak cells |
| Augmentation | A random quarter rotation (0/90/180/270), a horizontal flip (p 0.5; with the rotations this covers all 8 symmetries), scale 0.9 to 1.1, a tile-centre jitter of ±32 px, contrast × 0.6 to 1.2, and Gaussian noise (sigma 0.03, p 0.3). |
| Optimiser | AdamW (lr 2e-3, weight decay 1e-4) with a one-cycle schedule, batch 8 |
| Defaults | 16 epochs x 2048 tiles, seed 0 (seeds Python, numpy and torch) |
| Device | CPU only. CUDA is never used. |

Labels come from the dataset's `detector` entries, which already follow the
P3 page-completeness rule: every receptacle on a page is a point, and
everything else is background.

**Threshold and scores (P5, P9).** After training, the model runs on every
`val` page and keeps candidates down to a score of 0.05. For each threshold
from 0.05 to 0.95 in steps of 0.025, predictions are matched to the labelled
points one-to-one within **12 px**. TP, FP and FN are summed over pages. The
threshold with the best F1 wins; a tie goes to the higher threshold. The
match count is a maximum-cardinality bipartite matching. That is the TP
count of the Phase 1 evaluator, whose distance tie-break never changes the
count. Then `test` is scored **once**, at that threshold. Each split's
metrics record the threshold, TP/FP/FN, precision, recall, F1, the page and
point counts and `tolerance_px`. On a synthetic dataset they also carry
`"note": "synthetic — not real-drawing accuracy"`, and the artifact is
marked `synthetic_only`.

If a dataset has no `val` pages, the threshold stays at 0.5 with
`"chosen_on": "default"` and a warning is printed. Such a model should not
be promoted.

## Inference

1. Convert the page to grayscale. It accepts gray, RGB and RGBA `uint8`.
2. Tile it at 512 px with a stride of 384. The tile origins are multiples of
   384, so they are also multiples of the output stride. The last row and
   column of tiles run past the page, and the page is padded with white
   there.
3. Run the net on batches of 8 tiles. Merge the heatmaps into one page-sized
   map by taking the **maximum** where tiles overlap. The offset at each
   cell comes from the tile that gave the maximum. A point on a seam
   therefore gives one peak in one place, taken from the tile where the
   point has the most context.
4. Pick peaks: keep cells that are a 3 x 3 local maximum and at or above the
   threshold. Decode them to canonical px and clip them to `[0, W] x [0, H]`.
   Then run a greedy NMS: a kept point suppresses any lower-scored point
   within **10 px**. Stop at `max_points`.

Scores are the model's sigmoid probability and are not calibrated against
other models (P6).

## CPU timings

These were measured on the 4-vCPU Linux container used to build this, with
torch 2.14.0+cpu and the default of 4 threads.

| What | Time |
|---|---|
| `detect_points` on a 3400 x 2200 page (54 tiles) | 0.9 to 1.5 s (the limit is 30 s) |
| Training step | about 45 ms per tile, including augmentation |
| Default training run (16 x 2048 tiles) | about 25 min, plus about 1.5 s per val and test page |
| A 6 x 512-tile run on 30 synthetic pages (1400 x 1100, 10 points each) | 139 s. Val and test F1 were 1.00 at 12 px, with a mean localisation error under 1 px. This is synthetic, not real-drawing accuracy. |
| Detector tests | about 6 s |

A slower laptop should budget roughly twice the training time. That is
still under the one-hour limit in P1 for the defaults. The run time depends
on the number of tiles, not on the number of pages, so use `--epochs` and
`--tiles-per-epoch` to trade time for accuracy.

## Known limits

* **Scale.** The model is trained on the canonical 200 DPI raster with scale
  jitter of 0.9 to 1.1 only. Symbols drawn noticeably larger or smaller than
  the training ones (for example, a sheet plotted at a different drawing
  scale) are out of range. On synthetic pages, scale 0.8 to 1.2 still found
  every point, but at 1.1 and 1.2 it added a few extra peaks at a threshold
  of 0.3.
* **Rotation.** Only the four quarter turns and flips are augmented.
  Symbols at other angles (for example, 45° on a skewed wall) are not
  trained for.
* **Density.** NMS removes any second point within 10 px of a stronger one.
  Two receptacles closer than 10 px (about 1.3 mm at 200 DPI) come out as
  one. The sigma-4 target also blurs pairs closer than about 16 px.
* **Symbol classes.** The model has a single output class, "receptacle
  point". It learns whatever the reviews call a receptacle. It does not
  separate symbol types.
* **Synthetic training.** A model trained only on synthetic pages (P4) is
  `synthetic_only`. Its scores are not real-drawing accuracy, and P9 does
  not allow promoting it.
* **Memory.** Training keeps one chunk of 16 decoded pages in memory, which
  is about 120 MB for 3400 x 2200 pages. Inference holds one padded page
  and its stride-4 maps.

## Placeholders for other chats

Chats A and B had not landed on `phase-2` when this was written, so this
branch contains minimal versions of files they own. The coordinator
reconciles them:

* `pinny/models/artifact.py` (B) provides exactly the P5 behaviour:
  `save_model(models_dir, kind=, arch=, state_dict=, input=, dataset_id=, synthetic_only=, train_config=, operating_point=, metrics=)`,
  `read_metadata(model_dir)` and `load_model(model_dir, kind=None) -> (metadata, state_dict)`.
  It checks the sha256 before calling `torch.load(weights_only=True)` and
  refuses to overwrite an existing model id.
* `pinny/training/__main__.py` (A) is a dispatcher with a `COMMANDS` table.
  The detector adds one entry:
  `"train-detector": "pinny.training.detector_train:main"`.
* `pinny/training/__init__.py`, `pinny/models/__init__.py`,
  `tests/training/__init__.py` and `tests/models/__init__.py` are empty
  package markers.
* `tests/training/test_detector_fixtures.py` is a self-contained writer for
  tiny synthetic detector datasets, standing in for A's
  `pinny.training.synthetic`.
