"""Upload/versioning and the render service interface (contracts v1 section 9).

    render_page(document_version, page_index) -> np.ndarray  uint8 RGB (H, W, 3)
    page_frame(document_version, page_index)  -> frame descriptor dict
    crop_renderer(spec)                        -> PNG bytes (RGB)

Storage under $PINNY_DATA_DIR (default ./pinny-data, git-ignored):

    documents/<sha256 hex>/source.pdf      immutable uploaded bytes
    documents/<sha256 hex>/version.json    DocumentVersion metadata
    documents/<sha256 hex>/pages/p<i>.png  cached canonical raster

Directory names come only from the content hash; uploaded filenames are
display metadata and never touch the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import unicodedata
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import cv2
import numpy as np

from . import pdf
from .errors import (
    DocumentVersionNotFoundError,
    InvalidCropError,
    PageNotFoundError,
    PageTooLargeError,
    PdfUnreadableError,
    UploadTooLargeError,
    VersionOwnershipConflictError,
)
from .frame import CANONICAL_DPI, canonical_page_id, frame_descriptor, parse_canonical_page_id, version_hex

DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_PAGES = 500
# 200 DPI is fixed by contract, so oversized pages are refused, never downscaled.
# 100 MP covers a 36x48 in sheet (69 MP); RGB at 100 MP is ~300 MB in memory.
DEFAULT_MAX_RASTER_PIXELS = 100_000_000
_CHUNK = 1024 * 1024


def default_data_dir() -> Path:
    return Path(os.environ.get("PINNY_DATA_DIR") or "pinny-data").resolve()


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _display_name(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in unicodedata.normalize("NFC", name) if unicodedata.category(c)[0] != "C").strip()
    return name[:255] or "upload.pdf"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def encode_png_rgb(rgb: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise PdfUnreadableError("png_encode_failed", "Could not encode the raster as PNG.")
    return buf.tobytes()


def decode_png_rgb(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise PdfUnreadableError("raster_cache_corrupt", "A cached page raster is unreadable; delete it and retry.")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


@dataclass(frozen=True)
class PageInfo:
    page_index: int
    canonical_page_id: str
    rotation: int
    width_px: int
    height_px: int


@dataclass(frozen=True)
class DocumentVersion:
    document_id: str
    document_version: str
    original_filename: str
    byte_size: int
    page_count: int
    uploaded_at: str
    renderer: str
    pages: tuple[PageInfo, ...]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pages"] = [asdict(p) for p in self.pages]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> DocumentVersion:
        return cls(**{**d, "pages": tuple(PageInfo(**p) for p in d["pages"])})


class RenderService:
    def __init__(
        self,
        data_dir: os.PathLike | str | None = None,
        *,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_raster_pixels: int = DEFAULT_MAX_RASTER_PIXELS,
        cache_size: int = 2,
    ):
        self.root = Path(data_dir).resolve() if data_dir else default_data_dir()
        self.max_upload_bytes = max_upload_bytes
        self.max_pages = max_pages
        self.max_raster_pixels = max_raster_pixels
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.RLock()
        (self.root / "documents").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    # --- upload / versioning -------------------------------------------------
    def ingest_pdf(
        self,
        source: bytes | BinaryIO | os.PathLike | str,
        *,
        original_filename: str | None = None,
        document_id: str | None = None,
    ) -> DocumentVersion:
        """Store a PDF as an immutable document version.

        Same bytes -> same document_version (idempotent). Pass the existing
        document_id to record a replacement file as a new version of that
        document; omit it and a new uuid4 document_id is assigned.
        """
        if document_id is not None:
            try:
                document_id = str(uuid.UUID(document_id))
            except ValueError as exc:
                raise PdfUnreadableError("invalid_document_id", "document_id must be a uuid4 string.") from exc
        staged = self.root / "tmp" / f"upload_{uuid.uuid4().hex}.part"
        try:
            digest, size, head = hashlib.sha256(), 0, b""
            with staged.open("wb") as out:
                for chunk in self._chunks(source):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise UploadTooLargeError(
                            "upload_too_large",
                            f"The PDF is larger than the {self.max_upload_bytes // (1024 * 1024)} MB limit.",
                        )
                    if len(head) < 1024:
                        head += chunk[: 1024 - len(head)]
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise PdfUnreadableError("empty_upload", "The upload was empty.")
            if b"%PDF-" not in head:
                raise PdfUnreadableError("pdf_unreadable", "The file is not a PDF.")
            version = "sha256:" + digest.hexdigest()
            with self._lock:
                existing = self._load_meta(version, missing_ok=True)
                if existing is not None:
                    if document_id and existing.document_id != document_id:
                        raise VersionOwnershipConflictError(
                            "version_owned_by_other_document",
                            "These exact bytes were already uploaded as a version of another document.",
                        )
                    return existing
                geometry = pdf.inspect(staged, self.max_pages)
                meta = DocumentVersion(
                    document_id=document_id or str(uuid.uuid4()),
                    document_version=version,
                    original_filename=_display_name(original_filename),
                    byte_size=size,
                    page_count=len(geometry),
                    uploaded_at=_utcnow(),
                    renderer=pdf.RENDERER_VERSION,
                    pages=tuple(
                        PageInfo(g.page_index, canonical_page_id(version, g.page_index), g.rotation, g.width_px, g.height_px)
                        for g in geometry
                    ),
                )
                vdir = self._version_dir(version)
                (vdir / "pages").mkdir(parents=True, exist_ok=True)
                os.replace(staged, vdir / "source.pdf")
                _write_atomic(vdir / "version.json", json.dumps(meta.to_dict(), indent=2).encode())
                return meta
        finally:
            staged.unlink(missing_ok=True)

    def get_version(self, document_version: str) -> DocumentVersion:
        return self._load_meta(document_version)

    def list_versions(self, document_id: str | None = None) -> list[DocumentVersion]:
        out = []
        for d in (self.root / "documents").iterdir():
            if (d / "version.json").is_file() and len(d.name) == 64:
                meta = DocumentVersion.from_dict(json.loads((d / "version.json").read_text()))
                if document_id is None or meta.document_id == document_id:
                    out.append(meta)
        return sorted(out, key=lambda m: m.uploaded_at)

    # --- contract section 9 --------------------------------------------------
    def page_frame(self, document_version: str, page_index: int) -> dict[str, Any]:
        page = self._page(document_version, page_index)
        return frame_descriptor(page.width_px, page.height_px)

    def render_page(self, document_version: str, page_index: int) -> np.ndarray:
        """Canonical raster: uint8 RGB (H, W, 3), 200 DPI, /Rotate applied. Read-only array."""
        page = self._page(document_version, page_index)
        key = page.canonical_page_id
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        if page.width_px * page.height_px > self.max_raster_pixels:
            raise PageTooLargeError(
                "page_too_large",
                f"Page {page_index + 1} would be {page.width_px}x{page.height_px} px at {CANONICAL_DPI} DPI, "
                f"over the {self.max_raster_pixels:,} pixel limit.",
            )
        png = self._version_dir(document_version) / "pages" / f"p{page_index}.png"
        if png.is_file():
            rgb = decode_png_rgb(png.read_bytes())
        else:
            rgb = pdf.render_rgb(self._version_dir(document_version) / "source.pdf", page_index, page.width_px, page.height_px)
            _write_atomic(png, encode_png_rgb(rgb))
        if rgb.shape != (page.height_px, page.width_px, 3):
            raise PdfUnreadableError("raster_size_mismatch", "Cached raster does not match the page frame; delete it and retry.")
        rgb.setflags(write=False)
        with self._lock:
            self._cache[key] = rgb
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return rgb

    def crop_renderer(self, spec: Any) -> bytes:
        """PNG (RGB) of a canonical-pixel crop (contracts v1 section 6).

        `spec` is a mapping or object with:
          document_version (or document_version_id), and
          page_index or canonical_page_id, and
          pixel_box: (x0, y0, x1, y1) half-open canonical px, or
          box: {x, y, width, height} canonical px.
        If spec has `dpi`, it must be 200. The box is clipped to the raster.
        """
        get = spec.get if isinstance(spec, Mapping) else (lambda k, d=None: getattr(spec, k, d))
        version = get("document_version") or get("document_version_id")
        page_index = get("page_index")
        if page_index is None and get("canonical_page_id"):
            pid_version, page_index = parse_canonical_page_id(get("canonical_page_id"))
            version = version or pid_version
        if version is None or page_index is None:
            raise InvalidCropError("invalid_crop_spec", "Crop spec needs document_version and page_index.")
        dpi = get("dpi")
        if dpi is not None and float(dpi) != CANONICAL_DPI:
            raise InvalidCropError("invalid_crop_spec", f"Crop spec dpi must be {CANONICAL_DPI}, got {dpi}.")
        if get("pixel_box") is not None:
            x0, y0, x1, y1 = (float(v) for v in get("pixel_box"))
        elif get("box") is not None:
            b = get("box")
            b = b if isinstance(b, Mapping) else {"x": b[0], "y": b[1], "width": b[2], "height": b[3]}
            x0, y0 = float(b["x"]), float(b["y"])
            x1, y1 = x0 + float(b["width"]), y0 + float(b["height"])
        else:
            raise InvalidCropError("invalid_crop_spec", "Crop spec needs pixel_box or box.")
        rgb = self.render_page(version, int(page_index))
        h, w = rgb.shape[:2]
        ix0, iy0 = max(0, int(np.floor(x0))), max(0, int(np.floor(y0)))
        ix1, iy1 = min(w, int(np.ceil(x1))), min(h, int(np.ceil(y1)))
        if ix1 <= ix0 or iy1 <= iy0:
            raise InvalidCropError("empty_crop", "The crop lies outside the page.")
        return encode_png_rgb(np.ascontiguousarray(rgb[iy0:iy1, ix0:ix1]))

    # --- internals -------------------------------------------------------------
    def _version_dir(self, document_version: str) -> Path:
        return self.root / "documents" / version_hex(document_version)

    def _load_meta(self, document_version: str, missing_ok: bool = False) -> DocumentVersion | None:
        path = self._version_dir(document_version) / "version.json"
        if not path.is_file():
            if missing_ok:
                return None
            raise DocumentVersionNotFoundError("document_version_not_found", f"No uploaded PDF has version {document_version}.")
        return DocumentVersion.from_dict(json.loads(path.read_text()))

    def _page(self, document_version: str, page_index: int) -> PageInfo:
        meta = self._load_meta(document_version)
        if not isinstance(page_index, int) or not 0 <= page_index < meta.page_count:
            raise PageNotFoundError(
                "page_not_found", f"Page index {page_index} is out of range (0..{meta.page_count - 1})."
            )
        return meta.pages[page_index]

    def _chunks(self, source: bytes | BinaryIO | os.PathLike | str):
        if isinstance(source, bytes | bytearray | memoryview):
            data = bytes(source)
            for i in range(0, len(data), _CHUNK):
                yield data[i : i + _CHUNK]
            return
        if isinstance(source, str | os.PathLike):
            with open(source, "rb") as fh:
                yield from iter(lambda: fh.read(_CHUNK), b"")
            return
        yield from iter(lambda: source.read(_CHUNK), b"")


_default: RenderService | None = None
_default_lock = threading.Lock()


def default_service() -> RenderService:
    global _default
    with _default_lock:
        if _default is None:
            _default = RenderService()
        return _default


def render_page(document_version: str, page_index: int) -> np.ndarray:
    return default_service().render_page(document_version, page_index)


def page_frame(document_version: str, page_index: int) -> dict[str, Any]:
    return default_service().page_frame(document_version, page_index)


def crop_renderer(spec: Any) -> bytes:
    return default_service().crop_renderer(spec)
