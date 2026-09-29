"""Tesseract OCR engine (Apache-2.0), driven through its command-line tool.

Pinny does not bundle Tesseract. Install it separately (for example
``apt install tesseract-ocr`` or ``brew install tesseract``) and, if it is not
on ``PATH``, point ``$PINNY_TESSERACT_CMD`` at the executable. No Python
wrapper is needed: the engine is run as a subprocess with TSV output.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

from . import geometry
from .types import OcrBox, OcrError, OcrResult, OcrSettings, OcrTimeout, OcrUnavailable, OcrWord

TESSERACT_CMD_ENV = "PINNY_TESSERACT_CMD"
_WORD_LEVEL = "5"


class TesseractEngine:
    name = "tesseract"
    license = "Apache-2.0"

    def __init__(self, command: Optional[str] = None) -> None:
        self._command = command
        self._version: Optional[str] = None
        self._languages: Optional[frozenset] = None

    # -- discovery ---------------------------------------------------------

    @property
    def command(self) -> Optional[str]:
        cmd = self._command or os.environ.get(TESSERACT_CMD_ENV) or "tesseract"
        return shutil.which(cmd)

    def is_available(self) -> bool:
        try:
            self.version()
        except OcrError:
            return False
        return True

    def version(self) -> str:
        if self._version is None:
            out = self._run_info(["--version"])
            first = out.strip().splitlines()[0] if out.strip() else ""
            self._version = first.split()[-1] if first.lower().startswith("tesseract") else first or "unknown"
        return self._version

    def languages(self) -> frozenset:
        if self._languages is None:
            out = self._run_info(["--list-langs"])
            # First line is a header ("List of available languages ...").
            self._languages = frozenset(line.strip() for line in out.splitlines()[1:] if line.strip())
        return self._languages

    def _require_command(self) -> str:
        cmd = self.command
        if cmd is None:
            raise OcrUnavailable(
                "Tesseract is not installed or not on PATH. Install it (e.g. "
                f"'apt install tesseract-ocr') or set ${TESSERACT_CMD_ENV} to the executable."
            )
        return cmd

    def _run_info(self, args: Sequence[str]) -> str:
        cmd = self._require_command()
        try:
            proc = subprocess.run([cmd, *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OcrUnavailable(f"Could not run Tesseract at {cmd!r}: {exc}.") from exc
        if proc.returncode != 0:
            raise OcrUnavailable(f"Tesseract at {cmd!r} failed: {proc.stderr.strip() or proc.returncode}.")
        # Older releases print --version to stderr.
        return proc.stdout or proc.stderr

    # -- recognition -------------------------------------------------------

    def recognize(
        self,
        page: np.ndarray,
        settings: OcrSettings = OcrSettings(),
        region: Optional[OcrBox] = None,
    ) -> OcrResult:
        settings.validate()
        image = _as_rgb_or_gray(page)
        height, width = image.shape[:2]
        if region is not None:
            if not isinstance(region, OcrBox):
                raise OcrError("invalid_region", "region must be an OcrBox.")
            if region.x < 0 or region.y < 0 or region.x2 > width or region.y2 > height:
                raise OcrError(
                    "invalid_region",
                    f"region {region.to_dict()} lies outside the {width}x{height} page.",
                )
            image = image[region.y:region.y2, region.x:region.x2]
        if image.shape[0] * image.shape[1] > settings.max_page_pixels:
            raise OcrError(
                "page_too_large",
                f"OCR area is {image.shape[1]}x{image.shape[0]} px, above max_page_pixels="
                f"{settings.max_page_pixels}. Pass a smaller region.",
            )
        missing = [lang for lang in settings.language.split("+") if lang not in self.languages()]
        if missing:
            raise OcrError(
                "language_unavailable",
                f"Tesseract language data not installed: {', '.join(missing)}. Install the "
                "matching traineddata (e.g. 'apt install tesseract-ocr-<lang>').",
            )

        started = time.monotonic()
        area = (image.shape[1], image.shape[0])
        gray = _upscale(_to_gray(image), settings.scale)
        scaled = (gray.shape[1], gray.shape[0])
        dx, dy = (region.x, region.y) if region else (0, 0)
        by_rotation = {}
        warnings: List[str] = []
        for rotation in settings.rotations:
            remaining = settings.max_runtime_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise _timeout(settings)
            tsv = self._run_tsv(geometry.rotate_image(gray, rotation), settings, remaining)
            raw, row_warnings = parse_tsv(tsv, min_confidence=settings.min_confidence)
            warnings.extend(f"rotation {rotation}: {w}" for w in row_warnings)
            mapped = []
            for word in raw:
                box = geometry.unscale_box(geometry.unrotate_box(word.box, scaled, rotation), settings.scale, area)
                mapped.append(replace(word, box=OcrBox(box.x + dx, box.y + dy, box.width, box.height), rotation=rotation))
            by_rotation[rotation] = mapped
        words = geometry.merge_rotations(by_rotation, tuple(settings.rotations), settings.duplicate_overlap_ratio)
        return OcrResult(
            engine=self.name,
            engine_version=self.version(),
            settings=settings,
            words=tuple(words),
            region=region,
            warnings=tuple(warnings),
        )

    def _run_tsv(self, image: np.ndarray, settings: OcrSettings, timeout: float) -> str:
        cmd = self._require_command()
        with tempfile.TemporaryDirectory(prefix="pinny-ocr-") as tmp:
            src = Path(tmp) / "page.pnm"
            src.write_bytes(_encode_pnm(image))
            args = [
                cmd, str(src), "stdout",
                "-l", settings.language,
                "--psm", str(settings.page_segmentation_mode),
                "--dpi", str(settings.upscale_to_dpi or settings.dpi),
                "tsv",
            ]
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                    env={**os.environ, "OMP_THREAD_LIMIT": os.environ.get("OMP_THREAD_LIMIT", "1")},
                )
            except subprocess.TimeoutExpired as exc:
                raise _timeout(settings) from exc
            except OSError as exc:
                raise OcrUnavailable(f"Could not run Tesseract at {cmd!r}: {exc}.") from exc
        if proc.returncode != 0:
            raise OcrError("ocr_failed", f"Tesseract failed: {proc.stderr.strip() or proc.returncode}.")
        return proc.stdout


def parse_tsv(tsv: str, dx: int = 0, dy: int = 0, min_confidence: float = 0.0) -> Tuple[List[OcrWord], List[str]]:
    """Parse Tesseract TSV into words, shifting boxes by ``(dx, dy)``."""
    words: List[OcrWord] = []
    warnings: List[str] = []
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        if row.get("level") != _WORD_LEVEL:
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
            w, h = int(row["width"]), int(row["height"])
            line_key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        except (KeyError, TypeError, ValueError):
            warnings.append(f"Skipped an unparseable Tesseract row: {row!r}.")
            continue
        if w <= 0 or h <= 0 or conf < min_confidence:
            continue
        words.append(OcrWord(text=text, box=OcrBox(left + dx, top + dy, w, h), confidence=conf, line_key=line_key))
    return words, warnings


def _timeout(settings: OcrSettings) -> OcrTimeout:
    return OcrTimeout(
        f"OCR took longer than {settings.max_runtime_seconds} s for {len(settings.rotations)} "
        "rotation(s). Pass a smaller region, fewer rotations or raise max_runtime_seconds."
    )


def _to_gray(image: np.ndarray) -> np.ndarray:
    """ITU-R BT.601 luma, the same weights as OpenCV's RGB->GRAY."""
    if image.ndim == 2:
        return image
    rgb = image.astype(np.float32)
    gray = rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114
    return np.clip(np.rint(gray), 0, 255).astype(np.uint8)


def _upscale(gray: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return gray
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - cv2 is a declared dependency
        raise OcrUnavailable(
            "Upscaling needs opencv-python-headless. Install it or set upscale_to_dpi=None."
        ) from exc
    h, w = gray.shape
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return cv2.resize(gray, size, interpolation=cv2.INTER_CUBIC)


def _as_rgb_or_gray(page: np.ndarray) -> np.ndarray:
    if not isinstance(page, np.ndarray) or page.dtype != np.uint8:
        raise OcrError("invalid_page", "Page raster must be a uint8 numpy array (contracts section 2).")
    if page.ndim == 2:
        image = page
    elif page.ndim == 3 and page.shape[2] in (1, 3, 4):
        image = page[:, :, 0] if page.shape[2] == 1 else page[:, :, :3]
    else:
        raise OcrError("invalid_page", f"Page raster must be (H, W) or (H, W, 1|3|4), got {page.shape}.")
    if image.shape[0] == 0 or image.shape[1] == 0:
        raise OcrError("invalid_page", "Page raster is empty.")
    return np.ascontiguousarray(image)


def _encode_pnm(image: np.ndarray) -> bytes:
    """Binary PGM (gray) or PPM (RGB); read natively by Tesseract's Leptonica."""
    h, w = image.shape[:2]
    magic = b"P5" if image.ndim == 2 else b"P6"
    return magic + f"\n{w} {h}\n255\n".encode("ascii") + image.tobytes()
