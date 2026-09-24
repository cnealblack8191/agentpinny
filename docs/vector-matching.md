# Vector symbol matching (`pinny.vector`)

Most construction drawings are CAD exports. Their receptacle symbols are
vector paths, and often one block definition placed many times. Matching
those drawing primitives is faster and more exact than raster template
matching, and it gives exact rotation and mirror labels. `pinny.vector` does
this. The raster matcher (`pinny.detection`) stays the fallback for scans.

This module does no rendering and makes no network or LLM calls.

## Public API

```python
from pinny.vector import VectorMatcher, VectorSettings, classify_page

kind = classify_page(pdf_path, page_index)       # PageKind(kind, stats)
result = VectorMatcher().detect(pdf_path, page_index,
                                exemplar_box_px,  # BoundingBox | {x,y,width,height} | (x,y,w,h)
                                settings=VectorSettings())
result.to_dict()
```

`to_dict()` has the same shape as the scan result in contracts §4. The app
adds `scan_id`, `document` and `created_at`.

```json
{
  "coordinate_frame": {"space": "canonical_raster_px", "dpi": 200, "width": 7200,
                       "height": 4800, "origin": "top-left", "y_axis": "down"},
  "template": {"box": {"x": 0, "y": 0, "width": 40, "height": 40}},
  "detector": {"name": "pinny-vector-xobject" | "pinny-vector-paths",
               "version": "1", "settings": {"...": "VectorSettings"}},
  "detections": [
    {"id": "det-1", "box": {"x": 0, "y": 0, "width": 34, "height": 51},
     "x": 17.0, "y": 25.5, "score": 1.0, "rotation": 90,
     "mirrored": true, "angle": 30.0, "source": "vector-xobject"}
  ],
  "strategy": "xobject" | "paths",
  "page_kind": {"kind": "vector", "stats": {"...": "..."}},
  "threshold": 0.9, "truncated": false, "elapsed_seconds": 0.02,
  "warnings": [], "stats": {"...": "..."}
}
```

* `box` holds integer canonical px. It is the smallest integer box that
  covers the symbol's line centrelines, so stroke width is not included.
  `x` and `y` are the box centre.
* `rotation` is the clockwise quarter-turn, in the y-down raster, that
  carries the exemplar onto the match. This is the convention in
  `docs/detection.md`.
* `mirrored` appears only when it is `true`. It means the exemplar was
  mirrored left-right first and then rotated.
* `angle` appears only when a placement is more than
  `angle_tolerance_deg` off a quarter turn. It is the exact clockwise
  angle. `rotation` is then the nearest quarter turn, and a warning is
  added.
* `source` is `vector-xobject` (block reuse, `score` 1.0) or `vector-path`
  (flattened geometry, `score` in [0, 1]).
* Symmetric symbols are labelled consistently. When several poses score
  the same, the matcher picks the unmirrored one with the smallest
  rotation.

Errors are `VectorMatchError(code, message)`, a subclass of
`pinny.detection.DetectionError`. The codes are `pdf_unreadable`,
`pdf_encrypted` (a password is needed; PDFs with permission-only
encryption are read with a warning), `page_index_out_of_range`,
`raster_page` (`RasterPageError`, meaning "use the raster matcher"),
`invalid_exemplar_box`, `no_vector_geometry`, `invalid_settings` and
`timeout`.

## Coordinate mapping

The code is in `pinny/vector/frame.py`. The visible page area is the
CropBox. It defaults to the MediaBox and is clipped to it. `/MediaBox`,
`/CropBox`, `/Rotate` and `/Resources` are inherited from the page tree.
With CropBox `(x0, y0, x1, y1)`, `W = x1 - x0` and `H = y1 - y0`:

1. `u = x - x0`, `v = y1 - y` puts the top-left corner at the origin with y down.
2. Apply the `/Rotate` clockwise turn so the page stays in the positive quadrant:
   `0: (u, v)`, `90: (H - v, u)`, `180: (W - u, H - v)`, `270: (v, W - u)`.
3. Scale by `px = pt * 200 / 72`.

The canonical size is `ceil(W' * 200/72) x ceil(H' * 200/72)`. `W'` and
`H'` are swapped when `/Rotate` is 90 or 270. `/Rotate` values that are
negative or above 360 are normalised. A value that is not a multiple of 90
is treated as 0, which is what viewers do. `UserUnit` is ignored.

Tests cover each of the four rotations against three CropBox origins (zero,
offset and negative). They check round trips in both directions, page
corners, the rule "top-left of the upright view", and an independent
check: pdfium renders a mark at 200 DPI and the test finds it within
1.5 px of where the mapping predicts.

## How it works

### 0. Page classification (`classify_page`)

The content stream is interpreted once (`content.py`, using pikepdf). The
interpreter tracks `q`/`Q`/`cm`, descends into Form XObjects, and counts
paths, image XObjects and inline images. It also sums the area the images
cover on the page.

| kind | rule | matcher |
|---|---|---|
| `raster` | images cover ≥ 50% of the page and there are < 200 path segments | raster only; `detect` raises `raster_page` |
| `mixed` | images cover ≥ 15% (and not raster) | vector runs with a warning, and the app should also run raster |
| `vector` | otherwise | vector |

### 1. Block (Form XObject) reuse (`xobject_matcher.py`)

Each `Do` of a Form XObject is recorded, at any nesting depth, with its full
transform: form `/Matrix`, then the CTM, then user→px. Its placed box is the
ink bounding box of the paths drawn inside it. The form `/BBox` is used only
when the form draws no paths. The placement whose box has the best IoU with
the exemplar box becomes the exemplar. At a tie, the deeper placement wins.
It must reach `xobject_min_iou`, which defaults to 0.5. Every placement with
the same identity is then returned. The identity is a sha256 of the decoded
content stream, `/BBox` and nested XObject identities. Two separate objects
with identical content therefore group together.

The rotation comes from `L_i · L_ref⁻¹`, the 2×2 linear part in px:

* `det < 0` means the placement is mirrored.
* The angle is `atan2` of the de-mirrored matrix, snapped to a quarter turn.

In `auto` mode this strategy is used only when the XObject has two or more
placements. A lone form falls through to path matching, which also sees
geometry drawn inside forms. This handles sheets that wrap everything in
one big form.

### 2. Flattened paths (`path_matcher.py`)

1. **Primitives.** Every stroked or filled segment (`l`, `c`, `v`, `y`,
   `re`, `h`, close-and-paint) becomes a cubic Bézier in canonical px, with
   its CTM applied. Segments inside forms are included, at any depth.
   Clipping-only paths (`n`) are ignored.
2. **Exemplar.** The primitives that lie *entirely* inside the exemplar box
   (plus the tolerance). A wire that only crosses the box is left out.
3. **Connected components.** Primitives are joined when their bounding
   boxes come within `tolerance_pt` of each other. Candidate pairs come
   from a uniform grid, and a vectorised union-find merges them.
4. **Signature fast path.** For the exemplar and for each component with the
   same primitive count, the endpoints and control points are normalised by
   centroid and RMS radius. They are then transformed by each of the 8
   dihedral maps (4 quarter turns × optional mirror), quantised and sorted.
   The canonical hash is the minimum over the 8 maps. If the hashes are
   equal, that gives the pose directly. Otherwise a symmetric Hausdorff test
   on the normalised point sets runs, which absorbs quantisation boundary
   effects. Scale must agree within `scale_tolerance` (default 2%).
5. **Anchor hypotheses.** These handle symbols touching other line work,
   such as a wire drawn through them. Up to `max_anchors` (default 2)
   exemplar primitives are used as anchors. The matcher picks those whose
   type and length are rarest on the page. Each anchor is paired with every
   page primitive of the same type and chord length (±2 tol). Each dihedral
   map that carries the anchor's four control points onto the candidate's
   (in either direction, within 2 tol) gives a pose `x → D·x + t`.
6. **Coarse filter.** A bitmap of the page's line work, with cell size tol
   and dilated, rejects most poses cheaply. It never scores a pose below
   its exact coverage.
7. **Exact score.**
   * `coverage` is the fraction of exemplar sample points (spaced ≤ tol
     apart) that lie within tol of any page segment. This is a directed,
     thresholded Chamfer distance. Extra line work does not lower it, so
     crossing wires are tolerated.
   * `precision = L_ex / (L_ex + L_extra)`. `L_extra` is the length of page
     primitives that lie entirely inside the placed symbol and that the
     exemplar does not explain (less than 80% of their length is on the
     exemplar geometry). This catches look-alikes that are the exemplar
     plus extra marks.
   * `score = coverage × precision`, in [0, 1]. The matcher reports matches
     where `score ≥ threshold`, which defaults to **0.9**.
8. **Duplicates.** The matcher keeps the best match per location, using a
   centre distance of `0.5 ×` the exemplar's short side. Once a location has
   a perfect match, other poses there are not verified.

Scores in the tests show the margins. An exact symbol scores 1.0, including
one with a wire through it. A "simplex" (one centre line instead of two)
scores 0.68. A "quad" (an extra bar inside) scores 0.86. A square body
instead of a circle scores 0.41. A 1.3× scaled copy is rejected by the
scale check.

### Settings (`VectorSettings`)

| field | default | meaning |
|---|---|---|
| `threshold` | 0.9 | minimum path score |
| `tolerance_pt` | 0.5 | geometric tolerance (1.39 px) |
| `strategy` | `auto` | `auto`, `xobject` or `paths` |
| `xobject_min_iou` | 0.5 | exemplar box ↔ placement box |
| `rotations`, `allow_mirrored` | all, true | which poses to report |
| `scale_tolerance` | 0.02 | signature path only |
| `angle_tolerance_deg` | 1.0 | when to report `angle` |
| `max_anchors` | 2 | hypotheses sources |
| `extra_match_fraction` | 0.8 | when an extra primitive counts as explained |
| `duplicate_center_ratio` | 0.5 | duplicate distance ÷ exemplar short side |
| `max_hypotheses`, `max_exemplar_primitives`, `max_detections`, `max_runtime_seconds` | 2M, 2000, 5000, 60 | resource bounds |
| `allow_mixed` | true | run on mixed pages |

## Measured runtime

`tests/vector/test_benchmark.py` builds an Arch D sheet (2592 × 1728 pt,
7200 × 4800 px). It has 500 receptacle symbols at all 4 rotations, some of
them mirrored, and 20,000 extra random line segments 20–40 pt long. That
comes to 24,000 primitives. The line work is dense enough that most symbols
touch a random line, so only 7 of 500 took the signature fast path. The
timings were taken in a 4-core x86_64 container running single-threaded
Python 3.11 with numpy 2.4 and pikepdf 10.13. Each time is the full
`detect()` call, including opening and parsing the PDF.

| page | strategy | detections | time |
|---|---|---|---|
| symbols flattened into paths + 20k lines | `paths` | 500 / 500, 0 false | **2.2 s** |
| symbols as one Form XObject + 20k lines | `xobject` | 500 / 500, 0 false | **0.6 s** |
| 20 symbols, wires, 6 distractors (tabloid) | `paths` | 20 / 20 | ~0.15 s |

For comparison, `docs/detection.md` records the raster matcher at about
7.5 s for four rotations on a page of the same size. Vector matching also
gives exact boxes and labels.

The time for the path strategy splits roughly into: content parsing
(~0.5 s), the coarse bitmap (~0.8 s) and pose verification (~0.6 s). The
XObject strategy spends almost all its time in content parsing.

## Limits

* **SHX text and outlined fonts.** AutoCAD SHX text is exported as short
  strokes. A symbol with a letter in it ("GFI", "WP") matches only when the
  letters are drawn the same way. Letter strokes near a symbol can also end
  up inside the exemplar box. Draw the box tightly around the symbol. Real
  PDF text (`Tj`) is not geometry here, so it is ignored.
* **Symbols split across layers or paint operations.** Primitives are
  matched regardless of layer, colour or line weight. Optional-content
  (`/OC`) visibility is ignored, so hidden layers are matched too.
* **Clipped symbols.** Clipping paths are not applied. A symbol cut by a
  viewport or match-line clip matches as if unclipped, while a symbol that
  is only partly drawn scores its partial coverage.
* **Different segmentation.** The signature path needs the same primitive
  structure, for example a circle drawn as 4 arcs rather than 2. The anchor
  path needs the anchor primitive to be drawn the same way. Coverage itself
  does not depend on segmentation. The same symbol drawn with different
  segmentation, or with dashed lines, may be missed.
* **Scale.** Path matching assumes the same size (±2% on the signature
  path; exact on the anchor path). XObject matching accepts any scale.
* **Arbitrary angles.** XObject matching reports any angle, with a
  warning. Path matching searches only quarter turns and mirrors.
* **Dense clutter.** Coverage counts any nearby line work. In very dense
  hatching, a look-alike missing a small part could reach the threshold.
  Precision only penalises extra primitives lying fully inside the symbol.
* **Mixed exports.** In `auto` mode, if the exemplar is a reused block,
  only placements of that block are returned. Copies of it that were
  exploded into paths on the same sheet are not found unless you rerun
  with `strategy="paths"`.
* **Type 3 fonts, shadings, and annotations** (for example Bluebeam
  markups in `/Annots`) are not interpreted.

## How the app should choose

```
kind = classify_page(pdf, i)
if kind.kind == "raster":                  -> raster matcher
else:
    try:  r = VectorMatcher().detect(pdf, i, box)
    except VectorMatchError as e:
        if e.code in ("raster_page", "no_vector_geometry"): -> raster matcher
        else: surface e.code / message
    if r.strategy == "paths" and len(r.detections) <= 1:  -> also try the raster matcher
    if kind.kind == "mixed": also run the raster matcher and merge
      (drop raster hits whose centre is within 0.5 × short side of a vector hit)
```

* The box the user draws is in canonical px, the same frame the viewer and
  raster matcher use. No conversion is needed.
* Keep the vector `score` in the scan's `score` field. Its scale differs
  from NCC, so record the detector name and tune thresholds per detector.
* `no_vector_geometry` usually means the symbol is part of an image or is
  font text. Raster matching is the right fallback in both cases.

## Dependencies

| package | licence | use |
|---|---|---|
| `pikepdf` | MPL-2.0 | open PDFs, parse content streams (runtime) |
| `numpy` | BSD | geometry (runtime) |
| `pypdfium2` | Apache-2.0 / BSD-3 | test-only independent render check |

PyMuPDF (AGPL) is deliberately not used. pdfminer.six was not needed,
because pikepdf's content-stream parser gives exact geometry, including
paths inside forms, with less overhead.
