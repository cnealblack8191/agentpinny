# Legend reader (`pinny/legend/`)

Reads the symbol legend from a vector drawing set, lets a person refine it,
and keeps a saved legend per engineer for their next project. It uses no
LLM or network access: text comes from PDFium (`pypdfium2`) and drawing
geometry from `pinny.vector`, both in canonical raster pixels
(contracts §2).

```python
from pinny.legend import read_legend, EngineerLibrary, compare

legend = read_legend("set.pdf", engineer="Hanson & Reyes")   # finds the legend page
legend.needs_review()        # rows with two drawings, wrapped text, duplicate tags
legend.split("L4")           # one entry per symbol drawing
legend.set_tag("L1", "D")    # the engineer's tag, if Pinny had to make one up
legend.add((140, 820, 200, 866))   # a symbol Pinny missed: box on the legend page
legend.save("legend.json")

library = EngineerLibrary.load("hanson-reyes.json")
compare(legend, library)     # match / new / changed / missing
library.update_from(legend, project="Maple Street Clinic"); library.save("hanson-reyes.json")
```

Command line:

```
python -m pinny.legend find set.pdf
python -m pinny.legend read set.pdf [--page N] [--engineer NAME] [--out legend.json]
python -m pinny.legend compare legend.json hanson-reyes.json
```

## How it reads a legend

1. **Find the page.** A heading such as `LEGEND`, `SYMBOLS`, `SYMBOL LIST`
   or `ELECTRICAL SYMBOLS LEGEND`. A `SYMBOL` column header next to
   `DESCRIPTION` is not mistaken for a heading. Only page text is read for
   this step, so a 91 MB, 42-page set is searched in about 0.75 s.
2. **Columns.**
   * Each `DESCRIPTION` header under the heading starts a column, so
     legends with columns side by side work.
   * `SYMBOL` and `TAG`/`MARK`/`TYPE` headers mark those areas.
   * Without headers, the description column is the most common left edge
     of the text under the heading.
3. **Rows.**
   * A description line with drawing beside it starts a row, and so does a
     line that starts after a table rule.
   * A line just below with no new drawing continues the row: a wrapped
     description. A tall symbol drawn beside both lines stays with the
     first row.
   * A short capitalised line with no drawing is a section heading, such as
     `EQUIPMENT`, and becomes the group of the rows under it.
   * The column ends at a large gap or at `GENERAL NOTES`,
     `ABBREVIATIONS` or similar.
4. **Symbols.**
   * The drawing in the row's band, left of the description, is collected
     without the table rules.
   * It is grouped by horizontal gaps. More than one group sets the
     `combined` flag.
   * Short text drawn inside the symbol area, such as `GFI` or `WP`, is
     kept as a label.
5. **Engineer's tag**, in this order:
   * a tag column (`tag_column`);
   * a description written `D1 - DUPLEX…` (`description`);
   * a single label drawn with the symbol (`symbol_label`).
   
   Only when none exists does Pinny make one from the name's initials. It
   is marked `generated` with a `generated_tag` flag so the screen can say
   so.
6. **Defaults.**
   * Lines, not devices, start as not counted: homeruns, conduit, and
     drawings much wider than tall.
   * So do items another trade provides: "BY MECH", "BY OTHERS" and
     similar.
   * The group comes from the legend's section heading if there is one,
     otherwise from keywords.

## Legend file (`pinny.legend` v1)

Each entry contains:
* `id`, `tag`, `tag_source`, `name`, `description`, `group`;
* `count` and `not_counted_reason`;
* `symbol_boxes` (one per drawing, canonical px on the legend page),
  `labels` and `label_boxes` (where each label is, used to check labels on
  scanned sheets), `row_box`;
* `flags`, `source` (`read` or `user`), `confirmed`, `signatures`;
* `status`: `ok`, `check` (needs a look) or `added` (added or split by a
  person).

The file also keeps each legend row's text, so a symbol added by hand takes
its description from its row. It also keeps an `edits` log of every change
with a timestamp, which lets the learning store separate what Pinny read
correctly from what a person fixed.

Flags that need a look before scanning: `combined`, `wrapped`,
`duplicate_tag`. `confirm(id)` accepts an entry as it is. Renaming a wrapped
entry, or splitting a combined one, also clears its flag.

## Saved legend per engineer (`pinny.legend_library` v1)

* **Matching** a new legend against the saved one happens in two passes:
  * exact tag (when it is the engineer's own) or identical wording;
  * then, for what is left, similar wording (word overlap) together with
    the symbol's drawing.
* **Result for each symbol:** `match`, `new` or `changed`.
  * Changes are listed: wording, tag, count setting, or the symbol drawn
    differently.
  * Saved symbols missing from this set are listed as `missing`.
* **Drawing fingerprint:** which cells of a 16×16 grid over the symbol's
  box contain ink. Below 0.6 similarity (Jaccard) the drawing counts as
  changed.
* **Updating:** `update_from(legend, project=…)` takes the reviewed
  symbols in and keeps saved symbols this set didn't use.

## Limits

* **Scanned legends** raise `raster_page`, because reading scanned text
  needs OCR (Tesseract or PaddleOCR), which isn't built yet. Symbols can
  still be added by hand.
* **Rotated text:** legends drawn with rotated text inside an upright sheet
  aren't handled. Sheets that display upright through `/Rotate` are.
* **Text drawn as outlines:** some CAD exports turn text into lines (SHX
  fonts). PDFium can't read that text, so such a legend reads no rows.
* **Tests:** all tests use synthetic PDFs. The row rules should be checked
  against real legends from several engineers.
