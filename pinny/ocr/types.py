"""Engine-agnostic data types for Pinny's optional OCR.

Coordinates follow ``docs/contracts.md`` sections 2 and 3: canonical raster
pixels, origin top-left, ``x`` right, ``y`` down, boxes ``{x, y, width,
height}`` with an exclusive right/bottom edge. Engines return values in the
original page raster, never in a cropped intermediate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional, Tuple

try:  # docs/contracts.md section 7; pinny/errors.py is owned by the foundation.
    from pinny.errors import PinnyError as _ErrorBase
except ImportError:  # pragma: no cover - until the foundation lands
    class _ErrorBase(Exception):  # type: ignore[no-redef]
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code
            self.message = message


class OcrError(_ErrorBase):
    """Invalid input, a missing engine or a failed run. ``code`` is stable;
    the message says what to change."""

    def __repr__(self) -> str:
        return f"OcrError({self.code!r}, {str(self)!r})"


class OcrUnavailable(OcrError):
    """The selected engine is not installed or cannot run."""

    def __init__(self, message: str) -> None:
        super().__init__("ocr_unavailable", message)


class OcrTimeout(OcrError):
    """The run exceeded ``OcrSettings.max_runtime_seconds``."""

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


@dataclass(frozen=True)
class OcrBox:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise OcrError("invalid_box", f"OcrBox.{name} must be an int, got {value!r}.")
        if self.width <= 0 or self.height <= 0:
            raise OcrError(
                "invalid_box",
                f"OcrBox width and height must be positive, got {self.width}x{self.height}.",
            )

    @property
    def x2(self) -> int:
        return self.x + self.width

    @property
    def y2(self) -> int:
        return self.y + self.height

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class OcrSettings:
    #: Tesseract language codes joined with ``+`` (e.g. ``"eng"``, ``"eng+fra"``).
    language: str = "eng"
    #: Tesseract page segmentation mode. 11 ("sparse text") suits drawings,
    #: where labels are scattered rather than laid out in paragraphs.
    page_segmentation_mode: int = 11
    #: Words scoring below this (0-100) are dropped from the result.
    min_confidence: float = 0.0
    #: The canonical raster DPI (contracts section 2), passed to the engine.
    dpi: int = 200
    max_page_pixels: int = 60_000_000
    max_runtime_seconds: float = 120.0

    def validate(self) -> None:
        lang = self.language
        if not lang or not all(part and part.replace("_", "").isalnum() for part in lang.split("+")):
            raise OcrError(
                "invalid_settings",
                f"language must be Tesseract codes joined with '+', e.g. 'eng', got {lang!r}.",
            )
        if not (0 <= self.page_segmentation_mode <= 13):
            raise OcrError("invalid_settings", "page_segmentation_mode must be between 0 and 13.")
        if not (0.0 <= self.min_confidence <= 100.0):
            raise OcrError("invalid_settings", "min_confidence must be between 0 and 100.")
        if self.dpi <= 0 or self.max_page_pixels <= 0 or self.max_runtime_seconds <= 0:
            raise OcrError("invalid_settings", "dpi, max_page_pixels and max_runtime_seconds must be positive.")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OcrWord:
    text: str
    box: OcrBox
    #: The engine's own word confidence (0-100). Not a calibrated probability.
    confidence: float
    #: Tesseract's (block, paragraph, line) numbers, for grouping words into lines.
    line_key: Tuple[int, int, int] = (0, 0, 0)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "box": self.box.to_dict(),
            "confidence": self.confidence,
            "line_key": list(self.line_key),
        }


@dataclass(frozen=True)
class OcrResult:
    engine: str
    engine_version: str
    settings: OcrSettings
    #: Top-to-bottom, left-to-right reading order as reported by the engine.
    words: Tuple[OcrWord, ...]
    region: Optional[OcrBox] = None
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        """Words joined with spaces within a line and newlines between lines."""
        lines: list = []
        last = None
        for word in self.words:
            if word.line_key != last:
                lines.append([])
                last = word.line_key
            lines[-1].append(word.text)
        return "\n".join(" ".join(line) for line in lines)

    def to_dict(self) -> dict:
        return {
            "engine": {"name": self.engine, "version": self.engine_version, "settings": self.settings.to_dict()},
            "region": self.region.to_dict() if self.region else None,
            "words": [w.to_dict() for w in self.words],
            "warnings": list(self.warnings),
        }
