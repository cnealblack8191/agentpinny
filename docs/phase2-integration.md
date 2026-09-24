# Phase 2 integration: model registry and scan modes

Owner: chat D (docs/phase2-ownership.md). This covers `pinny/models/registry.py`
(P8), the scan modes in `pinny/viewer/service.py` and `server.py` (P7), and
the scan-mode selector in `web/`. Every rule in `docs/contracts.md`,
`docs/phase2-contracts.md` and `docs/viewer.md` still applies.

## Model registry (`pinny.models.registry`, P8)

This module uses only the standard library. It never imports torch, so the
viewer can list and check models on a base install.

```
$PINNY_DATA_DIR/models/
  <model_id>/model.json, weights.pt     P5 artifacts (written by the trainers)
  active.json                           the promoted model per kind
  promotions/<model_id>-<sha8>.json     a copy of each accepted promotion report
```

| Call | Returns / does |
|---|---|
| `list_models(kind=None)` | metadata of every valid `pinny.model` v1 artifact, oldest first. Unreadable directories are skipped. |
| `get(model_id)` | one artifact's `model.json`. `unknown_model` (404) or `invalid_model`. |
| `active(kind)` | the promoted `model_id`, or `None` |
| `promote(model_id, evidence_path)` | makes the model active for its kind and returns the new `active.json` entry |
| `deactivate(kind)` | clears the active model of that kind (an operator escape hatch; it is recorded in the history) |

Each function takes an optional `data_dir=`. It defaults to `$PINNY_DATA_DIR`,
read at call time. `ModelRegistry(data_dir)` is the same API as an object.
The CLI is:

```
python -m pinny.models.registry [--data-dir DIR] list [--kind verifier|detector]
python -m pinny.models.registry active
python -m pinny.models.registry promote <model_id> --evidence report.json
python -m pinny.models.registry deactivate verifier|detector
```

**`promote` is refused** (`PinnyError`, and exit code 2 from the CLI) in
these cases:

| code | when |
|---|---|
| `evidence_missing` | there is no file at `evidence_path` |
| `evidence_invalid` | the file is not JSON, or is not `format: "pinny.promotion"`, `format_version: 1` |
| `evidence_wrong_model` | the report does not name this model. Ids are read from `model_id`/`model_ids`, at the top level or under `candidate`; `+`-joined ids are split. |
| `evidence_wrong_mode` | the report's candidate mode (`candidate.mode`, `candidate_mode` or `mode`) is not the mode for this kind (`verifier` → `template+verifier`, `detector` → `model`). This is checked only when the report states a mode. |
| `promotion_not_recommended` | `promote` is anything other than JSON `true` (so `false`, missing, `"true"` or `1`). The report's `reasons`/`reason` is included in the message. |
| `weights_mismatch` / `invalid_model` | `weights.pt` is missing, or its sha256 differs from `model.json` |
| `unknown_model` | there is no such artifact |

On success, the registry keeps a copy of the report under `promotions/`, so
the evidence survives if the original file is deleted. It then rewrites
`active.json`:

```json
{"format": "pinny.models.active", "format_version": 1,
 "active": {"verifier": {"model_id": "...", "kind": "verifier", "promoted_at": "RFC3339",
                         "evidence_path": "/abs/report.json", "evidence_copy": "promotions/...json",
                         "evidence_sha256": "...", "dataset_id": "..."}},
 "history": [{"action": "promote", "at": "...", "kind": "verifier", "model_id": "...",
              "previous": null, "evidence_sha256": "..."}]}
```

**Atomic write.** The file goes to a temp file in the same directory,
which is fsynced and then `os.replace`d over `active.json`. The directory
is fsynced where the OS allows it. A reader always sees the old file or
the new one, never a partial one. If `active.json` can't be read,
`active()` raises `registry_unreadable` (500). It does not silently treat
the file as "no model".

## Scan modes (P7)

`POST /api/scans` (and `ViewerService.scan`) takes an optional `mode`:

```
{document_version, page_index, request_id,
 mode?: "template" | "template+verifier" | "model",   # default "template"
 template_box?,        # required for the two template modes, refused in "model"
 threshold?,           # template NCC threshold in [-1, 1]; template modes only
 model_threshold?}     # overrides the active model's operating point, in [0, 1]
```

| mode | pipeline | `detector` | detections |
|---|---|---|---|
| `template` | Phase 1, unchanged | `opencv-template`, version `git:<sha>`, settings = `ScanSettings` | as in Phase 1 |
| `template+verifier` | the Phase 1 template scan, then `Verifier.score` on every candidate centre. Candidates with verifier score < threshold are moved to `suppressed`. | `opencv-template+verifier`, version = verifier `model_id`, settings = `ScanSettings` plus `verifier: {model_id, threshold, threshold_source}` and `code_version` | `score` = `verifier_score`, and `template_score` is kept. Ranked by verifier score (ties keep template order). |
| `model` | `PointDetector.detect_points(page, threshold=…, max_points=500)` | `pinny-point-detector`, version = detector `model_id`, settings `{model_id, threshold, threshold_source, max_points, box_px: 40, code_version}` | `score` = model score, `box` = a 40 × 40 integer box centred on the point (within 0.5 px), `rotation` = 0, point = the model's exact `(x, y)` |

* Every scan result, and every `GET /api/scans/{id}` response, carries
  `mode`. `suppressed` is a list. It is empty except in
  `template+verifier`, where each entry is `{id: "sup-<n>", box, x, y,
  rotation, template_score, verifier_score, score, suppressed_by: "verifier"}`.
  Suppressed candidates are never pins. They are part of the immutable
  scan result, and the report's `original_detections` includes them, so
  evaluation can measure what the verifier removed.
* Pins (`scan_state`, `act`, `report.corrected_pins`) gain
  `template_score` and `verifier_score` when the detection has them.
* A model-mode scan has `template: null` in the scan state. In the
  report's `original_detections`, the `template` key is **left out**
  instead, because the Phase 1 evaluator (`pinny_eval pending`) crashes
  on `null`. See the contract gaps below.
* Reviews, idempotent retries (`request_id` → scan id), stale handling and
  report export all behave exactly as in Phase 1, whatever the mode.
* `GET /api/documents/{v}/pages/{i}/scans` gives each scan's `mode`.

**Errors**

| code | HTTP | when |
|---|---|---|
| `invalid_mode` | 400 | an unknown mode, or a mode that isn't a string |
| `model_not_active` | 409 | the mode's model kind has no promoted model. The message says how to promote one. |
| `models_unavailable` | 503 | the model class can't be imported (for example, torch isn't installed). The message points to the `train` extra. |
| `template_not_used` | 400 | a `template_box` was sent in `model` mode |
| `invalid_threshold` | 400 | `threshold` outside [-1, 1] or sent in `model` mode, or `model_threshold` outside [0, 1] or sent in `template` mode |
| `model_output_invalid` | 500 | the model returned the wrong number of scores, a non-finite score or one outside [0, 1], or a point off the page. Nothing is recorded. |
| `model_load_failed` / `model_failed` / `model_mismatch` | 500 | `load` or inference raised, or the loaded `model_id` differs from the registry's |

`GET /api/models` returns
`{"modes": [{mode, available, kind, model_id, reason, needs_template, threshold?, synthetic_only?}], "active": {"verifier": id|null, "detector": id|null}}`.
Availability is checked with `importlib.util.find_spec`, which does not
import torch.

**Loading.** On each scan, the service reads `active(kind)`, so a
promotion takes effect on the next scan without a restart. It loads the
model with `Class.load($PINNY_DATA_DIR/models/<model_id>)` and caches it
by `model_id`. The real classes are imported only at that point
(`pinny.models.verifier.Verifier`, `pinny.models.point_detector.PointDetector`).
Importing `pinny.viewer` or `pinny.models.registry` does not import torch;
a test checks this. `ViewerService(model_classes={"verifier": C, "detector": D})`
injects other classes (the tests use fakes).

## Web UI

* **3. Scan** has a *Scan mode* selector. Each option names the active
  model (`Template + verifier: verifier-2026…`), or says "(no active
  model)" and is disabled. A line below it shows the active model id, its
  threshold, and whether it was trained only on synthetic data. It shows
  the reason when a mode can't run. The list refreshes when the selector
  gets focus, and after a `model_not_active` or `models_unavailable` error.
  The choice is remembered in `localStorage` (per browser, optional).
* In **model** mode, the template step (instructions, template info,
  threshold) and the *Template box (T)* tool are hidden. `T` does nothing
  and shows a short note. The template overlay isn't drawn, and *Scan*
  needs no box.
* Pins show `v 0.93 · t 0.85` (verifier · template) on the canvas and in
  the pin table when both scores are present. Otherwise they show the
  single score, as in Phase 1. The selected-pin line shows
  `verifier 0.930 | template 0.850`. The counts line names the mode and
  model, and the number of suppressed matches. The scan list labels
  non-template scans with their mode.
* Every Phase 1 behaviour and shortcut is unchanged (V/T/P, +/-/0, R, H,
  A, X/Delete, N, Esc, and Space-drag).
* Fix: `#cursor-pos` now has a fixed size. When the toolbar wrapped (at
  window widths of about 1200 px or less), the first cursor readout made
  the toolbar taller. That moved the canvas under the pointer in the
  middle of a drag, so the template box came out about 20–30 px off.

## Tests

```
python -m pytest tests/models/test_registry.py tests/viewer/test_modes.py
node tests/viewer/test_modes_browser.mjs     # Chromium, fake models, 26 checks
```

* `tests/viewer/test_modes_fakes.py` has `FakeVerifier` and
  `FakePointDetector`. They satisfy P6 (the `load` classmethod, `model_id`,
  `threshold`, `score`, `detect_points`) and read their behaviour from a
  `fake` block in `model.json`. The file also has `make_model` (a P5
  artifact with placeholder weights) and `write_evidence` (a
  `pinny.promotion` v1 report).
* Registry: listing, bad ids and artifacts, and every refusal (no
  evidence, not JSON, wrong format or version, `promote` false, missing or
  truthy-but-not-true, wrong model, wrong mode, tampered weights).
  `active.json` survives a restart, is unchanged after a crash during
  `os.replace`, and gives a clear error when corrupt. Also covered:
  `$PINNY_DATA_DIR`, the CLI, and no torch import.
* Modes, through the service and HTTP: template is the Phase 1 default;
  the modes listing; `model_not_active` for both model modes; rescoring,
  suppression and extras in `template+verifier`; threshold override and
  model caching; a new promotion takes effect on the next scan; model
  mode with no template (40 px boxes, exact points); bad model output is
  refused and not recorded; retries; restart; the evaluator accepts the
  new reports. Without torch, `template` works and the model modes give
  `models_unavailable`.
* The optional test `test_real_model_classes_satisfy_p6_and_scan` runs
  only when torch and the B and C classes are importable. It checks their
  P6 signatures. Given trained artifacts in `$PINNY_TEST_VERIFIER_DIR` and
  `$PINNY_TEST_DETECTOR_DIR`, it promotes them and runs both model modes
  end to end.

## Notes for the coordinator and the other chats

1. **`pinny/models/__init__.py` (chat B) must not import torch.** The
   viewer imports `pinny.models.registry` at start-up. On this branch,
   `pinny/models` is a namespace package with no `__init__.py`.
   `test_registry_and_viewer_import_without_torch` fails if an eager
   torch import arrives.
2. **The promotion report shape (chat E).** The registry needs `format`,
   `format_version`, `promote` (a JSON boolean) and the candidate's model
   id, in `model_id`/`model_ids` either at the top level or under
   `candidate`. `candidate.mode` is optional but checked. `reasons` is
   shown on refusal.
3. **The evaluator crashes on `"template": null`**
   (`evaluation/pinny_eval/report.py`, the `prov["template"].get`). The
   viewer leaves the key out of model-mode exports. Chat E should also
   accept `null`.
4. **`tests/viewer/browser_e2e.mjs` was already broken on `phase-2`.** It
   still imports the removed `pinny.viewer.render_stub` and expects the
   stub banner and 14 stub symbols. This branch doesn't touch it. The
   mode UI is covered by `test_modes_browser.mjs`.
5. `docs/viewer.md` (the API table) doesn't list `GET /api/models` or the
   new scan-request fields yet. This file is the reference for them.
6. I added `tests/models/__init__.py` (empty) so the `tests/models/`
   package imports. Chats B and C may add the same empty file; identical
   additions merge cleanly.
7. For `template+verifier`, P7 says `detector.version` is "the model_id
   (or both ids joined with +)". Only one model is involved, so it is the
   verifier's `model_id`. The template code version is recorded as
   `settings.code_version`.
