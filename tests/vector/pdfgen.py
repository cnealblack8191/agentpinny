"""Synthetic PDF fixtures for the vector matcher tests (written with pikepdf).

Symbols are drawn in "symbol space" (points, y up, origin at the circle
centre) and placed with a ``cm`` of mirror -> counter-clockwise rotation ->
translation, either through a Form XObject (``/Sym Do``) or inline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pikepdf

K = 0.5522847498


def circle_ops(r: float, cx: float = 0.0, cy: float = 0.0) -> str:
    k = K * r
    return (
        f"{cx + r} {cy} m "
        f"{cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} c "
        f"{cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy} c "
        f"{cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} c "
        f"{cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy} c S\n"
    )


def line_ops(x0, y0, x1, y1) -> str:
    return f"{x0} {y0} m {x1} {y1} l S\n"


def symbol_ops(variant: str = "duplex") -> str:
    """Receptacle-like symbol and look-alikes. All share the leg and tick."""
    common = line_ops(0, -6, 0, -12) + line_ops(0, -12, 3, -12)
    if variant == "duplex":
        return circle_ops(6) + line_ops(-2, -4, -2, 4) + line_ops(2, -4, 2, 4) + common
    if variant == "simplex":
        return circle_ops(6) + line_ops(0, -4, 0, 4) + common
    if variant == "square":
        sq = "-6 -6 m 6 -6 l 6 6 l -6 6 l h S\n"
        return sq + line_ops(-2, -4, -2, 4) + line_ops(2, -4, 2, 4) + common
    if variant == "quad":
        return symbol_ops("duplex") + line_ops(-5, 0, 5, 0)
    raise ValueError(variant)


def symbol_points(variant: str = "duplex", n: int = 64) -> np.ndarray:
    """Dense points on the duplex symbol in symbol space (for expectations)."""
    assert variant == "duplex"
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pts = [np.stack([6 * np.cos(t), 6 * np.sin(t)], axis=1)]
    for (x0, y0, x1, y1) in [(-2, -4, -2, 4), (2, -4, 2, 4), (0, -6, 0, -12), (0, -12, 3, -12)]:
        s = np.linspace(0, 1, 9)[:, None]
        pts.append(np.array([x0, y0]) * (1 - s) + np.array([x1, y1]) * s)
    return np.concatenate(pts)


#: Ink bounding box of the duplex symbol in symbol space.
DUPLEX_BBOX = (-6.0, -12.0, 6.0, 6.0)


@dataclass(frozen=True)
class Placement:
    tx: float
    ty: float
    angle: float = 0.0  # counter-clockwise degrees in PDF user space
    mirror: bool = False
    variant: str = "duplex"

    def matrix(self) -> Tuple[float, float, float, float, float, float]:
        c, s = math.cos(math.radians(self.angle)), math.sin(math.radians(self.angle))
        sx = -1.0 if self.mirror else 1.0
        # row-vector: [x y] @ diag(sx, 1) @ [[c, s], [-s, c]]
        a, b = sx * c, sx * s
        cc, d = -s, c
        return (round(a, 12), round(b, 12), round(cc, 12), round(d, 12), self.tx, self.ty)

    def to_user(self, pts: np.ndarray) -> np.ndarray:
        a, b, c, d, e, f = self.matrix()
        return np.stack([a * pts[:, 0] + c * pts[:, 1] + e, b * pts[:, 0] + d * pts[:, 1] + f], axis=1)


def cm(p: Placement) -> str:
    return " ".join(f"{v:.10g}" for v in p.matrix()) + " cm\n"


def user_to_canonical_px(pts: np.ndarray, crop: Sequence[float], rotate: int) -> np.ndarray:
    """Independent reference mapping (written out per rotation) for tests."""
    x0, y0, x1, y1 = crop
    w, h = x1 - x0, y1 - y0
    u = pts[:, 0] - x0
    v = y1 - pts[:, 1]
    if rotate == 0:
        X, Y = u, v
    elif rotate == 90:
        X, Y = h - v, u
    elif rotate == 180:
        X, Y = w - u, h - v
    elif rotate == 270:
        X, Y = v, w - u
    else:
        raise ValueError(rotate)
    return np.stack([X, Y], axis=1) * (200.0 / 72.0)


def expected_box_px(p: Placement, crop, rotate) -> Tuple[float, float, float, float]:
    px = user_to_canonical_px(p.to_user(symbol_points()), crop, rotate)
    return (px[:, 0].min(), px[:, 1].min(), px[:, 0].max(), px[:, 1].max())


def _new_page(pdf: pikepdf.Pdf, mediabox, cropbox=None, rotate=0) -> pikepdf.Page:
    page = pdf.add_blank_page(page_size=(mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]))
    page.obj.MediaBox = pikepdf.Array(list(mediabox))
    if cropbox is not None:
        page.obj.CropBox = pikepdf.Array(list(cropbox))
    if rotate:
        page.obj.Rotate = rotate
    return page


def form_xobject(pdf: pikepdf.Pdf, ops: str, bbox=(-8, -14, 8, 8)) -> pikepdf.Stream:
    s = pikepdf.Stream(pdf, ops.encode())
    s.Type = pikepdf.Name.XObject
    s.Subtype = pikepdf.Name.Form
    s.BBox = pikepdf.Array(list(bbox))
    s.Resources = pikepdf.Dictionary()
    return s


def build_xobject_page(pdf: pikepdf.Pdf, placements: Iterable[Placement], *, mediabox,
                       cropbox=None, rotate=0, extra_ops: str = "",
                       duplicate_form_copy: bool = False) -> pikepdf.Page:
    """Each variant becomes one Form XObject; placements use ``Do``. With
    ``duplicate_form_copy`` a second, separate but identical duplex XObject
    is used for every other duplex placement (same content hash)."""
    page = _new_page(pdf, mediabox, cropbox, rotate)
    forms: Dict[str, pikepdf.Object] = {}
    xres = pikepdf.Dictionary()
    ops = [extra_ops]
    n = 0
    for p in placements:
        name = p.variant
        if duplicate_form_copy and p.variant == "duplex" and n % 2 == 1:
            name = "duplex_copy"
        if p.variant == "duplex":
            n += 1
        if name not in forms:
            forms[name] = pdf.make_indirect(form_xobject(pdf, symbol_ops(p.variant)))
            xres[f"/{name}"] = forms[name]
        ops.append(f"q {cm(p)}/{name} Do Q\n")
    page.obj.Resources = pikepdf.Dictionary(XObject=xres)
    page.obj.Contents = pdf.make_stream("".join(ops).encode())
    return page


def build_flat_page(pdf: pikepdf.Pdf, placements: Iterable[Placement], *, mediabox,
                    cropbox=None, rotate=0, extra_ops: str = "") -> pikepdf.Page:
    page = _new_page(pdf, mediabox, cropbox, rotate)
    ops = ["0.5 w\n", extra_ops]
    for p in placements:
        ops.append(f"q {cm(p)}{symbol_ops(p.variant)}Q\n")
    page.obj.Contents = pdf.make_stream("".join(ops).encode())
    return page


def build_raster_page(pdf: pikepdf.Pdf, *, mediabox=(0, 0, 612, 792), extra_ops: str = "") -> pikepdf.Page:
    page = _new_page(pdf, mediabox)
    w, h = 64, 80
    rng = np.random.default_rng(0)
    img = pikepdf.Stream(pdf, rng.integers(0, 255, size=w * h, dtype=np.uint8).tobytes())
    img.Type = pikepdf.Name.XObject
    img.Subtype = pikepdf.Name.Image
    img.Width, img.Height = w, h
    img.ColorSpace = pikepdf.Name.DeviceGray
    img.BitsPerComponent = 8
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img))
    W, H = mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]
    page.obj.Contents = pdf.make_stream(
        f"q {W} 0 0 {H} {mediabox[0]} {mediabox[1]} cm /Im0 Do Q\n{extra_ops}".encode()
    )
    return page


def random_lines_ops(n: int, width: float, height: float, seed: int = 1,
                     min_len: float = 20.0, max_len: float = 40.0,
                     avoid: Optional[List[Tuple[float, float, float, float]]] = None) -> str:
    rng = np.random.default_rng(seed)
    out: List[str] = []
    x = rng.uniform(0, width, n)
    y = rng.uniform(0, height, n)
    ln = rng.uniform(min_len, max_len, n)
    # Mostly axis-aligned like walls and grid lines, some diagonal.
    ang = rng.choice([0.0, 90.0, 0.0, 90.0, 45.0, 30.0, 135.0], n) + rng.normal(0, 0.0, n)
    for xi, yi, li, ai in zip(x, y, ln, ang):
        x1 = xi + li * math.cos(math.radians(ai))
        y1 = yi + li * math.sin(math.radians(ai))
        out.append(f"{xi:.2f} {yi:.2f} m {x1:.2f} {y1:.2f} l S\n")
    return "".join(out)


def _dihedral_ref(rotation: int, mirrored: bool, pts: np.ndarray) -> np.ndarray:
    """Reference: mirror left-right (x -> -x), then rotate clockwise in y-down."""
    x, y = pts[:, 0].copy(), pts[:, 1].copy()
    if mirrored:
        x = -x
    for _ in range((rotation // 90) % 4):
        x, y = -y, x  # (1, 0) -> (0, 1): right -> down = clockwise on screen
    return np.stack([x, y], axis=1)


def expected_pose(exemplar: Placement, inst: Placement, crop, rotate) -> Tuple[int, bool]:
    """The (rotation, mirrored) that carries the exemplar's canonical-px
    geometry onto the instance's, found by brute force over the 8 maps."""
    pts = symbol_points()
    e = user_to_canonical_px(exemplar.to_user(pts), crop, rotate)
    i = user_to_canonical_px(inst.to_user(pts), crop, rotate)
    e = e - e.mean(axis=0)
    i = i - i.mean(axis=0)
    for mirrored in (False, True):
        for rotation in (0, 90, 180, 270):
            if np.abs(_dihedral_ref(rotation, mirrored, e) - i).max() < 1e-6:
                return rotation, mirrored
    raise AssertionError("instance is not a quarter-turn/mirror of the exemplar")


def box_for(p: Placement, crop, rotate, margin: int = 3) -> Tuple[int, int, int, int]:
    """An exemplar box a user might draw: the symbol plus a small margin."""
    b = expected_box_px(p, crop, rotate)
    x0, y0 = int(math.floor(b[0])) - margin, int(math.floor(b[1])) - margin
    x1, y1 = int(math.ceil(b[2])) + margin, int(math.ceil(b[3])) + margin
    return (x0, y0, x1 - x0, y1 - y0)


def standard_placements(n: int = 20, x0: float = 120, y0: float = 120, dx: float = 70,
                        dy: float = 70, cols: int = 5) -> List[Placement]:
    """``n`` duplex placements cycling through all rotations, every 3rd mirrored."""
    return [
        Placement(x0 + dx * (i % cols), y0 + dy * (i // cols), angle=90.0 * (i % 4),
                  mirror=(i % 3 == 2))
        for i in range(n)
    ]
