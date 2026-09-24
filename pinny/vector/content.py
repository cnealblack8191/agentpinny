"""Content-stream interpreter: drawing primitives and Form XObject placements.

The interpreter walks a page's content stream with pikepdf, tracks the
current transformation matrix (``q``/``Q``/``cm``), descends into Form
XObjects (applying their ``/Matrix``), and records:

* every stroked or filled path segment as a cubic Bezier in PDF user space
  (straight lines are stored as degenerate cubics, ``kind == LINE``);
* every ``Do`` of a Form XObject with its full form-to-user matrix, its
  ``/BBox``, a content hash, and the range of primitives drawn inside it;
* image and text statistics used to classify the page.

Clipping paths, shadings, Type 3 glyphs and text glyph outlines are not
interpreted (text is not geometry here; see docs/vector-matching.md).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pikepdf

from .errors import VectorMatchError
from .frame import PageFrame, apply, frame_from_boxes, pdf_matrix

LINE = 0
CURVE = 1

MAX_FORM_DEPTH = 32
_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
_CLOSE_PAINT_OPS = {"s", "b", "b*"}
_TEXT_SHOW_OPS = {"Tj", "TJ", "'", '"'}
_IDENTITY = pdf_matrix(1, 0, 0, 1, 0, 0)


# --------------------------------------------------------------------------
# Opening documents
# --------------------------------------------------------------------------


def open_pdf(pdf_path: "str | os.PathLike[str]") -> pikepdf.Pdf:
    """Open a PDF for reading, mapping failures to :class:`VectorMatchError`."""
    try:
        return pikepdf.open(os.fspath(pdf_path))
    except pikepdf.PasswordError:
        raise VectorMatchError(
            "pdf_encrypted",
            f"{os.fspath(pdf_path)!r} is encrypted and needs a password. Ask for an "
            "unencrypted copy (or remove the password) before matching.",
        ) from None
    except FileNotFoundError:
        raise VectorMatchError(
            "pdf_unreadable", f"PDF file not found: {os.fspath(pdf_path)!r}."
        ) from None
    except (pikepdf.PdfError, OSError, ValueError) as exc:
        raise VectorMatchError(
            "pdf_unreadable",
            f"Could not read {os.fspath(pdf_path)!r} as a PDF: {exc}.",
        ) from None


def get_page(pdf: pikepdf.Pdf, page_index: object) -> pikepdf.Page:
    if isinstance(page_index, bool) or not isinstance(page_index, (int, np.integer)):
        raise VectorMatchError(
            "page_index_out_of_range", f"page_index must be an integer, got {page_index!r}."
        )
    n = len(pdf.pages)
    if not 0 <= int(page_index) < n:
        raise VectorMatchError(
            "page_index_out_of_range",
            f"page_index {int(page_index)} is out of range; the PDF has {n} page(s) "
            f"(valid indices 0..{n - 1}).",
        )
    return pdf.pages[int(page_index)]


def _inherited(obj: pikepdf.Object, key: str):
    seen = 0
    node: Optional[pikepdf.Object] = obj
    while node is not None and seen < 64:
        if key in node:
            return node[key]
        node = node.get("/Parent")
        seen += 1
    return None


def page_frame(page: pikepdf.Page) -> PageFrame:
    obj = page.obj
    mediabox = _inherited(obj, "/MediaBox")
    if mediabox is None:
        mediabox = [0, 0, 612, 792]  # US Letter: what viewers assume.
    cropbox = _inherited(obj, "/CropBox")
    rotate = _inherited(obj, "/Rotate")
    return frame_from_boxes(list(mediabox), list(cropbox) if cropbox is not None else None,
                            rotate if rotate is not None else 0)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class Placement:
    """One ``Do`` of a Form XObject (possibly nested inside other forms)."""

    name: str
    #: Identity of the XObject's appearance: sha256 over its decoded content
    #: stream, ``/BBox`` and nested XObject identities. Two placements of the
    #: same indirect object always share it; copies with identical content do too.
    group_key: str
    objgen: Tuple[int, int]
    #: Form space to PDF user space (``/Matrix`` then the CTM at the ``Do``).
    matrix: np.ndarray
    bbox_form: Tuple[float, float, float, float]
    prim_start: int
    prim_end: int
    depth: int


@dataclass
class PageContent:
    frame: PageFrame
    #: ``(N, 4, 2)`` cubic control points in PDF user space.
    ctrl: np.ndarray
    #: ``(N,)`` LINE or CURVE.
    kind: np.ndarray
    placements: List[Placement]
    image_area_pt2: float = 0.0
    n_image_xobjects: int = 0
    n_inline_images: int = 0
    n_text_shows: int = 0
    n_path_paints: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def n_segments(self) -> int:
        return int(self.ctrl.shape[0])


# --------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------


class _Walker:
    def __init__(self, frame: PageFrame, want_paths: bool = True) -> None:
        self.frame = frame
        self.want_paths = want_paths
        self.segs: List[Tuple[float, ...]] = []
        self.kinds: List[int] = []
        self.placements: List[Placement] = []
        self.image_area = 0.0
        self.n_images = 0
        self.n_inline = 0
        self.n_text = 0
        self.n_paints = 0
        self.warnings: List[str] = []
        self._hash_memo: Dict[Tuple[int, int], str] = {}
        x0, y0, x1, y1 = frame.crop
        self._crop = (x0, y0, x1, y1)

    # -- helpers ---------------------------------------------------------

    def _image_area(self, ctm: np.ndarray) -> float:
        corners = apply(ctm, np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=float))
        x0, y0 = corners.min(axis=0)
        x1, y1 = corners.max(axis=0)
        cx0, cy0, cx1, cy1 = self._crop
        w = max(0.0, min(x1, cx1) - max(x0, cx0))
        h = max(0.0, min(y1, cy1) - max(y0, cy0))
        return w * h

    def form_hash(self, xobj: pikepdf.Object, stack: Tuple[Tuple[int, int], ...] = ()) -> str:
        og = tuple(xobj.objgen)
        if og != (0, 0) and og in self._hash_memo:
            return self._hash_memo[og]
        h = hashlib.sha256()
        try:
            h.update(xobj.read_bytes())
        except (pikepdf.PdfError, NotImplementedError, ValueError):
            h.update(xobj.read_raw_bytes())
        h.update(repr([round(float(v), 4) for v in xobj.get("/BBox", [])]).encode())
        res = xobj.get("/Resources")
        xres = res.get("/XObject") if isinstance(res, pikepdf.Dictionary) else None
        if isinstance(xres, pikepdf.Dictionary) and len(stack) < MAX_FORM_DEPTH:
            for key in sorted(xres.keys()):
                child = xres[key]
                if not isinstance(child, pikepdf.Stream):
                    continue
                cog = tuple(child.objgen)
                h.update(key.encode())
                if child.get("/Subtype") == pikepdf.Name.Form and cog not in stack:
                    h.update(self.form_hash(child, stack + (og,)).encode())
                else:
                    h.update(repr(cog).encode())
        digest = h.hexdigest()
        if og != (0, 0):
            self._hash_memo[og] = digest
        return digest

    # -- main loop -------------------------------------------------------

    def walk(self, source, resources, ctm: np.ndarray, depth: int,
             stack: Tuple[Tuple[int, int], ...]) -> None:
        try:
            instructions = pikepdf.parse_content_stream(source)
        except pikepdf.PdfError as exc:
            self.warnings.append(f"Skipped an unparsable content stream: {exc}.")
            return
        a, b, c, d, e, f = (float(v) for v in (ctm[0, 0], ctm[0, 1], ctm[1, 0], ctm[1, 1], ctm[2, 0], ctm[2, 1]))
        gstack: List[Tuple[float, ...]] = []
        want = self.want_paths
        segs = self.segs
        kinds = self.kinds
        path: List[Tuple[Tuple[float, ...], int]] = []
        cur = (0.0, 0.0)
        start = (0.0, 0.0)

        def tp(x: float, y: float) -> Tuple[float, float]:
            return (a * x + c * y + e, b * x + d * y + f)

        xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None

        for ins in instructions:
            op = str(ins.operator)
            if op in ("m", "l", "c", "v", "y", "re", "h"):
                if not want:
                    continue
                try:
                    o = [float(v) for v in ins.operands]
                except (TypeError, ValueError):
                    continue
                if op == "m" and len(o) >= 2:
                    cur = start = tp(o[0], o[1])
                elif op == "l" and len(o) >= 2:
                    p = tp(o[0], o[1])
                    path.append(((cur[0], cur[1], cur[0], cur[1], p[0], p[1], p[0], p[1]), LINE))
                    cur = p
                elif op == "c" and len(o) >= 6:
                    p1, p2, p3 = tp(o[0], o[1]), tp(o[2], o[3]), tp(o[4], o[5])
                    path.append(((cur[0], cur[1], p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]), CURVE))
                    cur = p3
                elif op == "v" and len(o) >= 4:
                    p2, p3 = tp(o[0], o[1]), tp(o[2], o[3])
                    path.append(((cur[0], cur[1], cur[0], cur[1], p2[0], p2[1], p3[0], p3[1]), CURVE))
                    cur = p3
                elif op == "y" and len(o) >= 4:
                    p1, p3 = tp(o[0], o[1]), tp(o[2], o[3])
                    path.append(((cur[0], cur[1], p1[0], p1[1], p3[0], p3[1], p3[0], p3[1]), CURVE))
                    cur = p3
                elif op == "re" and len(o) >= 4:
                    x, y, w, hh = o[:4]
                    q = [tp(x, y), tp(x + w, y), tp(x + w, y + hh), tp(x, y + hh)]
                    for i in range(4):
                        p0, p1 = q[i], q[(i + 1) % 4]
                        path.append(((p0[0], p0[1], p0[0], p0[1], p1[0], p1[1], p1[0], p1[1]), LINE))
                    cur = start = q[0]
                elif op == "h":
                    if cur != start:
                        path.append(((cur[0], cur[1], cur[0], cur[1], start[0], start[1], start[0], start[1]), LINE))
                    cur = start
            elif op in _PAINT_OPS:
                if want:
                    if op in _CLOSE_PAINT_OPS and cur != start:
                        path.append(((cur[0], cur[1], cur[0], cur[1], start[0], start[1], start[0], start[1]), LINE))
                    for seg, k in path:
                        segs.append(seg)
                        kinds.append(k)
                self.n_paints += 1
                path = []
            elif op == "n":
                path = []
            elif op == "q":
                gstack.append((a, b, c, d, e, f))
            elif op == "Q":
                if gstack:
                    a, b, c, d, e, f = gstack.pop()
            elif op == "cm":
                try:
                    m = [float(v) for v in ins.operands]
                except (TypeError, ValueError):
                    continue
                if len(m) != 6:
                    continue
                ma, mb, mc, md, me, mf = m
                # new CTM = M_cm x CTM (row-vector convention)
                a, b, c, d, e, f = (
                    ma * a + mb * c,
                    ma * b + mb * d,
                    mc * a + md * c,
                    mc * b + md * d,
                    me * a + mf * c + e,
                    me * b + mf * d + f,
                )
            elif op == "Do":
                if not ins.operands or xobjects is None:
                    continue
                name = str(ins.operands[0])
                xobj = xobjects.get(name)
                if not isinstance(xobj, pikepdf.Stream):
                    continue
                cur_ctm = pdf_matrix(a, b, c, d, e, f)
                subtype = xobj.get("/Subtype")
                if subtype == pikepdf.Name.Image:
                    self.n_images += 1
                    self.image_area += self._image_area(cur_ctm)
                elif subtype == pikepdf.Name.Form:
                    self._do_form(name, xobj, resources, cur_ctm, depth, stack)
            elif op == "INLINE IMAGE":
                self.n_inline += 1
                self.image_area += self._image_area(pdf_matrix(a, b, c, d, e, f))
            elif op in _TEXT_SHOW_OPS:
                self.n_text += 1

    def _do_form(self, name, xobj, parent_resources, ctm, depth, stack) -> None:
        og = tuple(xobj.objgen)
        if depth >= MAX_FORM_DEPTH:
            self.warnings.append(f"Form XObject nesting deeper than {MAX_FORM_DEPTH}; skipped {name}.")
            return
        if og != (0, 0) and og in stack:
            self.warnings.append(f"Form XObject {name} draws itself recursively; skipped.")
            return
        m = xobj.get("/Matrix")
        try:
            fm = pdf_matrix(*[float(v) for v in m]) if m is not None and len(m) == 6 else _IDENTITY
        except (TypeError, ValueError):
            fm = _IDENTITY
        full = fm @ ctm
        bbox_raw = xobj.get("/BBox", [0, 0, 0, 0])
        try:
            bx = [float(v) for v in bbox_raw]
            bbox = (min(bx[0], bx[2]), min(bx[1], bx[3]), max(bx[0], bx[2]), max(bx[1], bx[3]))
        except (TypeError, ValueError, IndexError):
            bbox = (0.0, 0.0, 0.0, 0.0)
        res = xobj.get("/Resources")
        if not isinstance(res, pikepdf.Dictionary):
            res = parent_resources  # PDF 1.1 style: inherit the caller's resources
        placement = Placement(
            name=name,
            group_key=self.form_hash(xobj),
            objgen=og,
            matrix=full,
            bbox_form=bbox,
            prim_start=len(self.segs),
            prim_end=len(self.segs),
            depth=depth + 1,
        )
        self.placements.append(placement)
        self.walk(xobj, res, full, depth + 1, stack + ((og,) if og != (0, 0) else ()))
        placement.prim_end = len(self.segs)


def read_page_content(pdf: pikepdf.Pdf, page_index: int, want_paths: bool = True) -> PageContent:
    """Interpret one page. ``page_index`` must already be validated."""
    page = get_page(pdf, page_index)
    frame = page_frame(page)
    walker = _Walker(frame, want_paths=want_paths)
    resources = _inherited(page.obj, "/Resources")
    if "/Contents" in page.obj:
        walker.walk(page, resources, _IDENTITY.copy(), 0, ())
    if walker.segs:
        ctrl = np.asarray(walker.segs, dtype=float).reshape(-1, 4, 2)
        kind = np.asarray(walker.kinds, dtype=np.uint8)
        lines = kind == LINE
        p0, p3 = ctrl[lines, 0], ctrl[lines, 3]
        ctrl[lines, 1] = p0 + (p3 - p0) / 3.0
        ctrl[lines, 2] = p0 + (p3 - p0) * (2.0 / 3.0)
    else:
        ctrl = np.zeros((0, 4, 2), dtype=float)
        kind = np.zeros((0,), dtype=np.uint8)
    return PageContent(
        frame=frame,
        ctrl=ctrl,
        kind=kind,
        placements=walker.placements,
        image_area_pt2=walker.image_area,
        n_image_xobjects=walker.n_images,
        n_inline_images=walker.n_inline,
        n_text_shows=walker.n_text,
        n_path_paints=walker.n_paints,
        warnings=walker.warnings,
    )
