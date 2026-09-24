# Pinny Phase 2 contracts: the learning model (v1)

Phase 2 adds learned models trained from the reviews that Phase 1 captures.
This file is the single source of truth for everything that crosses a
Phase 2 module boundary. Every rule in `docs/contracts.md` (Phase 1) still
applies: canonical 200 DPI raster px, XYWH boxes, `PinnyError`, and one
`pinny/` package. Change this file only through the coordinator.

Base branch: **`phase-2`**. Every Phase 2 chat starts from it and the
coordinator merges back into it.

## P1. Goal and the two models

| Model | `kind` | What it does | Why |
|---|---|---|---|
| **Verifier** | `verifier` | Rescores each template-match candidate. Output is `p(receptacle)` in [0, 1]. | It works with little data and cuts false positives straight away. |
| **Point detector** | `detector` | Finds receptacle *points* on a page with no template, using a heatmap and peak picking. | It finds symbol variants the template misses and removes the template-drag step. |

Both models are small convolutional nets in **PyTorch + torchvision**. They
must train on a CPU (a Windows laptop) in minutes for the verifier, and
under an hour for the detector on a few hundred pages. CUDA is optional
and never required.

**Not allowed:** Ultralytics/YOLO (AGPL license), any cloud training or
inference, and any download of pretrained weights at run time. If you use
ImageNet weights, record their URL and sha256 and make them an explicit,
optional flag. Default to training from scratch.

## P2. Dependencies

These pins are the `train` extra in `pyproject.toml`, owned by the
coordinator:

```
torch==2.14.0
torchvision==0.29.0
```

The base install (Phase 1) must keep working **without** torch. Import
torch only inside `pinny/training/` and `pinny/models/`, and only lazily
from the viewer. For CPU-only wheels, use
`pip install torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cpu`.
If that index is unreachable, use plain PyPI.

## P3. Labels: what counts as ground truth

The source is `LearningStore.export()` (`pinny.learning.export` v1). Labels
come from `interpret_pin`:

| Pin | Label | Verifier | Point detector |
|---|---|---|---|
| machine, approved | positive | positive sample | a point target |
| machine, rejected | negative | negative sample | background |
| manual, added | positive | positive sample (a crop centred on the pin) | a point target |
| manual, removed | none | not used | not used |
| unreviewed | none | not used | makes the page **ineligible** |

**Page completeness rule (detector only).** A page is used for detector
training only when its **latest scan has zero unreviewed pins**. Every
receptacle on a complete page is then either approved or added. Anything
else on the page is background. A page with unreviewed pins would teach
the model that real receptacles are background, so it is excluded. The
verifier has no such rule, because it uses labelled pins only.

Detections from any model are never labels. Only human review events are.

## P4. Dataset (`pinny.dataset` v1)

```
$PINNY_DATA_DIR/datasets/<dataset_id>/
  manifest.json          # the index below
  verifier/<split>/<sample_id>.png   # 96x96 grayscale crops
  detector/<split>/<page_key>.png    # canonical page raster (grayscale), or tile refs
```

`manifest.json`:

```json
{
  "format": "pinny.dataset", "format_version": 1,
  "dataset_id": "sha256 of the canonical JSON of everything below except dataset_id",
  "created_at": "RFC3339", "source": {"export_sha256": "...", "store_schema_version": 1},
  "split": {"method": "document_id_hash", "seed": 0, "fractions": {"train": 0.7, "val": 0.15, "test": 0.15}},
  "verifier": [ {"sample_id": "...", "split": "train", "path": "verifier/train/x.png",
                 "label": 1, "canonical_page_id": "...", "document_id": "...",
                 "x": 0.0, "y": 0.0, "source_example_id": "scan/pin"} ],
  "detector": [ {"page_key": "...", "split": "train", "path": "detector/train/x.png",
                 "canonical_page_id": "...", "document_id": "...", "width": 3400, "height": 2200,
                 "points": [{"x": 0.0, "y": 0.0}]} ],
  "counts": {"verifier": {"train": {"pos": 0, "neg": 0}}, "detector": {"train": {"pages": 0, "points": 0}}}
}
```

* **Split by `document_id`, never by page or pin.** The split is
  `sha256(document_id) → [0, 1)`, compared with the cumulative fractions.
  All versions and pages of one drawing set land in one split. Otherwise
  the test score is inflated by near-duplicate pages.
* **Verifier crop:** 96 × 96 px, grayscale `uint8`, centred on the pin
  `(x, y)`. It is cut from the canonical raster, and the out-of-page area
  is padded with white (255). At 200 DPI, 96 px is about 12 mm, which is
  larger than a receptacle symbol and includes some context.
* **Detector input:** the whole canonical page in grayscale. Tiling
  happens in the training and inference code: 512 × 512 tiles with a
  stride of 384. The dataset stores whole pages and points only.
* The build is **deterministic**: the same export and the same rasters
  give the same `dataset_id` and byte-identical files.
* **Synthetic data:** `pinny.training.synthetic` generates labelled
  synthetic pages. It uses the receptacle glyphs from `tests/factory.py`
  with distractor symbols, wall lines, text, rotation and noise. Those
  pages feed tests and bootstrap training before real reviews exist. A
  synthetic dataset is marked `"synthetic": true` in `source`. Its scores
  are never reported as real-drawing accuracy.

## P5. Model artifact (`pinny.model` v1)

```
$PINNY_DATA_DIR/models/<model_id>/
  model.json     # metadata below
  weights.pt     # state_dict only
```

```json
{
  "format": "pinny.model", "format_version": 1,
  "model_id": "<kind>-<UTC yyyymmddThhmmssZ>-<first 8 hex of weights sha256>",
  "kind": "verifier | detector", "arch": "e.g. verifier-cnn-v1",
  "input": {"channels": 1, "crop_px": 96, "tile_px": 512, "stride_px": 384, "dpi": 200},
  "dataset_id": "...", "synthetic_only": false,
  "train_config": {"epochs": 0, "lr": 0.0, "batch_size": 0, "seed": 0, "augment": {}},
  "weights_sha256": "...", "code_version": "git:<sha>",
  "operating_point": {"threshold": 0.5, "chosen_on": "val"},
  "metrics": {"val": {}, "test": {}},
  "created_at": "RFC3339"
}
```

* Load weights **only** with `torch.load(path, weights_only=True)`, after
  checking `weights_sha256`. Never unpickle arbitrary objects.
* An artifact is immutable. Retraining writes a new `model_id`.
* The **threshold is chosen on `val`**. `test` is scored once, at that
  threshold, and never used for tuning.

## P6. Inference interfaces

```python
# pinny/models/verifier.py
class Verifier:
    @classmethod
    def load(cls, model_dir) -> "Verifier": ...
    model_id: str; threshold: float
    def score(self, page_rgb: np.ndarray, points: Sequence[tuple[float, float]]) -> list[float]: ...

# pinny/models/point_detector.py
class PointDetector:
    @classmethod
    def load(cls, model_dir) -> "PointDetector": ...
    model_id: str; threshold: float
    def detect_points(self, page_rgb: np.ndarray, *, threshold: float | None = None,
                      max_points: int = 500) -> list[dict]:   # [{"x", "y", "score"}], canonical px
        ...
```

* `page_rgb` is the canonical raster from `RenderService.render_page`
  (uint8 RGB, (H, W, 3)). Models convert it to grayscale themselves.
* Scores are the model's probability. Like the template score, they are
  **not calibrated** across models.
* Both models must process a 3400 × 2200 page on a CPU in under 30 s,
  and the verifier must score 500 points in under 5 s.

## P7. Scan modes (contracts §4 extension)

A scan result gains `"mode"`:

| `mode` | Pipeline | `detector.name` | Per-detection extras |
|---|---|---|---|
| `template` | Phase 1, unchanged | `opencv-template` | — |
| `template+verifier` | template candidates, then verifier rescoring, then candidates below the verifier threshold are dropped | `opencv-template+verifier` | `template_score`, `verifier_score`; `score` = `verifier_score` |
| `model` | the point detector only; no template is needed | `pinny-point-detector` | `score` = model score; `box` = a 40 × 40 px box centred on the point; `rotation` = 0 |

`detector.version` is the `model_id` (or both ids joined with `+`). The
original detector output stays immutable, and reviews work exactly as in
Phase 1. Dropped candidates are **recorded** in the scan result under
`"suppressed"` with their scores. That lets evaluation measure what the
verifier removed.

## P8. Model registry and promotion

`pinny.models.registry`:

* `list_models(kind=None)` and `get(model_id)` return artifact metadata.
* `active(kind)` returns the promoted `model_id` or `None`. It is stored
  in `$PINNY_DATA_DIR/models/active.json`.
* `promote(model_id, evidence_path)` is refused unless the evidence file
  is a **promotion report** (P9) for that model that says `"promote": true`.

## P9. Evaluation and the promotion gate

* The benchmark runs each mode on the **test split** pages of a dataset
  (the complete pages only, P3). It writes one `pinny.detections` v1 file
  per page and mode, and scores each file with the Phase 1 evaluator.
  The evaluator stays stdlib-only and does not import `pinny`.
* Matching tolerance is **12 px** (half a symbol at 200 DPI), fixed in
  advance.
* A **promotion report** (`pinny.promotion` v1) compares the candidate mode
  with the `template` baseline on the same pages. Scores are summed over
  pages as TP/FP/FN, then divided. It recommends promotion only if:
  - recall is at least the baseline's and precision is at least the
    baseline's minus 0.02, **and** at least one of them improves by
    0.02 or more;
  - the test split has ≥ 3 documents and ≥ 100 reference points;
  - the dataset is not `synthetic_only`.
* Every number carries its dataset id, split, model id and tolerance.
  Synthetic results are labelled "synthetic — not real-drawing accuracy".

## P10. Package layout (Phase 2 additions)

```
pinny/training/        dataset builder, synthetic generator, trainers, CLI (python -m pinny.training)
  dataset.py           Dataset chat
  synthetic.py         Dataset chat
  verifier_train.py    Verifier chat
  detector_train.py    Detector chat
pinny/models/          inference-time code, loaded by the app
  verifier.py          Verifier chat
  point_detector.py    Detector chat
  registry.py          Integration chat
pinny/benchmark/       Evaluation chat (imports pinny; calls the stdlib evaluator)
tests/training/, tests/models/, tests/benchmark/
```

Model and dataset files are never committed. Tests build tiny synthetic
datasets in `tmp_path` and train for a few steps. They must run in under
60 s on a CPU, and they are skipped cleanly when torch is not installed
(`pytest.importorskip("torch")`).
