# Browser scan page

A single web page that runs the receptacle detector in the browser. Open a
PDF, drag a box around one receptacle, press Scan, and tune the threshold
with a slider. Nothing is uploaded.

* `index.src.html` is the source. `build.py` embeds a generated sample
  sheet (`make_sample.py`) and writes `index.html`, which is what gets
  published as the hosted page.
* The page renders with pdf.js at the canonical 200 DPI and converts to
  grayscale with OpenCV's `RGB2GRAY` weights.
* The matcher is a JavaScript port of `pinny/detection/opencv_matcher.py`:
  `TM_CCOEFF_NORMED` computed by tiled FFT correlation, the same flat-window
  mask, local-maximum rule, per-rotation cap and duplicate suppression. On
  the same raster it returned the same candidates as the Python detector,
  with scores within 1e-5, on the sample sheet and on a 7200x4800 cluttered
  sheet (3 and 4 rotations).
* Keep the two in step. If `opencv_matcher.py` or `suppression.py` change,
  update the worker script in `index.src.html`.

Limits: pdf.js and PyMuPDF anti-alias differently, so scores on the same PDF
differ slightly between this page and `scripts/smoke_test.py`. A full Arch D
sheet takes about 15-25 s with four rotations. iPhone and iPad Safari cap
canvas size and may fail on large sheets. Use a desktop browser.
