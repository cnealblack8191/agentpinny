"""Synthetic drawing sets with a symbol legend, written with pikepdf.

Layout is given top-down in points (y grows down) and converted to PDF user
space. Text uses Helvetica, so PDFium extracts it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pikepdf

from pinny.vector.frame import frame_from_boxes

K = 0.5522847498
PAGE = (1224.0, 792.0)  # ANSI B landscape, points


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Canvas:
    def __init__(self, width: float = PAGE[0], height: float = PAGE[1]) -> None:
        self.w, self.h = width, height
        self.ops: List[str] = []

    def Y(self, y: float) -> float:
        return self.h - y

    def text(self, x: float, y: float, s: str, size: float = 8.0) -> None:
        self.ops.append(f"BT /F1 {size} Tf {x:.2f} {self.Y(y):.2f} Td ({_esc(s)}) Tj ET\n")

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.6) -> None:
        self.ops.append(f"{width} w {x0:.2f} {self.Y(y0):.2f} m {x1:.2f} {self.Y(y1):.2f} l S\n")

    def rect(self, x: float, y: float, w: float, h: float, fill: bool = False) -> None:
        self.ops.append(f"0.6 w {x:.2f} {self.Y(y + h):.2f} {w:.2f} {h:.2f} re {'f' if fill else 'S'}\n")

    def circle(self, cx: float, cy: float, r: float) -> None:
        cy = self.Y(cy)
        k = K * r
        self.ops.append(
            f"0.6 w {cx + r:.2f} {cy:.2f} m {cx + r:.2f} {cy + k:.2f} {cx + k:.2f} {cy + r:.2f} {cx:.2f} {cy + r:.2f} c "
            f"{cx - k:.2f} {cy + r:.2f} {cx - r:.2f} {cy + k:.2f} {cx - r:.2f} {cy:.2f} c "
            f"{cx - r:.2f} {cy - k:.2f} {cx - k:.2f} {cy - r:.2f} {cx:.2f} {cy - r:.2f} c "
            f"{cx + k:.2f} {cy - r:.2f} {cx + r:.2f} {cy - k:.2f} {cx + r:.2f} {cy:.2f} c S\n"
        )

    def symbol(self, kind: str, cx: float, cy: float) -> None:
        """Symbols about 12–16 pt across, centred on (cx, cy) top-down."""
        if kind in ("duplex", "gfci", "wp", "dedicated", "usb"):
            self.line(cx, cy - 6, cx, cy - 11)
            self.circle(cx, cy, 6)
            self.line(cx - 2, cy - 3, cx - 2, cy + 3)
            self.line(cx + 2, cy - 3, cx + 2, cy + 3)
            if kind == "dedicated":
                self.ops.append(f"{cx + 8:.2f} {self.Y(cy + 3):.2f} m {cx + 13:.2f} {self.Y(cy + 3):.2f} l {cx + 10.5:.2f} {self.Y(cy - 2):.2f} l h f\n")
        elif kind == "quad":
            self.line(cx, cy - 7, cx, cy - 12)
            self.circle(cx, cy, 7)
            for dx in (-3.5, -1.2, 1.2, 3.5):
                self.line(cx + dx, cy - 3, cx + dx, cy + 3)
        elif kind == "floor":
            self.rect(cx - 7, cy - 7, 14, 14, fill=True)
        elif kind == "disc":
            self.rect(cx - 6, cy - 6, 12, 12)
            self.line(cx - 6, cy + 6, cx + 6, cy - 6)
        elif kind == "tstat":
            self.circle(cx, cy, 5)
            self.line(cx - 2, cy - 2, cx + 2, cy - 2)
            self.line(cx, cy - 2, cx, cy + 3)
        elif kind == "homerun":
            self.line(cx - 16, cy + 3, cx + 12, cy - 2)
            self.ops.append(f"{cx + 12:.2f} {self.Y(cy - 2):.2f} m {cx + 6:.2f} {self.Y(cy - 5):.2f} l {cx + 7:.2f} {self.Y(cy + 1):.2f} l h f\n")
            for dx in (-8, -4):
                self.line(cx + dx - 1, cy - 1, cx + dx + 1, cy + 3)
        elif kind == "jbox":
            self.circle(cx, cy, 6)
            self.line(cx - 1, cy - 3, cx + 1, cy - 3)
            self.line(cx, cy - 3, cx, cy + 3)
        else:
            raise ValueError(kind)


@dataclass
class Row:
    syms: Sequence[str]
    text: Sequence[str]
    tag: Optional[str] = None
    label: Optional[str] = None
    section: Optional[str] = None  # a section heading drawn above this row


@dataclass
class Layout:
    x: float = 40.0          # legend frame left
    y: float = 40.0          # legend frame top
    sym_x: float = 75.0      # symbol centre (single)
    pair_dx: float = 18.0    # half spacing for two symbols in one row
    tag_x: Optional[float] = None
    desc_x: float = 150.0
    width: float = 520.0
    rules: bool = True
    headers: bool = True
    heading: str = "ELECTRICAL SYMBOLS LEGEND"


def draw_legend(c: Canvas, rows: Sequence[Row], lay: Layout, notes: bool = True) -> Dict[str, List[Tuple[float, float]]]:
    """Draw a legend; return the top-down pt centre of every symbol drawn,
    keyed by the row's first description line."""
    centres: Dict[str, List[Tuple[float, float]]] = {}
    c.text(lay.x + 10, lay.y + 18, lay.heading, size=12)
    y = lay.y + 34
    if lay.headers:
        c.text(lay.sym_x - 20, y, "SYMBOL", size=7)
        if lay.tag_x is not None:
            c.text(lay.tag_x - 2, y, "TAG", size=7)
        c.text(lay.desc_x, y, "DESCRIPTION", size=7)
        y += 5
    if lay.rules:
        c.line(lay.x + 5, y, lay.x + lay.width - 5, y, 0.4)
    for r in rows:
        if r.section:
            y += 4
            c.text(lay.desc_x, y + 10, r.section, size=8)
            y += 18
        h = 22 + 10 * (len(r.text) - 1)
        cy = y + h / 2
        pts = []
        if len(r.syms) == 1:
            pts = [(lay.sym_x, cy)]
        else:
            pts = [(lay.sym_x - lay.pair_dx, cy), (lay.sym_x + lay.pair_dx, cy)]
        for kind, (sx, sy) in zip(r.syms, pts):
            c.symbol(kind, sx, sy)
        if r.label:
            c.text(pts[0][0] + 8, cy + 9, r.label, size=5)
        if r.tag and lay.tag_x is not None:
            c.text(lay.tag_x, y + 14, r.tag, size=8)
        for i, t in enumerate(r.text):
            c.text(lay.desc_x, y + 14 + 10 * i, t, size=8)
        centres[r.text[0]] = pts
        y += h
        if lay.rules:
            c.line(lay.x + 5, y, lay.x + lay.width - 5, y, 0.4)
    c.rect(lay.x, lay.y, lay.width, y - lay.y + 8)
    if notes:
        c.text(lay.x + 10, y + 40, "GENERAL NOTES", size=10)
        c.text(lay.x + 10, y + 54, "1. ALL RECEPTACLES TAMPER RESISTANT.", size=8)
    return centres


def floor_plan(c: Canvas) -> None:
    c.rect(100, 100, 800, 500)
    c.line(500, 100, 500, 600)
    c.text(300, 350, "OFFICE 101", size=10)
    c.text(650, 350, "OFFICE 102", size=10)
    for x in (130, 300, 470, 700):
        c.symbol("duplex", x, 118)
    c.text(100, 760, "E-101 FIRST FLOOR POWER PLAN", size=10)


def build(pages: Sequence[Tuple[Canvas, int, Optional[Tuple[float, float, float, float]]]], path: str) -> None:
    """Write pages ``(canvas, rotate, cropbox)``."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    for c, rotate, crop in pages:
        media = PAGE if rotate in (0, 180) or crop else (c.h, c.w)
        page = pdf.add_blank_page(page_size=media)
        if crop:
            page.obj.CropBox = pikepdf.Array(list(crop))
        if rotate:
            page.obj.Rotate = rotate
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        prefix = ""
        if rotate or crop:
            # Draw so the sheet displays upright, as CAD exports do: the
            # canvas is the displayed page, mapped into user space.
            frame = frame_from_boxes((0, 0, media[0], media[1]), crop, rotate)
            s = 200.0 / 72.0
            pts = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])  # canvas y-up
            px = np.stack([pts[:, 0] * s, (c.h - pts[:, 1]) * s], 1)
            u = frame.to_user(px)
            a_, b_ = u[1] - u[0], u[2] - u[0]
            prefix = f"{a_[0]:.6f} {a_[1]:.6f} {b_[0]:.6f} {b_[1]:.6f} {u[0][0]:.4f} {u[0][1]:.4f} cm\n"
        page.obj.Contents = pdf.make_stream((prefix + "".join(c.ops)).encode("latin-1"))
    pdf.save(path)


MAIN_ROWS = [
    Row(["duplex"], ['DUPLEX RECEPTACLE, 20A, 125V, +18" AFF U.O.N.']),
    Row(["gfci"], ["GFCI DUPLEX RECEPTACLE, 20A, 125V"], label="GFI"),
    Row(["wp"], ["WEATHERPROOF GFCI RECEPTACLE, IN-USE COVER"], label="WP"),
    Row(["quad", "usb"], ["QUAD RECEPTACLE (LEFT) / USB CHARGING", "RECEPTACLE, TYPE A/C (RIGHT)"]),
    Row(["dedicated"], ["DEDICATED RECEPTACLE, ISOLATED CIRCUIT"]),
    Row(["disc"], ["DISCONNECT SWITCH, FUSED"], section="EQUIPMENT"),
    Row(["tstat"], ["THERMOSTAT (BY MECH)"]),
    Row(["homerun"], ["HOMERUN TO PANEL, HASH MARKS = CONDUCTORS"]),
]
