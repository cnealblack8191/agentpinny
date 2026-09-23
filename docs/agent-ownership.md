# Agent ownership

Each module has one owning session and one branch. A session edits only
the paths it owns. To change another module, message the coordinator or
the owner. `docs/contracts.md` is shared and changes only through a
coordinator-approved PR.

| Area | Paths | Session / branch | Status (2026-09-23) |
|---|---|---|---|
| Coordination, contracts, integration | `docs/contracts.md`, `docs/agent-ownership.md`, merges | Chat response coordination / `claude/beautiful-johnson-qylpd0` | contracts v1 published |
| Foundation: packaging, deps, errors, PDF upload/versioning, canonical render, crop renderer, test factory | `pyproject.toml`, `pinny/__init__.py`, `pinny/errors.py`, `pinny/pdf/` or `pinny/render/`, `tests/conftest.py`, `tests/factory*` | Pinny Phase 1 foundation / `claude/fervent-noether-8022eq` | in progress |
| Detection | `pinny/detection/`, `tests/detection/`, `docs/detection.md` | Pinny detection module / `claude/epic-einstein-qtx4ui` | aligned to contracts v1 (55 tests) |
| Learning store | `pinny/learning/` (was `pinny_learning/`), `tests/learning/`, `docs/learning-store.md` | Pinny local persistence / `claude/great-cerf-hbzlrf` | built; aligning to contracts |
| Viewer and pin interface | `pinny/viewer/`, `web/` (if a front end), `tests/viewer/`, `docs/viewer.md` | Pinny viewer and pin interface / `claude/youthful-maxwell-gqfdiu` | unblocked by this doc |
| Evaluation | `evaluation/` | Pinny evaluation CLI / `claude/blissful-clarke-l1k2ns` | done; `adapt-scan` adapter added |

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
