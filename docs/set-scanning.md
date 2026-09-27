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

## How each sheet is scanned

| Sheet | Method |
|---|---|
| Vector (CAD export) | The sheet is loaded and indexed once, then every legend symbol is matched against it with the vector matcher. Block reuse (the same Form XObject as the legend's) gives exact matches. Otherwise flattened line work is matched at any quarter turn or mirror, tolerating wires drawn through symbols. |
| Scanned | The sheet is rendered at 200 DPI (PDFium, contracts §2) and every legend symbol is matched with the raster template matcher. |
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
* **Storing and reviewing:** results aren't written to the learning store
  yet, and there is no review screen for them. Both are next.
