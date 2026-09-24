"""Errors raised by the vector matcher.

``VectorMatchError`` subclasses :class:`pinny.detection.types.DetectionError`
so callers that already handle detection errors (``code`` + message) handle
these too. When ``pinny.errors.PinnyError`` lands (contracts section 7) it
is expected to sit above ``DetectionError``.
"""

from __future__ import annotations

from pinny.detection.types import DetectionError


class VectorMatchError(DetectionError):
    """A vector-matching failure with a stable snake_case ``code``.

    Codes:

    * ``pdf_unreadable`` - the file cannot be opened or parsed as a PDF.
    * ``pdf_encrypted`` - the PDF needs a password to read its content.
    * ``page_index_out_of_range`` - ``page_index`` is not a page of the PDF.
    * ``raster_page`` - the page is a scan; use the raster matcher.
    * ``invalid_exemplar_box`` - the exemplar box is malformed or off-page.
    * ``no_vector_geometry`` - no drawing primitives lie inside the exemplar box.
    * ``invalid_settings`` - a ``VectorSettings`` value is out of range.
    * ``timeout`` - ``max_runtime_seconds`` was exceeded.
    """


class RasterPageError(VectorMatchError):
    """The page is a raster scan; the caller should fall back to raster matching."""

    def __init__(self, message: str) -> None:
        super().__init__("raster_page", message)
