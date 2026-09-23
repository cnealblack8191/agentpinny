"""Identity and coordinate-frame rules (contracts v1 sections 1-2)."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from pinny.errors import PinnyError

CANONICAL_DPI = 200
FRAME_SPACE = "canonical_raster_px"

_VERSION_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_PAGE_ID_RE = re.compile(r"^(sha256:[0-9a-f]{64})#p(\d+)$")


def document_version_for_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def version_hex(document_version: str) -> str:
    """Validate a document_version and return its 64-char hex digest."""
    m = _VERSION_RE.fullmatch(document_version or "")
    if not m:
        raise PinnyError("invalid_document_version", f"Expected 'sha256:<64 hex>', got {document_version!r}.")
    return m.group(1)


def canonical_page_id(document_version: str, page_index: int) -> str:
    version_hex(document_version)
    if page_index < 0:
        raise PinnyError("invalid_page_index", "page_index is 0-based and must be >= 0.")
    return f"{document_version}#p{page_index}"


def parse_canonical_page_id(page_id: str) -> tuple[str, int]:
    m = _PAGE_ID_RE.fullmatch(page_id or "")
    if not m:
        raise PinnyError("invalid_page_id", f"Expected 'sha256:<hex>#p<index>', got {page_id!r}.")
    return m.group(1), int(m.group(2))


def canonical_size(width_pt: float, height_pt: float) -> tuple[int, int]:
    """(width_px, height_px) = ceil(pt * 200 / 72), pt measured after /Rotate."""
    return math.ceil(width_pt * CANONICAL_DPI / 72), math.ceil(height_pt * CANONICAL_DPI / 72)


def frame_descriptor(width_px: int, height_px: int) -> dict[str, Any]:
    return {
        "space": FRAME_SPACE,
        "dpi": CANONICAL_DPI,
        "width": int(width_px),
        "height": int(height_px),
        "origin": "top-left",
        "y_axis": "down",
    }
