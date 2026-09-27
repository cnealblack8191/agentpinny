"""Canonical page rasters (contracts §2) rendered with PDFium.

The foundation's render service will own this; until it lands, this is the
one place that turns a PDF page into the 200 DPI RGB raster the raster
matcher works on. PDFium applies ``/Rotate`` and the CropBox, matching the
vector module's page frame.
"""

from __future__ import annotations

import math
import os

import numpy as np

from pinny.legend.text import open_document

CANONICAL_DPI = 200
RENDERER_VERSION = "pdfium-render-v1"


def render_page(pdf_path: "str | os.PathLike[str]", page_index: int, doc=None) -> np.ndarray:
    """``(H, W, 3)`` uint8 RGB raster of one page at 200 DPI."""
    own = doc is None
    doc = doc or open_document(pdf_path)
    try:
        page = doc[page_index]
        try:
            w_pt, h_pt = page.get_size()
            bitmap = page.render(scale=CANONICAL_DPI / 72.0, rev_byteorder=True)
            img = np.asarray(bitmap.to_numpy())
        finally:
            page.close()
    finally:
        if own:
            doc.close()
    if img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, :3]
    elif img.ndim == 2:
        img = np.repeat(img[:, :, None], 3, axis=2)
    # Exact canonical size: ceil(pt * 200 / 72) on each side.
    W, H = math.ceil(w_pt * CANONICAL_DPI / 72.0 - 1e-6), math.ceil(h_pt * CANONICAL_DPI / 72.0 - 1e-6)
    out = np.full((H, W, 3), 255, dtype=np.uint8)
    h, w = min(H, img.shape[0]), min(W, img.shape[1])
    out[:h, :w] = img[:h, :w]
    return out
