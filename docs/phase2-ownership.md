# Phase 2 ownership

Base branch: `phase-2`. Each chat works on its own branch, created from
`phase-2`, and edits only the paths it owns. The coordinator merges
every branch back into `phase-2` and runs every test suite.

| Chat | Owns | Depends on |
|---|---|---|
| **Coordinator** | `docs/phase2-*.md`, `pyproject.toml` (`train` extra), merges | — |
| **A. Dataset** | `pinny/training/dataset.py`, `pinny/training/synthetic.py`, `pinny/training/__init__.py`, `pinny/training/__main__.py` (CLI dispatcher), `tests/training/test_dataset*.py`, `tests/training/test_synthetic*.py`, `tests/training/conftest.py`, `docs/phase2-dataset.md` | Phase 1 learning store and render service |
| **B. Verifier** | `pinny/training/verifier_train.py`, `pinny/models/verifier.py`, `pinny/models/__init__.py`, `pinny/models/artifact.py` (P5 save/load helpers, shared), `tests/training/test_verifier*.py`, `tests/models/test_verifier*.py`, `docs/phase2-verifier.md` | P4 manifest format (build against a tiny fixture until A lands) |
| **C. Point detector** | `pinny/training/detector_train.py`, `pinny/models/point_detector.py`, `tests/training/test_detector*.py`, `tests/models/test_point_detector*.py`, `docs/phase2-detector.md` | P4, P5 (`artifact.py` from B; copy its interface if B hasn't landed yet) |
| **D. Integration** | `pinny/models/registry.py`, scan modes in `pinny/viewer/service.py` and `server.py`, the `web/` mode selector, `tests/models/test_registry*.py`, `tests/viewer/test_modes*.py`, `docs/phase2-integration.md` | P6 interfaces (use fakes until B and C land) |
| **E. Evaluation** | `pinny/benchmark/`, `evaluation/` changes (still stdlib-only), `tests/benchmark/`, `docs/phase2-evaluation.md` | P4, P6, P7, P9 |

The CLI dispatcher (`python -m pinny.training <command>`) belongs to A.
B and C register commands by adding their module to the dispatcher's
command table, which is a one-line change, and must say so in their
commit message.

## Combine order

1. A, B and C can merge in any order. Each one's tests must pass alone.
2. D merges after B and C, then swaps its fakes for the real classes.
3. E merges last and runs the benchmark on a synthetic dataset end to end.
4. The coordinator runs the full suite and starts the app. It trains
   both models on synthetic data and runs all three scan modes in the
   browser.
