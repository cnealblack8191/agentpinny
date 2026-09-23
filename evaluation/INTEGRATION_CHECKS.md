# Integration checks

Status as of 2026-09-23. When this was written, the repository contained no
application code: no upload, scan, overlay, correction, persistence or export
implementation, and no `docs/contracts.md`. So none of the app-level checks
could be run. Each check below is marked as either **Ran** or **Awaiting
integration**. A check that has not been run is not recorded as passing.

## Checks actually run

| # | Check | Command | Result |
|---|---|---|---|
| E1 | Evaluator self-tests (fixtures, brute-force matcher cross-check with 400 random cases, input rejection, CLI guards) | `cd evaluation && python3 -m unittest discover -s tests -v` | Ran: 15 tests passed |
| E2 | `score` on a synthetic mixed fixture | see `samples/synthetic_mixed_report.md` | Ran: TP 3 / FP 3 / FN 2, matches the hand-authored `expected.json` |
| E3 | `pending` on a page without a reference | see `samples/pending_report.md` | Ran: reports "Not measured — verified reference pending" |
| E4 | Rejection of a mismatched document version | `score` on `fixtures/synthetic/reject_version_mismatch` | Ran: exit 2 with the mismatch named |

## Awaiting integration

Each check needs the real app plus at least one real drawing. Checks that
produce accuracy numbers also need a **verified** reference; see README,
"Labelling a real page".

| # | Area | What to verify | Pass criterion | Status |
|---|---|---|---|---|
| I1 | Upload | Uploading a PDF records a stable `document_id` and a content-hash `document_version`. Re-uploading the same bytes gives the same version; a changed file gives a new one. | Hashes are reproducible; a changed file gets a different version | Awaiting integration |
| I2 | Page selection | The selected page's `page_index` (0-based) is carried into the scan result and its export. | The index in the export equals the page shown to the user | Awaiting integration |
| I3 | Scan | A scan writes an immutable original-results record (scan ID, detector name/version/settings, canonical raster size, points) that later corrections do not overwrite. | The export converts to `pinny.detections` v1 with `provenance: original_detector_output` and passes `pending` | Awaiting integration |
| I4 | Overlay alignment | Pins drawn on screen line up with canonical raster coordinates at several zoom levels and page rotations. | Placing test pins at known raster points (for example the 4 corners and the centre) and reading them back shows an error ≤ 1 canonical px | Awaiting integration |
| I5 | Corrections | Adding, moving or deleting a pin changes only the corrected set. The original detector record is unchanged, and edited items are marked with a non-`detector` source. | The original-results hash is the same before and after editing; the evaluator rejects a corrected set passed as detections | Awaiting integration |
| I6 | Persistence | After a reload or restart, the original results and the corrected set both reload unchanged, with the same IDs and coordinates. | Byte-identical (or canonical-JSON identical) exports before and after the reload | Awaiting integration |
| I7 | Export | The export keeps original detector results and final corrected pins separate, with document identity, page and frame on both. | Both parts are present and labelled; coordinates round-trip through import with no drift | Awaiting integration |
| I8 | Real-drawing accuracy | Score one scan of a verified labelled page at a tolerance chosen in advance. | A report whose status is "Measured against verified reference" | **Not measured — verified reference pending** |

To record a result, replace the Status cell with the date, the exact command
or steps, the commit tested, and the output location.
