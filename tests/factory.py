"""Synthetic PDF factory for tests (foundation-owned, shared by all modules).

Generates small vector PDFs in memory. Never commit real drawings.
Coordinates passed here are PDF user space (points, origin bottom-left).
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


def receptacle_ops(cx: float, cy: float, r: float = 6.0) -> str:
    """Duplex-receptacle-like glyph: circle with two parallel vertical bars, stroked."""
    k = 0.5523 * r
    circle = (
        f"{cx + r:.3f} {cy:.3f} m "
        f"{cx + r:.3f} {cy + k:.3f} {cx + k:.3f} {cy + r:.3f} {cx:.3f} {cy + r:.3f} c "
        f"{cx - k:.3f} {cy + r:.3f} {cx - r:.3f} {cy + k:.3f} {cx - r:.3f} {cy:.3f} c "
        f"{cx - r:.3f} {cy - k:.3f} {cx - k:.3f} {cy - r:.3f} {cx:.3f} {cy - r:.3f} c "
        f"{cx + k:.3f} {cy - r:.3f} {cx + r:.3f} {cy - k:.3f} {cx + r:.3f} {cy:.3f} c S "
    )
    bars = (
        f"{cx - r * 0.35:.3f} {cy - r * 0.6:.3f} m {cx - r * 0.35:.3f} {cy + r * 0.6:.3f} l S "
        f"{cx + r * 0.35:.3f} {cy - r * 0.6:.3f} m {cx + r * 0.35:.3f} {cy + r * 0.6:.3f} l S "
    )
    return circle + bars


def square_ops(x: float, y: float, size: float) -> str:
    return f"{x:.3f} {y:.3f} {size:.3f} {size:.3f} re f "


@dataclass
class PageSpec:
    width_pt: float = 612.0
    height_pt: float = 792.0
    rotate: int = 0
    content: str = ""  # raw PDF content-stream operators
    cropbox: tuple[float, float, float, float] | None = None
    receptacles: Sequence[tuple[float, float]] = field(default_factory=list)


def build_pdf(pages: Iterable[PageSpec], password: str | None = None) -> bytes:
    writer = PdfWriter()
    for spec in pages:
        page = writer.add_blank_page(width=spec.width_pt, height=spec.height_pt)
        ops = "0 G 0 g 1 w " + spec.content + "".join(receptacle_ops(x, y) for x, y in spec.receptacles)
        stream = DecodedStreamObject()
        stream.set_data(ops.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        if spec.rotate:
            page.rotation = spec.rotate
        if spec.cropbox:
            page.cropbox.lower_left = spec.cropbox[:2]
            page.cropbox.upper_right = spec.cropbox[2:]
    if password:
        writer.encrypt(password, algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def simple_pdf(n_pages: int = 1, **kwargs) -> bytes:
    return build_pdf([PageSpec(**kwargs) for _ in range(n_pages)])
