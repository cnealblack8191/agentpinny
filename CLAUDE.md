# Pinny

Receptacle-symbol detection on electrical drawing PDFs: vector-first matcher,
OpenCV raster fallback, learned verifier, human review loop, independent
evaluator. Python 3.12/3.13. Start with `README.md`, then `docs/contracts.md`.

## Setup and tests

```sh
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python --require-hashes -r requirements.lock.txt
uv pip install -p .venv/bin/python -e . --no-deps
.venv/bin/python -m pytest tests evaluation/tests            # every Python suite (~40 s)
PYTHONPATH=evaluation .venv/bin/python -m pinny_eval score-corpus \
  --manifest evaluation/fixtures/corpus_mini/manifest.json \
  --baseline evaluation/fixtures/corpus_mini/baseline.json --max-drop 0.01   # regression gate
node --test tests/viewer/test_transform.mjs tests/viewer/test_edits.mjs tests/viewer/test_batch.mjs
.venv/bin/python -m pinny.viewer                               # http://127.0.0.1:8765/
```

Tests needing `torch`/`onnxruntime` skip unless `pip install -e ".[train]"` / `.[onnx]`.
CI (`.github/workflows/tests.yml`) runs pytest plus the regression gate on every push.

## Rules

- **Ownership.** One session owns one module; edit only the paths listed for it in
  `docs/agent-ownership.md` (Phase 2: `docs/phase2-ownership.md`). Ask the coordinator
  to change anything else.
- **Contracts.** `docs/contracts.md` is the source of truth for everything crossing a
  module boundary (ids, 200 DPI canonical raster, scan result, pin states, errors).
  Change it only through a coordinator-approved PR. Same for `docs/phase2-contracts.md`.
- **Dependencies.** Only the foundation/coordinator edits `pyproject.toml`. After any
  change regenerate the lock: `uv pip compile --universal --python-version 3.12
  --generate-hashes pyproject.toml --extra dev -o requirements.lock.txt`.
- **Licensing.** Use pypdfium2/pikepdf/OpenCV. Do not add PyMuPDF or Ultralytics
  (AGPL) without a purchased licence.
- **Never commit** drawings, PDFs, SQLite databases, crops, exports or model
  artifacts. Data lives under `$PINNY_DATA_DIR`.
- **Evaluation honesty.** `evaluation/` never imports `pinny`. Never score a corrected
  pin set as detector output. A result not run against a verified reference is
  reported as "Not measured", never as a number.
- **LLM calls** are opt-in, keyed, crops-only (legend reading, ambiguous-crop
  adjudication); never for full-sheet detection. Nothing in the repo calls one yet.
- Integration branch: `training-site`. Feature branches start there and the
  coordinator merges back. Status of what is measured: `evaluation/INTEGRATION_CHECKS.md`.
