# Agent ownership

Each module has one owning session and one branch. A session edits only
the paths it owns. To change another module, message the coordinator or
the owner. `docs/contracts.md` is shared and changes only through a
coordinator-approved PR.

| Area | Paths | Session / branch | Status (2026-09-23) |
|---|---|---|---|
| Coordination, contracts, integration | `docs/contracts.md`, `docs/agent-ownership.md`, merges | Chat response coordination / `claude/beautiful-johnson-qylpd0` | phase 1 integrated; `training-site` merge on 2026-09-25 (see below) |
| Foundation: packaging, deps, errors, PDF upload/versioning, canonical render, crop renderer, test factory | `pyproject.toml`, `pinny/__init__.py`, `pinny/errors.py`, `pinny/pdf/` or `pinny/render/`, `tests/conftest.py`, `tests/factory*` | Pinny Phase 1 foundation / `claude/fervent-noether-8022eq` | done; merged |
| Detection | `pinny/detection/`, `tests/detection/`, `docs/detection.md` | Pinny detection module / `claude/epic-einstein-qtx4ui` | done; merged |
| Learning store | `pinny/learning/` (was `pinny_learning/`), `tests/learning/`, `docs/learning-store.md` | Pinny local persistence / `claude/great-cerf-hbzlrf` | done; merged |
| Viewer and pin interface | `pinny/viewer/`, `web/` (if a front end), `tests/viewer/`, `docs/viewer.md` | Pinny viewer and pin interface / `claude/youthful-maxwell-gqfdiu` | done; on the real render service |
| Evaluation | `evaluation/` | Pinny evaluation CLI / `claude/blissful-clarke-l1k2ns` | done; merged |
| Vector matcher; detection v1.1, learning loop, store schema 3, evaluator corpus/gate | `pinny/vector/`, `tests/vector/`, `docs/vector-matching.md`, `docs/learning-loop.md` (plus v1.1 changes in the areas above) | Pinny improvement / `claude/intelligent-wozniak-3kf3wk` | done; merged into `training-site` |

Phase 2 (learning models) areas are listed in `docs/phase2-ownership.md`.

## Training site

From `docs/training-site-plan.md` section 5. `training-site` combines
`phase-2` and `claude/intelligent-wozniak-3kf3wk` (merged 2026-09-25:
587 pytest tests pass with the `train` and `onnx` extras, 566 on the base
install; mini-corpus gate passes). Every branch below starts from `training-site`, and the
coordinator merges them back in plan order (steps 6a and 8).

| Area | Paths | Branch | Status (2026-09-25) |
|---|---|---|---|
| Training-site contract | `docs/training-site.md`, `docs/training-site-plan.md` | `training-site` (coordinator) | plan copied; contract is Step 2 |
| Secure web tier | `pinny/viewer/`, `web/` (viewer pages), `tests/viewer/` | `training-site-web` | not started (Step 3) |
| Jobs and sandbox | `pinny/jobs/`, `tests/jobs/` | `training-site-jobs` | not started (Step 4) |
| Deployment | `deploy/`, `docs/deployment.md`, CI image builds | `training-site-deploy` | not started (Step 5) |
| Training API | training routes in `pinny/viewer/` or `pinny/site/` | `training-site-train-api` | not started (Step 7a) |
| Training pages | `web/` training pages, Playwright tests | `training-site-train-ui` | not started (Step 7b) |

While these branches are open, the secure web tier owns `pinny/viewer/`,
`web/` and `tests/viewer/` except the training routes and training pages,
which belong to the training API and training pages branches.

## Integration order

1. Foundation pushes `pyproject.toml`, `pinny/errors.py` and the render
   service stub (sections 7–9 of the contracts).
2. Detection and the learning store rebase on that, adopt `PinnyError`,
   and move into `pinny/`.
3. The viewer builds against the render service and the learning-store
   actions (contracts section 5).
4. The evaluator adds the `scan result → pinny.detections` adapter and
   runs the I1–I7 integration checks.
5. The coordinator merges each branch in order and runs every test suite.

The ".gitignore" is shared: take the **union** of every module's entries.
Never commit drawings, databases, crops or exports.
