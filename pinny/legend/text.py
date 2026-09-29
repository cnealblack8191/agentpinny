"""Text runs with positions, in canonical raster pixels.

PDFium groups a page's characters into rectangles, roughly one per run of
text on a line. We read each rectangle's text and map its corners from PDF
user space to canonical pixels with the same :class:`PageFrame` the vector
matcher uses (CropBox origin, ``/Rotate``, 200 DPI), so text and drawing
geometry share one coordinate system.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

from pinny.vector.frame import PageFrame

from .errors import LegendError

Box = Tuple[float, float, float, float]  # x0, y0, x1, y1 in canonical px


@dataclass(frozen=True)
class TextLine:
    text: str
    box: Box

    @property
    def x0(self) -> float:
        return self.box[0]

    @property
    def y0(self) -> float:
        return self.box[1]

    @property
    def x1(self) -> float:
        return self.box[2]

    @property
    def y1(self) -> float:
        return self.box[3]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2.0


def _to_px_box(frame: PageFrame, left: float, bottom: float, right: float, top: float) -> Box:
    pts = frame.to_px(np.array([[left, bottom], [right, bottom], [right, top], [left, top]], dtype=float))
    return (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))


def open_document(pdf_path: "str | os.PathLike[str]"):
    """Open a PDF with PDFium (close it when done)."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise LegendError(
            "pdfium_missing", "Reading legends needs the 'pypdfium2' package: pip install pypdfium2."
        ) from exc
    try:
        return pdfium.PdfDocument(os.fspath(pdf_path))
    except pdfium.PdfiumError as exc:
        raise LegendError("unreadable_pdf", f"PDFium could not open the PDF: {exc}.") from exc


def lines_from_document(doc, page_index: int, frame: PageFrame) -> List[TextLine]:
    """Text runs on one page of an open PDFium document."""
    if not 0 <= page_index < len(doc):
        raise LegendError("page_index_out_of_range", f"Page {page_index} is not in this {len(doc)}-page PDF.")
    page = doc[page_index]
    textpage = page.get_textpage()
    lines: List[TextLine] = []
    try:
        for i in range(textpage.count_rects()):
            left, bottom, right, top = textpage.get_rect(i)
            text = " ".join(textpage.get_text_bounded(left, bottom, right, top).split())
            if text:
                lines.append(TextLine(text, _to_px_box(frame, left, bottom, right, top)))
    finally:
        textpage.close()
        page.close()
    lines.sort(key=lambda t: (round(t.cy), t.x0))
    return lines


def extract_text_lines(pdf_path: "str | os.PathLike[str]", page_index: int, frame: PageFrame) -> List[TextLine]:
    """Text runs on one page, top to bottom then left to right."""
    doc = open_document(pdf_path)
    try:
        return lines_from_document(doc, page_index, frame)
    finally:
        doc.close()
