# Smoke test on a real drawing

No install needed: `scripts/browser-scan/` is a web page that does the same
thing in the browser (open a PDF, box a symbol, scan, tune the threshold).
See its README. The script below is the Python route.

`scripts/smoke_test.py` runs the detector on one page of a real PDF and
shows what it found. It is the quickest way to see whether the default
threshold (0.80) works on real drawings, which has never been measured.

It uses PyMuPDF as a temporary renderer until the foundation's render service
lands (`docs/contracts.md` section 9). Its raster follows section 2: 200 DPI,
`/Rotate` applied, RGB, top-left origin.

## Setup

```sh
pip install -e ".[smoke]"
```

## Steps

1. Render the page and find a symbol:

   ```sh
   python scripts/smoke_test.py path/to/drawing.pdf --page 0
   ```

   Open `smoke-out/grid.png`. Grid lines are every 250 canonical px.
   Find one clean receptacle symbol and note a tight box around it
   (`x,y,width,height` in px). `page.png` is the same raster without the grid,
   so you can crop more precisely in any image editor.

2. Scan:

   ```sh
   python scripts/smoke_test.py path/to/drawing.pdf --page 0 --template-box 1830,2210,42,42
   ```

   Open `smoke-out/overlay.png`. The template is outlined in blue and each
   detection is boxed in red with `rank:score`.

3. Tune. Try `--threshold 0.7` or `0.9` and compare. Use `--rotations 0`
   if every symbol on the sheet is upright.

## Outputs (in `smoke-out/`, git-ignored)

| File | Contents |
|---|---|
| `page.png`, `grid.png` | Canonical raster, without and with a pixel grid |
| `template.png` | The cropped symbol |
| `overlay.png` | Detections drawn on the page |
| `result.json` | Raw `DetectionResult` |
| `detections.json` | `pinny.detections` v1 export (contracts section 4) |

`detections.json` can be scored with the evaluator once a verified reference
exists for the page. Pass `--document-id` so it matches that reference, and
see `evaluation/README.md`.

## What to look for

* Missed symbols: they are often drawn at a different scale, mirrored, or
  crossed by other line work. The detector searches at exact scale and in
  quarter turns only.
* False hits on similar symbols (switches, junction boxes): raise the threshold.
* Runtime: a full Arch D sheet takes about 11 s with four rotations.

Don't commit drawings or outputs.
