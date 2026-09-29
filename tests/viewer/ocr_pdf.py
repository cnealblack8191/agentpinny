"""An Arch D sheet (36 x 24 in) with a title block along the right edge, as
real text (Helvetica), for the OCR sheet-info checks (docs/ocr.md)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tests.legend.pdfgen as pdfgen  # noqa: E402
from tests.legend.pdfgen import Canvas, build  # noqa: E402

W, H = 2592.0, 1728.0  # points
EXPECTED = {"sheet_number": "E-101", "sheet_title": "FIRST FLOOR POWER PLAN", "revision": "2"}


def title_block_sheet(path: str) -> None:
    c = Canvas(W, H)
    c.rect(36, 36, W - 72, H - 72)
    c.rect(2290, 990, 266, 702)  # title block, bottom right
    for y in (1080, 1380, 1480, 1580):
        c.line(2290, y, 2556, y)
    c.text(2300, 1010, "REV", 6); c.text(2330, 1010, "DATE", 6); c.text(2390, 1010, "DESCRIPTION", 6)
    c.text(2303, 1030, "1", 6); c.text(2330, 1030, "08/01/26", 6); c.text(2390, 1030, "PERMIT SET", 6)
    c.text(2303, 1050, "2", 6); c.text(2330, 1050, "09/15/26", 6); c.text(2390, 1050, "BID SET", 6)
    c.text(2300, 1398, "SHEET TITLE:", 6)
    c.text(2300, 1425, "FIRST FLOOR", 14); c.text(2300, 1445, "POWER PLAN", 14)
    c.text(2300, 1500, "SCALE: 1/8\" = 1'-0\"", 6)
    c.text(2300, 1600, "SHEET NUMBER", 6)
    c.text(2300, 1660, "E-101", 36)
    # Some plan content, away from the title block.
    c.text(300, 300, "FIRST FLOOR POWER PLAN", 18)
    for x in range(400, 2000, 200):
        c.line(x, 400, x, 1400)
    size = pdfgen.PAGE
    pdfgen.PAGE = (W, H)  # build() sizes pages from this module constant
    try:
        build([(c, 0, None)], path)
    finally:
        pdfgen.PAGE = size


if __name__ == "__main__":
    title_block_sheet(sys.argv[1])
