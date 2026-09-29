"""Engine selection. OCR is **off by default**: nothing runs until the user
picks an engine in the app settings (or overrides it with ``$PINNY_OCR_ENGINE``).

Precedence: an explicit ``name`` argument, then ``$PINNY_OCR_ENGINE``, then the
app setting in ``$PINNY_DATA_DIR/settings.json``, then ``"none"``.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from . import config
from .config import PathLike
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


def _normalize(name: str) -> str:
    chosen = name.strip().lower() or DEFAULT_ENGINE
    if chosen not in engine_names():
        raise OcrError(
            "unknown_engine",
            f"Unknown OCR engine {chosen!r}. Choose one of: {', '.join(engine_names())}.",
        )
    return chosen


def selected_engine_name(name: Optional[str] = None, data_dir: Optional[PathLike] = None) -> str:
    if name is not None:
        return _normalize(name)
    env = os.environ.get(OCR_ENGINE_ENV, "")
    if env.strip():
        return _normalize(env)
    stored = config.stored_engine(data_dir)
    return _normalize(stored) if stored is not None else DEFAULT_ENGINE


def set_engine(name: str, data_dir: Optional[PathLike] = None) -> str:
    """Save the app setting (what a settings screen calls). Returns the saved name.

    ``$PINNY_OCR_ENGINE``, if set, still overrides it at run time.
    """
    chosen = _normalize(name)
    config.store_engine(chosen, data_dir)
    return chosen


def get_engine(name: Optional[str] = None, data_dir: Optional[PathLike] = None) -> Optional[OcrEngine]:
    """The selected engine, or ``None`` when OCR is off.

    Raises ``ocr_unavailable`` if an engine was chosen but cannot run, so the
    app can tell the user instead of silently skipping OCR.
    """
    chosen = selected_engine_name(name, data_dir)
    if chosen == NONE:
        return None
    engine = _ENGINES[chosen]()
    if not engine.is_available():
        raise OcrUnavailable(
            f"OCR engine {chosen!r} is selected but not installed. Install it, or turn "
            "OCR off in the app settings."
        )
    return engine


def engine_options(data_dir: Optional[PathLike] = None) -> List[dict]:
    """Choices for an app settings screen: ``Off`` first (the default), then
    each engine with its licence and whether it is installed. ``selected``
    marks the effective choice; ``overridden`` is true when
    ``$PINNY_OCR_ENGINE`` decides it rather than the saved setting."""
    selected = selected_engine_name(data_dir=data_dir)
    overridden = bool(os.environ.get(OCR_ENGINE_ENV, "").strip())
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
    for option in options:
        option["selected"] = option["name"] == selected
        option["overridden"] = overridden
    return options
