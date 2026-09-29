# OCR module (`pinny/ocr/`)

Optional text recognition on canonical page rasters. **OCR is off by
default.** Nothing runs, and nothing needs to be installed, until the user
turns it on in the app settings. Its first use is reading the **sheet number,
sheet title and revision** from a drawing's title block.

The first engine is [Tesseract](https://github.com/tesseract-ocr/tesseract)
(Apache-2.0). Pinny runs its command-line tool as a subprocess and does not
bundle or link it, so no Python wrapper dependency is added. The module uses
`numpy` and, for upscaling, `opencv-python-headless`. Both are already
required by detection (contracts section 8).

## Turning it on

The choice is an app setting saved in `$PINNY_DATA_DIR/settings.json` as
`{"ocr": {"engine": "tesseract"}}`. A missing file or key means off. Other
top-level keys in that file are left untouched, so other modules can share it.

Precedence, highest first:

1. an explicit `get_engine(name)` argument
2. `$PINNY_OCR_ENGINE` (`none` or `tesseract`), an override for scripts and CI
3. the saved app setting
4. `none`

| Call | For |
|---|---|
| `engine_options()` | the settings screen. It lists `Off` first as the default, then each engine with its `license`, whether it is `available` (installed), `selected`, and `overridden` (true when the env var decides). |
| `set_engine("tesseract")` / `set_engine("none")` | saving the user's choice |
| `get_engine()` | callers. Returns `None` while off. Raises `ocr_unavailable` if an engine is chosen but not installed, so the app can tell the user instead of silently skipping OCR. |

Install Tesseract separately, for example `apt install tesseract-ocr` or
`brew install tesseract`. Point `$PINNY_TESSERACT_CMD` at it if it is not on
`PATH`. Other languages need their traineddata, for example `tesseract-ocr-fra`.

## Sheet info

```python
from pinny.ocr import read_sheet_info

info = read_sheet_info(page)   # uses the app setting; raises ocr_disabled while OCR is off
info.sheet_number.value        # "E-101"
info.sheet_title.value         # "FIRST FLOOR POWER PLAN"
info.revision.value            # "2"
info.warnings                  # read these before trusting the result
info.to_dict()                 # JSON-serialisable
```

* **Where it looks.** By default it searches the bottom-right corner of the
  sheet, `title_block_region()` = x 65–100 %, y 55–100 %. On Arch D at
  200 DPI that is about 12.6 × 10.8 in. It holds the sheet number in both the
  right-edge-strip and the bottom-strip layouts. Pass `region=OcrBox(...)` for
  a title block elsewhere, or `full_page=True`, which is slower.
* **How fields are found.** Labels such as `SHEET NO`, `SHEET NUMBER`,
  `DWG NO`, `SHEET TITLE`, `DRAWING TITLE`, `REV` and `REVISION` are matched.
  The value is the text after the label on the same line, beside it, or below
  it. A title can run over up to three lines and stops at the next label
  (`DATE`, `SCALE`, `DRAWN BY` and so on).
* **Each field is a `SheetField`** with `value`, a page-frame `box`, the mean
  `confidence`, `rotation`, `alternatives` and a `method`:

  | method | meaning | trust |
  |---|---|---|
  | `label` | found next to its label | best |
  | `table` | the highest entry in a `REV / DATE / DESCRIPTION` table | good, if the table is the current one |
  | `pattern` | no label was read, so this is the tallest text shaped like a sheet number (`E-101`, `E1.01`, `FP-2`, `A501A`) | a guess, with a warning |

* **Warnings.** A warning is added when a field is missing, when the number was
  guessed by pattern, or when a value touches the edge of the search region.
  The last one catches text cut in half, for example `E-10` for `E-101`.
* The viewer should show the values for the user to confirm. They aren't
  ground truth.

## Upscaling and rotated text

Both are on by default whenever OCR itself is on.

* **Upscaling.** `upscale_to_dpi=300` enlarges the image 1.5× with bicubic
  interpolation before OCR, because Tesseract is tuned for about 300 DPI and
  the canonical raster is 200. Boxes are mapped back to canonical px, rounding
  outwards. Set it to `None` to read at 200 DPI.
* **Rotations.** `rotations=(0, 90, 180, 270)` runs one engine pass per
  clockwise quarter-turn of the page, so vertical and upside-down labels read
  upright. Each word records the `rotation` it was read at. 90 means the text
  runs bottom to top on the page. Where passes overlap, the reading with the
  most evidence (confidence × alphanumeric characters) wins, and words from
  the same pass never suppress each other.
* **Cost.** Upscaling multiplies pixels by 2.25. Every rotation is another
  full pass, and `max_runtime_seconds` (300) covers all of them. A default
  title-block read on a synthetic Arch D sheet took about 3–4 s. Full-page OCR
  on real, dense drawings hasn't been measured. For bulk text, use a region or
  fewer rotations.

## Interface

```python
from pinny.ocr import get_engine, OcrSettings, OcrBox

engine = get_engine()
if engine is not None:
    result = engine.recognize(page, OcrSettings(), region=OcrBox(x, y, w, h))
    for word in result.words:
        word.text, word.box, word.confidence, word.rotation
    result.lines()                # words grouped per engine line and rotation
    result.text
```

* `OcrEngine` (`interface.py`) is a `typing.Protocol` with `name`, `license`,
  `is_available()` and `recognize(page, settings, region)`. Another
  permissively licensed engine can be registered in `registry.py`.
* `page` is a `uint8` array, `(H, W)` or `(H, W, 1|3|4)` (contracts section 2).
  It is converted to grayscale with the BT.601 weights and alpha is dropped.
* Boxes are in **canonical raster px** (contracts section 3). The region
  offset, rotation and scale are all undone.
* `confidence` is Tesseract's 0–100 word confidence. It is not a probability.
* Errors are `OcrError(code, message)`. They subclass `pinny.errors.PinnyError`
  once the foundation lands, and a local shim is used until then.

  | code | when |
  |---|---|
  | `ocr_disabled` | sheet info was requested while OCR is off |
  | `unknown_engine` | the name is not a registered engine |
  | `ocr_unavailable` | the engine was chosen but is not installed or does not run |
  | `invalid_config` | `settings.json` is unreadable or malformed (the message names the file) |
  | `language_unavailable` | the traineddata is not installed |
  | `invalid_settings`, `invalid_page`, `invalid_region`, `invalid_box` | bad input |
  | `page_too_large` | the area before upscaling is above `max_page_pixels` |
  | `timeout` | all passes together exceeded `max_runtime_seconds` |
  | `ocr_failed` | Tesseract exited with an error |

## Settings (`OcrSettings`)

| Setting | Default | Notes |
|---|---|---|
| `language` | `eng` | Tesseract codes joined with `+` |
| `page_segmentation_mode` | 11 (sparse text) | Suits scattered drawing labels |
| `min_confidence` | 0 | Drops words below this |
| `dpi` | 200 | DPI of the input, which is the canonical DPI |
| `upscale_to_dpi` | 300 | `None` turns it off, max 4 × `dpi` |
| `rotations` | 0, 90, 180, 270 | Any distinct subset |
| `duplicate_overlap_ratio` | 0.5 | Cross-rotation duplicate threshold |
| `max_page_pixels` | 60,000,000 | Same bound as detection |
| `max_runtime_seconds` | 300 | Total for all passes |

## CLI

```
python -m pinny.ocr engines                         # options and the current choice
python -m pinny.ocr set-engine tesseract            # turn OCR on (set-engine none turns it off)
python -m pinny.ocr sheet-info page.png [--region X,Y,W,H | --full-page]
python -m pinny.ocr text page.png [--rotations 0,90] [--no-upscale]
```

`page.png` should be a canonical 200 DPI raster. Pass `--dpi` otherwise.
Errors print `{"error": {"code", "message"}}` to stderr and exit with status 2.

## Not yet

* It is not wired into the scan flow, the viewer or the learning store.
  Sheet info has no shared contract. If other modules will read it, add a
  contracts section through the coordinator.
* It has been tested only on synthetic title blocks drawn in one OpenCV
  font. Accuracy on real drawings is unknown. Unusual label wording, logos,
  seals and dense line work will cause misses.
* The title-block search area is fixed. It isn't learned per project or
  remembered per document.
* Only one engine is available. Others must also be permissively licensed.

## Tests

```
python -m pytest tests/ocr
```

Selection, settings, geometry and parsing tests always run. The engine tests
need the Tesseract executable and `opencv-python-headless`, and are skipped
without them. They never touch your real settings, because `$PINNY_DATA_DIR`
is redirected to a temporary directory.
