"""Optional OCR for Pinny page rasters. Off by default.

Depend on :class:`OcrEngine` and the types below. Use :func:`get_engine` to
honour the app's choice; it returns ``None`` when OCR is off.
"""

from .interface import OcrEngine
from .registry import DEFAULT_ENGINE, OCR_ENGINE_ENV, engine_names, engine_options, get_engine, selected_engine_name
from .tesseract import TesseractEngine
from .types import OcrBox, OcrError, OcrResult, OcrSettings, OcrTimeout, OcrUnavailable, OcrWord

__all__ = [
    "DEFAULT_ENGINE", "OCR_ENGINE_ENV", "OcrBox", "OcrEngine", "OcrError", "OcrResult", "OcrSettings",
    "OcrTimeout", "OcrUnavailable", "OcrWord", "TesseractEngine", "engine_names", "engine_options",
    "get_engine", "selected_engine_name",
]
