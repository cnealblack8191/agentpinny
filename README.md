# Pinny

Local prototype for detecting receptacle pins on electrical drawing PDFs.
Branch `training-site` combines `phase-2` (learning models, benchmark and
model scan modes in the viewer) with `claude/intelligent-wozniak-3kf3wk`
(detection v1.1, the vector matcher, store schema 3 and the learning loop,
evaluator corpus scoring and baseline gate). It is the base for the
internet-facing training site.

* Contracts: `docs/contracts.md`
* Training site plan (security baseline, architecture, steps): `docs/training-site-plan.md`
* Viewer: `docs/viewer.md`
* Phase 2 (learning models): `docs/phase2-contracts.md`, `docs/phase2-ownership.md`
* Detection: `docs/detection.md`, vector matcher `docs/vector-matching.md`
* Learning store and loop: `docs/learning-store.md`, `docs/learning-loop.md`
* Evaluation: `evaluation/README.md`
* Ownership: `docs/agent-ownership.md`
* Render service (foundation): `docs/render-service.md`

## Install and run

Python 3.12 or 3.13. `pyproject.toml` declares every dependency;
`requirements.lock.txt` pins the base install plus the `dev` extra with
hashes.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements.lock.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m pinny.viewer      # then open http://127.0.0.1:8765/
```

Optional extras, not in the lock: `train` (torch and torchvision, for the
Phase 2 models) and `onnx` (onnxruntime, for the ONNX embedding verifier),
for example `pip install -e ".[train]"`.

After changing dependencies in `pyproject.toml`, regenerate the lock:

```sh
uv pip compile --universal --python-version 3.12 --generate-hashes pyproject.toml --extra dev -o requirements.lock.txt
```

## Tests

```sh
python -m pytest tests evaluation/tests               # every Python suite
PYTHONPATH=evaluation python -m pinny_eval score-corpus \
  --manifest evaluation/fixtures/corpus_mini/manifest.json \
  --baseline evaluation/fixtures/corpus_mini/baseline.json --max-drop 0.01   # evaluator gate
node --test tests/viewer/test_transform.mjs tests/viewer/test_edits.mjs      # viewer JS units
node tests/viewer/test_modes_browser.mjs                                     # Chromium, fake models
```

Tests that need `torch` or `onnxruntime` skip when those extras are not
installed. `tests/viewer/browser_e2e.mjs` still targets the removed stub
render service and does not run (see `docs/phase2-integration.md`).

To regenerate the regression gate's baseline or the evaluator samples, see
`evaluation/README.md`.
