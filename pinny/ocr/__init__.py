"""Optional OCR for Pinny page rasters. Off by default.

Depend on :class:`OcrEngine` and the types below. Use :func:`get_engine` to
honour the app's setting; it returns ``None`` while OCR is off.
:func:`read_sheet_info` reads the sheet number, title and revision.
"""

from .interface import OcrEngine
from .registry import (
    DEFAULT_ENGINE, OCR_ENGINE_ENV, engine_names, engine_options, get_engine, selected_engine_name, set_engine,
)
from .sheet_info import SheetField, SheetInfo, parse_sheet_info, read_sheet_info, title_block_region
from .tesseract import TesseractEngine
from .types import (
    ALLOWED_ROTATIONS, OcrBox, OcrError, OcrLine, OcrResult, OcrSettings, OcrTimeout, OcrUnavailable, OcrWord,
)

__all__ = [
    "ALLOWED_ROTATIONS", "DEFAULT_ENGINE", "OCR_ENGINE_ENV", "OcrBox", "OcrEngine", "OcrError", "OcrLine",
    "OcrResult", "OcrSettings", "OcrTimeout", "OcrUnavailable", "OcrWord", "SheetField", "SheetInfo",
    "TesseractEngine", "engine_names", "engine_options", "get_engine", "parse_sheet_info", "read_sheet_info",
    "selected_engine_name", "set_engine", "title_block_region",
]
