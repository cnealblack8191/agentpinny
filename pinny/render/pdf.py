"""PDFium access. PDF points exist only inside this package (contracts v1 section 2)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.version as pdfium_version

from .errors import PdfEncryptedError, PdfTooManyPagesError, PdfUnreadableError, PageNotFoundError
from .frame import CANONICAL_DPI, canonical_size

RENDERER_VERSION = f"pypdfium2 {pdfium_version.PYPDFIUM_INFO} / pdfium {pdfium_version.PDFIUM_INFO}"

# PDFium is not thread-safe.
_LOCK = threading.Lock()
_ERR_PASSWORD = 4


@dataclass(frozen=True)
class PageGeometry:
    page_index: int
    rotation: int  # normalized /Rotate, clockwise: 0/90/180/270
    width_pt: float  # displayed (after /Rotate), CropBox
    height_pt: float
    width_px: int  # canonical raster size, ceil(pt * 200 / 72)
    height_px: int


def _open(source: Path | bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == _ERR_PASSWORD:
            raise PdfEncryptedError(
                "pdf_encrypted", "This PDF is password-protected. Remove the password and upload it again."
            ) from exc
        raise PdfUnreadableError(
            "pdf_unreadable", "The file could not be read as a PDF. Re-export it from the source application."
        ) from exc


def inspect(source: Path | bytes, max_pages: int) -> list[PageGeometry]:
    with _LOCK:
        doc = _open(source)
        try:
            count = len(doc)
            if count < 1:
                raise PdfUnreadableError("pdf_unreadable", "The PDF has no pages.")
            if count > max_pages:
                raise PdfTooManyPagesError(
                    "pdf_too_many_pages", f"The PDF has {count} pages; the limit is {max_pages}. Split it and retry."
                )
            out = []
            for i in range(count):
                page = doc[i]
                try:
                    w_pt, h_pt = page.get_size()  # PDFium reports the rotated (displayed) size
                    w_px, h_px = canonical_size(w_pt, h_pt)
                    out.append(PageGeometry(i, page.get_rotation() % 360, w_pt, h_pt, w_px, h_px))
                finally:
                    page.close()
            return out
        finally:
            doc.close()


def render_rgb(source: Path | bytes, page_index: int, width_px: int, height_px: int) -> np.ndarray:
    """Render one page as uint8 RGB (height_px, width_px, 3), /Rotate applied, white background.

    PDFium rounds the bitmap size itself; the result is trimmed or white-padded
    by at most a pixel so the shape is exactly the contract size.
    """
    with _LOCK:
        doc = _open(source)
        try:
            if not 0 <= page_index < len(doc):
                raise PageNotFoundError("page_not_found", f"Page index {page_index} does not exist.")
            page = doc[page_index]
            try:
                bitmap = page.render(
                    scale=CANONICAL_DPI / 72,
                    rotation=0,  # PDFium already applies the page's own /Rotate
                    fill_color=(255, 255, 255, 255),
                    rev_byteorder=True,  # RGB rather than BGR
                    draw_annots=True,
                    may_draw_forms=True,
                )
                arr = np.array(bitmap.to_numpy(), dtype=np.uint8, copy=True)
                bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    arr = arr[:, :, :3]
    out = np.full((height_px, width_px, 3), 255, dtype=np.uint8)
    h, w = min(height_px, arr.shape[0]), min(width_px, arr.shape[1])
    out[:h, :w] = arr[:h, :w]
    return out
