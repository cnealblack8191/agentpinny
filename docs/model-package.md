# Model packages (`pinny/model/`)

A model package is one `.pinny` file holding everything needed to find one
symbol type on drawing pages. The training website builds packages from
reviewed examples. The QC app loads a package and runs it offline, with no
training code, review data or network access.

```
training website                                    QC app
────────────────                                    ──────
review pins ─► learning store ─► train_model() ─► receptacles-3.pinny ─► ModelPackage.load()
                                        │                                     │
                    evaluator (held-out docs) ─► evaluation{...}              └─► model.detect(page)
                                        │
                              promotion_check(new, current)
```

## Using it

```python
from pinny.model import ModelPackage, train_model, promotion_check

# Website: train from reviewed examples (seconds, CPU only).
trained = train_model(
    name="receptacles", version="3", symbol_class="duplex",
    original_template=template_pixels,        # the user's drawn exemplar
    positive_crops=approved_crops,            # contracts §6 crops (box + 24 px)
    negative_crops=rejected_crops,
    review_pairs=store.review_stats(...),     # (raw score, approved?)
    evaluation=held_out_metrics,              # from pinny_eval score-corpus
)
print(trained.notes)                          # what was and wasn't built
if promotion_check(trained.package, current_model).ok:
    trained.package.save("receptacles-3.pinny", signing_key=key)

# QC app: load and run.
model = ModelPackage.load("receptacles-3.pinny", verify_key=key)
result = model.detect(page)                   # canonical RGB raster, 200 DPI
for d in result.accepted:
    d.candidate.box, d.candidate.score, d.verifier_p, d.probability
result.to_dict(accepted_only=True)            # contracts §4 detection records
```

Command line:

```
python -m pinny.model inspect  receptacles-3.pinny [--key-env PINNY_MODEL_KEY]
python -m pinny.model verify   receptacles-3.pinny  --key-env PINNY_MODEL_KEY
python -m pinny.model promote-check new.pinny [current.pinny] [--max-drop 0.01]
```

Exit codes: 0 ok, 2 invalid package or failed check, 64 usage error. Keys are
read from an environment variable, never from the command line.

## What a package contains

| Component | Required | What it does |
|---|---|---|
| Template bank | yes | Up to `max_templates` medoids of approved symbols, always including the original exemplar. All are matched with `detect_multi`. |
| Negative bank | no | Representative rejected crops. A candidate that resembles one more than its best template is vetoed. |
| Verifier | no | kNN over HOG + intensity features of §6 crops (`knn-hog`), or over embeddings from a bundled ONNX model (`onnx-embedding`, needs `onnxruntime`). Gives `verifier_p`. |
| Calibration | no | Isotonic or Platt map from raw score to probability. Gives `probability`. |
| Decision | yes | `score`, `verifier` or `calibrated`, plus a threshold. Sets `accepted`. |
| Scan settings | yes | The `ScanSettings` the model was tuned with. `num_threads` and `search_region` describe the machine, so they aren't saved; pass them to `detect()`. |

`train_model` picks the decision rule automatically:

* It uses the verifier (`p >= 0.5`) once there are at least 3 approved and 3
  rejected crops.
* Otherwise it uses the raw score against the threshold suggested from
  review history (target precision 0.95, at least 30 labels).
* Otherwise it uses the scan threshold.

The raw `score` is never modified (contracts §4).

## File format (`pinny.model` v1)

A ZIP archive:

```
manifest.json            identity, input requirements, settings, components, provenance, evaluation
templates/000.png ...    template bank (8-bit PNG, native size, grayscale or RGB)
negatives/000.png ...    negative bank (optional)
verifier/features.npy    verifier examples, float32 (N, D) (optional)
verifier/labels.npy      1 = approved, 0 = rejected, int8 (N,)
verifier/embedder.onnx   ONNX embedding model (onnx-embedding only)
signature.json           HMAC-SHA256 over manifest.json (optional)
```

Key manifest fields:

* `format: "pinny.model"`, `format_version: 1`, `contracts_version: "1.1"`.
* `model_id` (uuid4), `name`, `version`, `symbol_class`, `created_at`.
* `input`: `{space: canonical_raster_px, dpi: 200, pixel_format: "RGB uint8",
  renderer_version}`. Pages must be rendered the way the model was trained
  (contracts §2). A different renderer can shift scores.
* `scan_settings`, `templates[]`, `negatives`, `verifier`, `calibration`,
  `decision`.
* `provenance`: free-form. Record the dataset export manifest digest, store
  sequence number and label counts.
* `evaluation`: held-out results, e.g.
  `{corpus_sha256, ap, f1, recall, precision}`. `promotion_check` requires
  `corpus_sha256`, `ap`, `f1` and `recall`.
* `requires`: Python packages the QC app needs (`numpy`,
  `opencv-python-headless`, plus `onnxruntime` for ONNX verifiers).
* `files`: sha256 of every other entry.

A reader must refuse a `format_version` newer than it knows. The v1 loader
also refuses unknown component types and unknown scan settings instead of
silently ignoring them.

## Integrity, signing and safety

* Every file's sha256 is checked against `manifest.files`. Missing, extra or
  altered entries are refused.
* **Signing (optional):** `save(..., signing_key=key)` adds an HMAC-SHA256
  over `manifest.json`. That covers every file through the hash table.
  `load(..., verify_key=key)` then refuses unsigned, altered or
  differently-keyed packages.
  * HMAC is symmetric: whoever holds the key can also sign. Keep the key on
    the training server, and give the QC app its own copy only if it runs in
    a trusted environment.
  * If the QC app is distributed widely, switch to an asymmetric signature
    (Ed25519) in a future format version.
* **Loader safety:**
  * Entry names come from an allow-list, so there are no paths, no `..` and
    no code.
  * Sizes are capped before decompression.
  * Arrays load with `allow_pickle=False`.
  * Nothing in a package is executed. An ONNX model is run by onnxruntime's
    graph interpreter.
* **Deterministic:** the same model gives byte-identical files, so the
  package digest identifies it.
* **Confidentiality:** templates and negatives are small crops of real
  drawings, and verifier features are derived from crops. Treat a package
  as containing drawing content.

## Promotion

`promotion_check(candidate, current, metrics=("ap", "f1", "recall"),
max_drop=0.01)` allows a candidate only if all of these hold:

* It has a held-out evaluation.
* Its symbol class matches the current model's.
* Both models were scored on the same corpus (`corpus_sha256`).
* No tracked metric dropped by more than `max_drop`.

With no current model, a candidate with a valid evaluation passes.

## Later: trained detectors

A fine-tuned detector (for example RF-DETR exported to ONNX) needs a new
component type, `detector`, and a `format_version` bump. Older readers will
then refuse those packages with `unsupported_version` instead of
misreading them. The template, verifier and decision layers stay the same.
