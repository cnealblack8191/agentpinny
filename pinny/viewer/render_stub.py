"""STUB render service. Replace with the foundation's service (contracts §9).

**This is a stand-in, not the real render service.** It exists only so the
viewer can be built and tested before the foundation session pushes
``pinny.render`` / ``pinny.pdf``. It does not rasterise PDF content. It reads
each page's size and ``/Rotate`` from the PDF, then draws a *synthetic*
canonical raster of the right size:

* a light 100 px grid and a "STUB RENDER" banner,
* red alignment markers centred exactly on the points returned by
  :func:`stub_alignment_points` (used by the overlay-alignment check I4),
* receptacle-like symbols at the points returned by
  :func:`stub_symbol_centres`, some turned 90°, so template scans find
  something.

Every value it returns carries ``"render_service": "stub"`` so the UI can
show that the page is synthetic.

The interface matches contracts §9 and adds the upload/versioning calls the
foundation also owns (§1):

* ``register_upload(data, filename, document_id=None) -> dict``
* ``list_documents() -> list[dict]``
* ``document_info(document_version) -> dict``
* ``page_frame(document_version, page_index) -> dict``
* ``render_page(document_version, page_index) -> np.ndarray`` (uint8 RGB, H×W×3)
* ``render_page_png(document_version, page_index) -> bytes``
* ``crop_renderer(spec) -> bytes``
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import re
import threading
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .errors import ViewerError

CANONICAL_DPI = 200
RENDER_SERVICE = "stub"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
#: Largest raster the stub will draw (about 20 000 x 20 000 px at 3 B/px would be 1.2 GB).
MAX_RASTER_PIXELS = 80_000_000

SYMBOL_SIZE = 40  # px; even so the symbol centre is an integer point
MARKER_RADIUS = 6
MARKER_INSET = 30


def frame_for(width_pt: float, height_pt: float) -> Dict[str, Any]:
    """Frame descriptor (contracts §2) for a page of this upright size in points."""
    return {
        "space": "canonical_raster_px",
        "dpi": CANONICAL_DPI,
        "width": int(math.ceil(width_pt * CANONICAL_DPI / 72.0 - 1e-9)),
        "height": int(math.ceil(height_pt * CANONICAL_DPI / 72.0 - 1e-9)),
        "origin": "top-left",
        "y_axis": "down",
    }


def stub_alignment_points(width: int, height: int) -> List[Tuple[int, int]]:
    """Canonical points at which the stub draws red alignment markers:
    near the four corners and at the centre."""
    i = MARKER_INSET
    return [(i, i), (width - i, i), (i, height - i), (width - i, height - i),
            (width // 2, height // 2)]


def stub_symbol_centres(document_version: str, page_index: int, width: int,
                        height: int) -> List[Tuple[int, int, int]]:
    """``(x, y, rotation)`` of every synthetic symbol drawn on a stub page."""
    rng = random.Random(f"{document_version}#p{page_index}")
    cell = 160
    cols, rows = max(1, (width - 200) // cell), max(1, (height - 260) // cell)
    cells = [(c, r) for c in range(cols) for r in range(rows)]
    rng.shuffle(cells)
    count = min(len(cells), 14)
    out = []
    for n, (c, r) in enumerate(cells[:count]):
        x = 100 + c * cell + cell // 2
        y = 160 + r * cell + cell // 2
        out.append((x, y, 90 if n % 4 == 3 else 0))
    return out


def _symbol_patch() -> np.ndarray:
    """A 40x40 duplex-receptacle-like symbol, black on white. It is
    asymmetric (a lead on the left) so quarter turns look different."""
    import cv2

    p = np.full((SYMBOL_SIZE, SYMBOL_SIZE, 3), 255, np.uint8)
    cv2.circle(p, (22, 20), 13, (0, 0, 0), 2, lineType=cv2.LINE_8)
    cv2.line(p, (18, 13), (18, 27), (0, 0, 0), 2)
    cv2.line(p, (26, 13), (26, 27), (0, 0, 0), 2)
    cv2.line(p, (0, 20), (9, 20), (0, 0, 0), 2)
    return p


def _draw_marker(img: np.ndarray, cx: int, cy: int, r: int = MARKER_RADIUS) -> None:
    """Red disc of pixels whose centres lie within ``r`` of the continuous
    point ``(cx, cy)``; its pixel-centre centroid is exactly ``(cx, cy)``."""
    h, w = img.shape[:2]
    x0, x1 = max(0, cx - r - 1), min(w, cx + r + 1)
    y0, y1 = max(0, cy - r - 1), min(h, cy + r + 1)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    mask = (xs + 0.5 - cx) ** 2 + (ys + 0.5 - cy) ** 2 <= r * r
    img[y0:y1, x0:x1][mask] = (255, 0, 0)


def _read_pages(data: bytes) -> List[Dict[str, Any]]:
    """Upright page sizes (points) and /Rotate for every page.

    Stub-quality parsing: uses pypdf when it is installed and importable,
    otherwise scans the file (and its inflated streams) for page
    dictionaries. The real render service replaces all of this."""
    try:
        from pypdf import PdfReader  # optional; only the stub uses it
    except BaseException as exc:  # noqa: BLE001 - a broken install can panic on import
        if isinstance(exc, KeyboardInterrupt):
            raise
        return _read_pages_scan(data)
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages:
            box = page.mediabox
            rot = int(page.get("/Rotate", 0) or 0) % 360
            pages.append(_page_entry(float(box.width), float(box.height), rot))
        return pages
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad upload
        raise ViewerError("unreadable_pdf",
                          f"The file could not be read as a PDF ({exc.__class__.__name__}). "
                          "Check that it is a valid, unencrypted PDF.") from exc


_PAGE_RX = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_BOX_RX = re.compile(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]")
_ROT_RX = re.compile(rb"/Rotate\s+(-?\d+)")


def _read_pages_scan(data: bytes) -> List[Dict[str, Any]]:
    """Find page dictionaries in the raw file and in Flate streams (object
    streams). A page without its own MediaBox inherits the first one seen."""
    chunks = [data]
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            chunks.append(zlib.decompress(m.group(1)))
        except zlib.error:
            continue
    default_box = None
    for chunk in chunks:
        b = _BOX_RX.search(chunk)
        if b:
            default_box = b
            break
    pages = []
    for chunk in chunks:
        for m in _PAGE_RX.finditer(chunk):
            start = chunk.rfind(b"<<", 0, m.start())
            end = chunk.find(b">>", m.end())
            body = chunk[max(0, start):end if end > 0 else len(chunk)]
            b = _BOX_RX.search(body) or default_box
            w, h = (612.0, 792.0) if b is None else (float(b[3]) - float(b[1]),
                                                     float(b[4]) - float(b[2]))
            r = _ROT_RX.search(body)
            pages.append(_page_entry(w, h, int(r[1]) % 360 if r else 0))
    return pages


def _page_entry(w: float, h: float, rot: int) -> Dict[str, Any]:
    if rot not in (0, 90, 180, 270):
        rot = 0
    uw, uh = (h, w) if rot in (90, 270) else (w, h)
    return {"width_pt": abs(uw), "height_pt": abs(uh), "rotate": rot}


class StubRenderService:
    """STUB. Keeps uploaded PDFs and a small JSON registry under
    ``<data_dir>/stub_render``. Thread-safe."""

    render_service = RENDER_SERVICE

    def __init__(self, data_dir: os.PathLike) -> None:
        self.root = Path(data_dir) / "stub_render"
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.root / "documents.json"
        self._lock = threading.Lock()
        self._raster_cache: Dict[Tuple[str, int], np.ndarray] = {}

    # ------------------------------------------------------------ registry
    def _load(self) -> Dict[str, Any]:
        if not self._registry_path.exists():
            return {"versions": {}}
        return json.loads(self._registry_path.read_text("utf-8"))

    def _save(self, reg: Dict[str, Any]) -> None:
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self._registry_path)

    def register_upload(self, data: bytes, filename: str = "upload.pdf",
                        document_id: Optional[str] = None) -> Dict[str, Any]:
        if not data:
            raise ViewerError("empty_upload", "The uploaded file is empty. Choose a PDF file.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ViewerError("upload_too_large",
                              f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                              status=413)
        if not data.lstrip()[:5] == b"%PDF-":
            raise ViewerError("not_a_pdf", "The uploaded file is not a PDF. Choose a .pdf file.")
        pages = _read_pages(data)
        if not pages:
            raise ViewerError("no_pages", "The PDF has no pages.")
        version = "sha256:" + hashlib.sha256(data).hexdigest()
        with self._lock:
            reg = self._load()
            existing = reg["versions"].get(version)
            if existing is not None and document_id in (None, existing["document_id"]):
                return self._public(existing)
            entry = {
                "document_id": document_id or str(uuid.uuid4()),
                "document_version": version,
                "filename": os.path.basename(filename or "upload.pdf")[:200],
                "pages": pages,
                "page_count": len(pages),
            }
            (self.root / (version.split(":", 1)[1] + ".pdf")).write_bytes(data)
            reg["versions"][version] = entry
            self._save(reg)
            return self._public(entry)

    def _public(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return {"document_id": entry["document_id"],
                "document_version": entry["document_version"],
                "filename": entry["filename"], "page_count": entry["page_count"],
                "render_service": RENDER_SERVICE}

    def list_documents(self) -> List[Dict[str, Any]]:
        with self._lock:
            reg = self._load()
        return [self._public(e) for e in reg["versions"].values()]

    def _entry(self, document_version: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._load()["versions"].get(document_version)
        if entry is None:
            raise ViewerError("unknown_document",
                              "That document version is not known. Upload the PDF again.", 404)
        return entry

    def document_info(self, document_version: str) -> Dict[str, Any]:
        return self._public(self._entry(document_version))

    def _page(self, document_version: str, page_index: int) -> Dict[str, Any]:
        entry = self._entry(document_version)
        if not isinstance(page_index, int) or not 0 <= page_index < entry["page_count"]:
            raise ViewerError("unknown_page",
                              f"Page {page_index} does not exist; the document has "
                              f"{entry['page_count']} page(s), numbered from 0.", 404)
        return entry["pages"][page_index]

    # ----------------------------------------------------------- contract §9
    def page_frame(self, document_version: str, page_index: int) -> Dict[str, Any]:
        p = self._page(document_version, page_index)
        return frame_for(p["width_pt"], p["height_pt"])

    def render_page(self, document_version: str, page_index: int) -> np.ndarray:
        key = (document_version, page_index)
        with self._lock:
            cached = self._raster_cache.get(key)
        if cached is not None:
            return cached
        p = self._page(document_version, page_index)
        frame = frame_for(p["width_pt"], p["height_pt"])
        w, h = frame["width"], frame["height"]
        if w * h > MAX_RASTER_PIXELS:
            raise ViewerError("page_too_large",
                              f"Page {page_index} would render at {w}x{h} px, above the "
                              "stub's limit.", 422)
        img = self._draw(document_version, page_index, w, h, p["rotate"])
        img.setflags(write=False)
        with self._lock:
            if len(self._raster_cache) > 8:
                self._raster_cache.clear()
            self._raster_cache[key] = img
        return img

    def render_page_png(self, document_version: str, page_index: int) -> bytes:
        import cv2

        path = self.root / f"{document_version.split(':', 1)[1]}_p{page_index}.png"
        if path.exists():
            return path.read_bytes()
        img = self.render_page(document_version, page_index)
        ok, buf = cv2.imencode(".png", np.ascontiguousarray(img[:, :, ::-1]))
        if not ok:  # pragma: no cover
            raise ViewerError("render_failed", "The page image could not be encoded.", 500)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(buf.tobytes())
        os.replace(tmp, path)
        return buf.tobytes()

    def crop_renderer(self, spec: Any) -> bytes:
        """PNG of ``spec.pixel_box`` cut from the canonical raster."""
        import cv2

        page_index = int(str(spec.canonical_page_id).rsplit("#p", 1)[1])
        img = self.render_page(spec.document_version_id, page_index)
        x0, y0, x1, y1 = spec.pixel_box
        h, w = img.shape[:2]
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            raise ViewerError("empty_crop", "Crop lies outside the page raster.")
        ok, buf = cv2.imencode(".png", np.ascontiguousarray(img[y0:y1, x0:x1, ::-1]))
        return buf.tobytes()

    # --------------------------------------------------------------- drawing
    def _draw(self, version: str, page_index: int, w: int, h: int, rotate: int) -> np.ndarray:
        import cv2

        img = np.full((h, w, 3), 255, np.uint8)
        grid = (232, 232, 232)
        img[:, ::100] = grid
        img[::100, :] = grid
        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(img, "STUB RENDER SERVICE - synthetic raster, not the PDF content",
                    (60, 70), font, 1.0, (200, 0, 120), 2, cv2.LINE_AA)
        cv2.putText(img, f"page {page_index}  {w}x{h} px @200dpi  /Rotate {rotate}  {version[:19]}",
                    (60, 110), font, 0.8, (90, 90, 90), 2, cv2.LINE_AA)
        patch = _symbol_patch()
        half = SYMBOL_SIZE // 2
        for x, y, rot in stub_symbol_centres(version, page_index, w, h):
            sym = np.rot90(patch, k=-rot // 90) if rot else patch  # clockwise in y-down
            if x - half < 0 or y - half < 0 or x + half > w or y + half > h:
                continue
            img[y - half:y + half, x - half:x + half] = np.minimum(
                img[y - half:y + half, x - half:x + half], sym)
        for x, y in stub_alignment_points(w, h):
            _draw_marker(img, x, y)
        return img
