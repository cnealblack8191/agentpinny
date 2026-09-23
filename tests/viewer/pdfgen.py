"""Minimal PDF writer for viewer tests (stdlib only). Pages are blank; each
has a MediaBox and optional /Rotate."""

from __future__ import annotations

from typing import Iterable, Tuple


def make_pdf(pages: Iterable[Tuple[float, float, int]] = ((612, 792, 0),), tag: str = "") -> bytes:
    pages = list(pages)
    objs = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    for w, h, rot in pages:
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Rotate {rot} >>")
    out = bytearray(b"%PDF-1.4\n%" + tag.encode() + b"\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
