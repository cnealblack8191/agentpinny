# OCR module (`pinny/ocr/`)

Optional text recognition on canonical page rasters. **OCR is off by
default.** Nothing runs, and nothing needs to be installed, until the app or
the user picks an engine.

The first engine is [Tesseract](https://github.com/tesseract-ocr/tesseract)
(Apache-2.0). Pinny runs its command-line tool as a subprocess and does not
bundle or link it, so no Python wrapper dependency is added. Only `numpy` is
required, and detection already needs it.

## Turning it on

| Setting | Values | Default |
|---|---|---|
| `$PINNY_OCR_ENGINE` (or `get_engine(name)`) | `none`, `tesseract` | `none` |
| `$PINNY_TESSERACT_CMD` | path to the executable | `tesseract` on `PATH` |

Install Tesseract separately, for example `apt install tesseract-ocr` or
`brew install tesseract`. Other languages need their traineddata, for example
`tesseract-ocr-fra`.

An app settings screen should offer `engine_options()`. It returns `Off`
first, marked as the default, then each engine with its licence and whether
it is installed:

```python
[{"name": "none", "label": "Off", "license": None, "available": True, "default": True},
 {"name": "tesseract", "label": "Tesseract", "license": "Apache-2.0", "available": True, "default": False}]
```

## Interface

```python
from pinny.ocr import get_engine, OcrSettings, OcrBox

engine = get_engine()             # None when OCR is off; raises ocr_unavailable if chosen but missing
if engine is not None:
    result = engine.recognize(page, OcrSettings(language="eng"), region=OcrBox(x, y, w, h))
    for word in result.words:
        word.text, word.box.x, word.box.y, word.box.width, word.box.height, word.confidence
    result.text                   # words joined per line
    result.to_dict()              # JSON-serialisable
```

* `OcrEngine` (`interface.py`) is a `typing.Protocol` with `name`, `license`,
  `is_available()` and `recognize(page, settings, region)`. Callers depend on
  it, so another Apache/MIT engine can be registered in `registry.py` later.
* `page` is a `uint8` array, `(H, W)` or `(H, W, 1|3|4)`, which is the canonical
  RGB raster (contracts section 2). Alpha is dropped.
* Word boxes are in **canonical raster px** (contracts section 3). The
  `region` offset is added back, so a box never refers to a crop.
* `confidence` is Tesseract's 0–100 word confidence. It is not a calibrated
  probability.
* Errors are `OcrError(code, message)`. They subclass `pinny.errors.PinnyError`
  once the foundation lands, and a local shim with the same shape is used
  until then.

  | code | when |
  |---|---|
  | `unknown_engine` | `$PINNY_OCR_ENGINE` is not a registered engine |
  | `ocr_unavailable` | the engine was chosen but is not installed or does not run |
  | `language_unavailable` | the requested traineddata is not installed |
  | `invalid_settings`, `invalid_page`, `invalid_region`, `invalid_box` | bad input |
  | `page_too_large` | the area is above `max_page_pixels` |
  | `timeout` | the run exceeded `max_runtime_seconds` |
  | `ocr_failed` | Tesseract exited with an error |

## Settings

| Setting | Default | Notes |
|---|---|---|
| `language` | `eng` | Tesseract codes joined with `+` |
| `page_segmentation_mode` | 11 (sparse text) | Suits scattered drawing labels |
| `min_confidence` | 0 | Drops words below this |
| `dpi` | 200 | The canonical DPI, passed to Tesseract |
| `max_page_pixels` | 60,000,000 | Same bound as detection |
| `max_runtime_seconds` | 120 | Subprocess timeout |

## Not yet (this phase)

* It is not wired into the scan flow, the viewer or the learning store. OCR
  output has no shared contract yet. If other modules will read it, a
  contracts section should be added through the coordinator.
* There is no upscaling or preprocessing. Tesseract is tuned for about 300 DPI
  and the canonical raster is 200 DPI, so small drawing text may read poorly.
  This hasn't been evaluated against real drawings.
* Text is read upright only. Rotated labels on drawings are not handled.
* Only one engine is available. Others must also be permissively licensed.

## Tests

```
python -m pytest tests/ocr
```

Selection, validation and parsing tests always run. The recognition tests
need the Tesseract executable and `opencv-python-headless`, and are skipped
without them.
