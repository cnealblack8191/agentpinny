# Render service (foundation)

Implements contracts v1 sections 1, 2, 6, 7 and 9. Code: `pinny/render/`,
`pinny/errors.py`. Tests: `tests/render/`. Shared test helpers:
`tests/factory.py` (synthetic PDFs) and `tests/conftest.py`.

## Interface

```python
from pinny.render import RenderService, render_page, page_frame, crop_renderer

svc = RenderService(data_dir)            # or the module-level functions ($PINNY_DATA_DIR, default ./pinny-data)
v = svc.ingest_pdf(pdf_bytes_or_path_or_file, original_filename="E-101.pdf", document_id=None)
v.document_id          # uuid4; pass it again to add a replacement file as a new version
v.document_version     # "sha256:<hex of file bytes>"
v.pages[i]             # PageInfo(page_index, canonical_page_id, rotation, width_px, height_px)

svc.render_page(v.document_version, 0)  # np.ndarray uint8 RGB (H, W, 3), read-only
svc.page_frame(v.document_version, 0)    # {"space": "canonical_raster_px", "dpi": 200, "width", "height", "origin": "top-left", "y_axis": "down"}
svc.crop_renderer(spec)                  # PNG bytes (RGB)
```

`crop_renderer(spec)` accepts a mapping or an object with:

* `document_version` (or `document_version_id`), plus `page_index` or `canonical_page_id`
* `pixel_box` = `(x0, y0, x1, y1)`, half-open canonical px, **or** `box` = `{x, y, width, height}` in canonical px
* optional `dpi`, which must be 200

Float edges are expanded outward (floor/ceil). The box is clipped to the raster.
A crop that is entirely outside the page raises `empty_crop`. The learning
store's `CropSpec` v2 can be passed directly once `pixel_box` is in canonical px.

## Canonical raster

* PDFium renders the CropBox with `/Rotate` applied (PDFium applies it
  itself, so the render call passes `rotation=0`). White background,
  annotations and form fields drawn, RGB byte order.
* Size = `ceil(pt * 200 / 72)` per contract. PDFium's own rounding can give an
  extra row or column. The service trims it, or pads with white, so the shape
  always matches the frame exactly.
* 200 DPI is fixed, so pages are never downscaled. A page over
  `max_raster_pixels` (default 100 MP) raises `page_too_large`.
* Rasters are cached as lossless PNG at `documents/<hex>/pages/p<i>.png`, and
  up to 2 rasters stay in memory.

## Storage and limits

```
$PINNY_DATA_DIR/documents/<sha256 hex>/source.pdf     immutable bytes
$PINNY_DATA_DIR/documents/<sha256 hex>/version.json   metadata
$PINNY_DATA_DIR/documents/<sha256 hex>/pages/p<i>.png raster cache
$PINNY_DATA_DIR/tmp/                                  upload staging (random names)
```

Directory names come only from the content hash. The uploaded filename is
display metadata and is sanitized. Uploads are streamed with a size cap
(default 200 MB) and checked for `%PDF-`. Documents are limited to 500 pages.

## Error codes (all `PinnyError` subclasses)

| code | http_status | when |
|---|---|---|
| `empty_upload` | 422 | zero bytes |
| `pdf_unreadable` | 422 | not a PDF / corrupt |
| `pdf_encrypted` | 422 | password-protected |
| `pdf_too_many_pages` | 422 | over the page limit |
| `upload_too_large` | 413 | over the byte limit |
| `page_too_large` | 422 | raster over the pixel limit |
| `invalid_document_version`, `invalid_page_id`, `invalid_document_id` | 400 | malformed ids |
| `document_version_not_found`, `page_not_found` | 404 | unknown version/page |
| `version_owned_by_other_document` | 409 | same bytes uploaded under another document_id |
| `invalid_crop_spec`, `empty_crop` | 422 | bad crop spec |

## Windows setup and verification

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m pytest tests\render
```

To update the pinned dependencies (foundation only): edit `pyproject.toml`
(the single dependency list), then run
`uv pip compile --universal --python-version 3.12 --generate-hashes pyproject.toml --extra dev -o requirements.lock.txt`.
