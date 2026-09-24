# Phase 2 training datasets (`pinny.training.dataset`, `pinny.training.synthetic`)

This module builds the `pinny.dataset` v1 datasets defined in
`docs/phase2-contracts.md` (P3 labels, P4 format). It needs only the
standard library, numpy and opencv. It never imports torch.

## CLI

```
python -m pinny.training build-dataset [--data-dir DIR] [--export FILE] [--document-version sha256:...]
                                       [--out-dir DIR] [--created-at RFC3339]
python -m pinny.training synthesize    [--data-dir DIR] [--out-dir DIR] [--documents 6] [--pages-per-document 2]
                                       [--seed 0] [--page-size 612x792] [--noise-sigma 6] [--created-at RFC3339]
python -m pinny.training dataset-info  <dataset dir | manifest.json | dataset id> [--data-dir DIR] [--no-verify]
```

* `--data-dir` defaults to `$PINNY_DATA_DIR`, then `~/.local/share/pinny`,
  which is the same directory the viewer uses. Datasets go to
  `<data-dir>/datasets/<dataset_id>/` unless you pass `--out-dir`.
* `build-dataset` exports the learning store (with unlabeled pins) and
  renders pages with the real `RenderService` on the same data dir.
  `--export` uses an existing export file instead.
* `dataset-info` prints the split, counts and documents per split. It also
  runs `verify_dataset` and exits with 1 if it finds a problem.
* Every command prints JSON. A `PinnyError` exits with 1 and prints
  `error [code]: message` on stderr.

### Command table (for chats B and C)

The dispatcher lives in `pinny/training/__main__.py`. To add a command, add
one line to `COMMANDS`:

```python
"train-verifier": ("pinny.training.verifier_train:main", "train the verifier"),
```

The function receives the remaining `argv` (a list of strings) and returns
an exit code. Modules are imported only when their command runs, so the
dataset commands never load torch.

## Python API

```python
from pinny.training.dataset import build_dataset, load_manifest, verify_dataset, split_for, crop_centered
res = build_dataset(store.export(), render_service, data_dir / "datasets")
res.dataset_id, res.path, res.manifest, res.reused

from pinny.training.synthetic import synthesize_dataset, SynthParams
res = synthesize_dataset(data_dir / "datasets", documents=20, pages_per_document=2, seed=0)
```

`DatasetWriter` is the shared writer that both builders use. Use it if you
need to write a dataset in the same format from another source.

## How labels become samples (P3)

| Pin in the export | Verifier | Detector page |
|---|---|---|
| machine, approved (`positive`) | `label: 1` | a point |
| machine, rejected (`negative`) | `label: 0` | background |
| manual, added (`positive`) | `label: 1` | a point |
| manual, removed (`manual_removed`) | not used | not used; the page stays complete |
| unreviewed (`unlabeled`) | not used | the page is excluded when this is on its latest scan |

* **Verifier:** every labelled pin of every scan is a sample. Rescans of
  the same page each give their own samples. They share a document, so
  they are always in the same split.
* **Detector:** there is one entry per `canonical_page_id`. It comes from
  the page's **latest** scan, in the export's record order
  (`recorded_at`, then insertion). A page is included only when that scan
  has zero `unlabeled` pins. Its points are the latest scan's approved and
  added pins. Older scans never add points.
* An export made with `--labeled-only` (`include_unlabeled: false`) is
  refused with `export_missing_unlabeled`, because completeness cannot be
  checked without the unlabeled pins.

## Format details beyond P4

* **Split:** `u = int(sha256(document_id)[:8 bytes]) / 2**64`, compared with
  the cumulative fractions in the order train, val, test. With a seed other
  than 0, the hash input is `f"{seed}:{document_id}"`. `split_for()` is the
  one implementation, so B, C and E can reuse it.
* **Crop window:** `x0 = floor(x - 48 + 0.5)`, `x1 = x0 + 96`, and the same
  for y. This is the whole-pixel window whose centre is nearest the pin,
  and it is the learning store's manual-crop rule at 96 px. Pixels outside
  the page are 255. The grayscale conversion is `cv2.COLOR_RGB2GRAY`.
* **Ids:** `sample_id = sha256(source_example_id)[:24]` and
  `page_key = sha256(canonical_page_id)[:24]`.
* **Extra fields:** each verifier and detector entry has a `sha256` of its
  PNG file, so the `dataset_id` also covers the pixels. Real detector
  entries also have `source_scan_id`. `source.synthetic` is always
  present.
* **`export_sha256`** is the sha256 of the export's canonical JSON *without*
  `exported_at`. Two exports of an unchanged store therefore hash the same.
* **`created_at`** is part of the hashed body (P4), so it must be
  deterministic. It defaults to the newest `recorded_at` or event
  `created_at` in the export. For synthetic data it defaults to
  `$SOURCE_DATE_EPOCH`, or `1970-01-01T00:00:00Z` if that is unset.
  `--created-at` overrides it, which changes the id.
* **Canonical JSON** (used for hashing) is
  `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
  `manifest.json` is written sorted with `indent=2` and a trailing newline.
  Entries are sorted by `sample_id` and `page_key`.
* **Writes are atomic:** files are staged in `datasets/.staging-<uuid>` and
  renamed to `<dataset_id>`. Rebuilding into a root that already holds the
  same dataset reuses it (`reused: true`). A different directory with the
  same name is a `dataset_conflict`.

## Synthetic data

Each synthetic document is a small uncompressed PDF, written by
`synthetic.build_pdf` with the standard library only. It is ingested and
rendered by the real `RenderService`, by default in a temporary data dir,
so it never appears in the viewer's document list. Pass `render=` to keep
the PDFs. Each page has:

* receptacles: the `tests/factory.py` glyph (`receptacle_ops`, copied and
  checked by a test), at a random quarter rotation (0, 90, 180 or 270) and
  a scale in 0.9–1.1. Their centres are the ground-truth points;
* distractors: circles without bars, filled and outlined squares, text
  (Helvetica, some vertical) and 2–5 pt wall lines. They are kept clear of
  the receptacles so the labels are exact;
* noise: gaussian noise (σ = 6 gray levels) and dark speckles, added to
  the grayscale raster with a seeded numpy generator.

Verifier negatives are the circle, square and text distractors, plus
random background spots far from any receptacle. Document ids are uuid5
values chosen so that the hash split fills train, val and test in
proportion (for example 5/1/1 for 7 documents). The split rule itself is
unchanged. `source.synthetic` is `true`, and `source.generator` records the
seed and every parameter. Scores on synthetic data are never real-drawing
accuracy.

The default run is 6 documents × 2 letter pages. On a CPU, 20 documents ×
2 pages (40 pages, about 330 points) take about 8 s.

## Tests

```
python -m pytest tests/training -q
```

They cover the split (no document leakage, all versions and pages together),
the completeness rule, removed manual pins, edge padding, determinism
(byte-identical rebuilds, the same id across re-exports), manifest counts
against the files, tamper detection, synthetic ground truth, and the CLI.
One CLI test checks that torch is never imported. The suite takes about 2 s.
