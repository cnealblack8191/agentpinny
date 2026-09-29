# Scanning a set from its legend (`pinny/scan/`)

Counts every counted symbol of a reviewed legend on every sheet of the
drawing set. Nobody has to draw a box on a sheet: each legend symbol is the
example.

```python
from pinny.legend import read_legend
from pinny.scan import scan_set, SetScanSettings

legend = read_legend("set.pdf")            # review / edit it first (docs/legend-reader.md)
result = scan_set("set.pdf", legend)
result.counts()          # {"D": 212, "G": 31, "DD": 4, "Q": 18}  in legend order
result.counts_by_sheet() # {1: {...}, 2: {...}}
result.to_dict()         # pinny.set_scan v1 JSON, every detection with tag, box, score, rotation
```

```
python -m pinny.scan set.pdf [--legend legend.json] [--pages 1,2,3] [--out result.json]
```

The scan refuses to start when:
* the legend was read from a different file (`legend_mismatch`);
* a legend symbol still needs a look (`legend_needs_review`);
* nothing is set to be counted (`nothing_to_count`).

The legend page is skipped unless `skip_legend_page=False`.

`progress(page_index, done, total)` is called as each sheet is finished;
`matched(page_index, done, total)` as each sheet's matching (the slow part)
finishes, before the size search, so a caller can show progress while the
sheets are searched.

## How each sheet is scanned

| Sheet | Method |
|---|---|
| Vector (CAD export) | The sheet is loaded and indexed once, then every legend symbol is matched against it with the vector matcher. Block reuse (the same Form XObject as the legend's) gives exact matches. Otherwise flattened line work is matched at any quarter turn or mirror, tolerating wires drawn through symbols. |
| Scanned | The sheet is rendered at 200 DPI (PDFium, contracts §2) and every legend symbol is matched with the raster template matcher. The sheet is finished at once, so only one page raster is held at a time. |
| Mixed | Both, merged. |

**Legend symbols as examples**
* `pinny.vector.make_exemplar` takes a symbol from the legend page as a
  `VectorExemplar` that works on any page of the same file.
  `detect_exemplar`, or `prepare_page` + `match_prepared` for many symbols
  per sheet, finds it.
* Raster templates are cut from the legend render. Only pixels within 3 px
  of the symbol's own lines are kept, so table lines touching the symbol
  and nearby labels don't become part of the template.

## One symbol per spot

Legend symbols often contain one another. A GFCI is often a duplex with
"GFI" written next to it, and a dedicated receptacle is a duplex plus a
triangle. Every legend symbol is matched, then each spot keeps one:

1. **Labels are required.** A symbol whose legend drawing carries a label
   (`GFI`, `WP`, `USB`) only counts where that label is written next to it,
   within 1.5 symbol sizes.
   * On vector sheets the label is read from the PDF text.
   * On scanned sheets, the label's pixels are cut from the legend and
     matched next to the symbol. That works because one set uses one font,
     so no OCR is needed.
2. **Among matches at a spot scoring about as well as the best** (within
   0.05, or 0.10 on scanned sheets), the winner is decided in this order:
   * a found label;
   * then the more detailed legend drawing (more line work, or more dark
     pixels for templates);
   * then score;
   * then legend order.
3. Dropped matches are counted per tag in `SheetResult.dropped`.

**Symbols Pinny can't tell apart.** When two legend symbols are drawn the
same and neither has a label, the result carries a warning ("D and U are
drawn the same…"). Matches go to the first in the legend. Merge them or add
a label in the legend screen.

## Legend drawn at another size

* **When it runs:** a symbol with no matches anywhere at legend size is
  tried at 0.5, ⅔, 0.75, 0.8, 1.25, 4⁄3, 1.5 and 2×.
* **How the size is chosen:** it is picked on up to 3 sample sheets, then
  every vector sheet is searched once at that size.
* **Where it shows:** the chosen size is in `result.scales`, with a
  warning such as "D is drawn at 0.67× its legend size".
* **Turning it off:** `scale_search=False`.

## Measured

* **Synthetic set (legend, plan, rotated plan):** exact counts for duplex,
  GFCI (label), dedicated (triangle) and quad, in about 0.25 s.
  * The thermostat marked "by mech" isn't counted.
  * The duplex with a wire through it is found.
* **Scanned copy of the plan:** the same exact counts.
* **91 MB, 42-page set:** 41 sheets, 40 of them dense (500 symbols and
  20,000 line segments each).
  * About 99 s, roughly 2.4 s per dense sheet, with 7 legend symbols.
  * Peak memory about 600 MB, because sheets are loaded one at a time.
  * The first version kept every sheet for the size search and used
    1.6 GB and 149 s.

## Limits and next steps

* **Symbol types:** sheets are searched once per legend symbol. Legends
  with 40+ symbols will take proportionally longer. Sharing work between
  symbols with the same base shape is the next speed-up.
* **Raster size search:** scanned sheets don't search other sizes yet.
* **Label reading:** labels must be the same text as in the legend.
  Different fonts on scanned sheets may be missed.
* **Tests:** all results so far come from synthetic sets. Before trusting
  counts, run the evaluator (`pinny_eval score-corpus`) on real, labelled
  sheets.
* **Tags corrected after the scan:** pins keep the tag they were scanned
  with. Renaming a tag in the legend afterwards needs a new whole-set scan
  for the counts to use the new tag.

## Storing and reviewing (in the viewer)

Built: the viewer runs the whole workflow (docs/viewer.md "Legend and
whole set", docs/training-site.md sections 3-5).

* **Legend.** `read_legend` (a sandboxed job) reads the legend; the web
  process stores it as `documents/<sha256>/legend.json` (`pinny.legend` v1)
  and applies the reviewer's edits, each logged with who made it. The
  legend's edit log also records `read` and `confirm_legend`; a legend is
  confirmed while its last edit is a confirmation, so any later change
  needs a new one.
* **Scan.** `scan_set` (a sandboxed job in the `scan` pool) scans every
  sheet with a snapshot of the confirmed legend,
  `documents/<sha256>/set_scans/<run_id>/legend.json`, and writes
  `result.json` (`pinny.set_scan` v1) beside it. Its payload is ids only
  (`document_version`, `run_id`, `legend_sha256`); the child has no access to
  the learning store.
* **Recording.** When the job is done, the web process records one ordinary
  scan per sheet: `detector.name` `pinny-set-scan`, `detector.version` the
  build's version when the scan started, and `settings` with the legend's
  sha256, legend page, sizes (`scales`), warnings, method and the vector and
  raster settings. Scan result `mode` is `legend`; there is no template.
  Each detection carries `class_label` (the legend tag) and `legend_entry`,
  so each pin starts with `class_label` = its tag (learning store
  `Detection.class_label`, additive). Scan ids derive from the run id and
  the page, and the content from the run, so recording again after a
  restart repeats nothing (`LegendService.resume_interrupted` on startup).
* **Frames.** Each sheet's frame from the job must equal the render
  service's frame for that page, or the sheet is not saved; the tests check
  that pins land on the symbols in the viewer's raster on a `/Rotate 90`
  sheet.
* **Review and training.** The pins are reviewed like any scan's; "Mark
  page fully reviewed" applies per sheet, and a new whole-set scan reopens
  the sheets it scans. Datasets use them like any other reviewed scan.

## Learning each symbol type

Each legend tag can have its own **learned package** (`pinny/model/`, see
`model-package.md`), trained from that tag's reviews only, so light fixtures
and receptacles learn separately.

* **Training** (Training → Symbol types, admin; `POST
  /api/training/symbols/train {tag}`): a `train_symbol` job in the `train`
  pool (`pinny/jobs/symbol_tasks.py`). The examples are the contracts §6
  crops of the tag's reviewed machine matches from every drawing: approved
  ones are positives, rejected ones negatives. Pins added by hand are
  counted but not learned from yet (their crop has no symbol box). The
  package starts from the legend's drawing of the symbol, as the newest
  whole-set scan cut it (`set_scans/<run>/symbols/<entry>.png`, listed in
  `symbols.json`). It holds a template bank (the legend drawing plus
  approved variants), a negative bank of rejected look-alikes and, with at
  least 3 approved and 3 rejected examples, a kNN verifier.
* **Check before use** (`pinny/model/judge.py`): each drawing (or each
  sheet, when all reviews come from one drawing) is judged by a package
  trained on the others. The package is switched on only if, on those
  held-out reviews, it lost at most 5 % of the approved matches and threw
  out at least one rejected match (and no fewer than it lost). Otherwise it
  is saved but off, with the reason. An admin can switch a package off, and
  back on only if it passed.
* **Storage:** `models/symbols/index.json` (`pinny.symbol_models` v1) and one
  `.pinny` file per tag, with its sha256 in the index.
* **Scanning:** when a whole-set scan starts, the web tier copies the active
  packages of the legend's counted tags into the run
  (`set_scans/<run>/packages/`, listed with their sha256 in
  `packages.json`), since the sandboxed scan worker cannot read `models/`.
  The job checks each sha256 and loads them. A package is used only if its
  copy of the legend drawing correlates at least 0.5 with this legend's
  drawing of the symbol, so a tag that means another symbol in another
  drawing set is left alone (`learned[tag].used` is false, with the reason).
  On every sheet, before one symbol per spot is chosen, each match of a
  learned type is judged on the sheet's 200 DPI raster: it is thrown out if
  it resembles a rejected example more than any template, or if the
  verifier (when it decides) scores it below 0.5. The approved variants
  (templates other than the legend drawing) are also searched, and new
  finds are added with `source: "learned"`. Each sheet's result records
  `learned: {rejected: {tag: n}, added: {tag: n}}`, the run records which
  packages it used, and the viewer's scan summary says what they did.
* **Tested** on a drawing set whose crossed-out receptacles (demolished
  devices) match the legend's duplex: after rejecting them on two sheets,
  the package throws them out on an unreviewed sheet and keeps every real
  receptacle (`tests/scan/test_learned.py`,
  `tests/viewer/test_symbol_learning.py`, in-process and in sandboxed
  workers). It has not yet been measured on real drawing sets.
