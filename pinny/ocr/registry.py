"""Engine selection. OCR is **off by default**: nothing runs until the app
(or the user, via ``$PINNY_OCR_ENGINE``) picks an engine."""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from .interface import OcrEngine
from .tesseract import TesseractEngine
from .types import OcrError, OcrUnavailable

OCR_ENGINE_ENV = "PINNY_OCR_ENGINE"
NONE = "none"
DEFAULT_ENGINE = NONE

_ENGINES: Dict[str, Callable[[], OcrEngine]] = {
    TesseractEngine.name: TesseractEngine,
}

_LABELS = {NONE: "Off", TesseractEngine.name: "Tesseract"}


def engine_names() -> List[str]:
    return [NONE, *_ENGINES]


def selected_engine_name(name: Optional[str] = None) -> str:
    """The explicit ``name``, else ``$PINNY_OCR_ENGINE``, else ``"none"``."""
    chosen = (name if name is not None else os.environ.get(OCR_ENGINE_ENV, DEFAULT_ENGINE)).strip().lower()
    chosen = chosen or DEFAULT_ENGINE
    if chosen not in engine_names():
        raise OcrError(
            "unknown_engine",
            f"Unknown OCR engine {chosen!r}. Choose one of: {', '.join(engine_names())}.",
        )
    return chosen


def get_engine(name: Optional[str] = None) -> Optional[OcrEngine]:
    """The selected engine, or ``None`` when OCR is off.

    Raises ``ocr_unavailable`` if an engine was chosen but cannot run, so the
    app can tell the user instead of silently skipping OCR.
    """
    chosen = selected_engine_name(name)
    if chosen == NONE:
        return None
    engine = _ENGINES[chosen]()
    if not engine.is_available():
        raise OcrUnavailable(
            f"OCR engine {chosen!r} is selected but not installed. Install it or set "
            f"${OCR_ENGINE_ENV}=none."
        )
    return engine


def engine_options() -> List[dict]:
    """Choices for an app settings screen: ``Off`` first (the default), then
    each engine with its licence and whether it is installed."""
    options = [{"name": NONE, "label": _LABELS[NONE], "license": None, "available": True, "default": True}]
    for name, factory in _ENGINES.items():
        engine = factory()
        options.append({
            "name": name,
            "label": _LABELS.get(name, name),
            "license": engine.license,
            "available": engine.is_available(),
            "default": False,
        })
    return options
