"""Read a symbol legend from a vector drawing set.

How it works, all in canonical raster pixels (contracts §2):

1. **Find the legend.** Look for a heading such as ``LEGEND``,
   ``SYMBOLS``, ``SYMBOL LIST`` or ``ELECTRICAL SYMBOLS``. Column headers
   (``DESCRIPTION``, and optionally ``SYMBOL`` and ``TAG``/``MARK``) found
   under it fix each column's layout; a legend may have several columns side
   by side. Without headers, the description column is the most common left
   edge of the text under the heading.
2. **Split rows.** Walk down each description column. A line whose symbol
   area holds drawing (or that starts after a horizontal rule) starts a new
   row; a line just below with an empty symbol area continues the row above
   (a wrapped description). A short line with no drawing is a section
   heading ("RECEPTACLES", "LIGHTING") and names the group of what follows.
   The column ends at a large gap or at the next heading (notes,
   abbreviations).
3. **Collect each row's symbol.** Drawing primitives inside the row's band
   and left of the description, minus table rules, are grouped into clusters
   by horizontal gaps. More than one cluster means the row may hold two
   symbols. Short text inside the symbol area ("GFI", "WP") is a label.
4. **Engineer's tag.** From a ``TAG``/``MARK``/``TYPE`` column, else a
   description written ``D1 - DUPLEX ...``, else a short label drawn with the
   symbol. Only if none exists does Pinny make one up (flagged).
5. **Defaults.** Lines, not devices (homeruns, conduit), and items another
   trade provides ("BY MECH", "BY OTHERS") start as not counted.

Scanned legends need text recognition, which isn't built yet; such pages
raise ``raster_page``.
"""

from __future__ import annotations

import hashlib
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from pinny.vector.classify import kind_from_content
from pinny.vector.content import get_page, open_pdf, page_frame, read_page_content
from pinny.vector.geometry import sample

from .errors import LegendError
from .model import Legend, LegendEntry, LegendRow, generated_tag, guess_group, nice_name
from .text import Box, TextLine, extract_text_lines, lines_from_document, open_document

HEADING_RE = re.compile(
    r"^(?:[A-Z]+\s+){0,3}(LEGEND|SYMBOLS?(?:\s+(?:LIST|LEGEND|SCHEDULE|AND\s+ABBREVIATIONS))?)(?:\s+[A-Z]+){0,2}$"
)
STOP_RE = re.compile(r"\b(ABBREVIATIONS|GENERAL NOTES|KEYED NOTES|NOTES|SCHEDULE|SHEET NOTES)\b")
DESC_HEADER_RE = re.compile(r"^DESCRIPTION$")
SYM_HEADER_RE = re.compile(r"^SYMBOLS?$")
TAG_HEADER_RE = re.compile(r"^(TAG|MARK|TYPE|ID|KEY|CODE)$")
TAG_PREFIX_RE = re.compile(r"^([A-Z]{1,3}[0-9]{0,2}[A-Z]?)\s*(?:-|–|—|:|=)\s+(.+)$")
LABEL_RE = re.compile(r"^[A-Z0-9][A-Z0-9/\-]{0,4}$")
BY_OTHERS_RE = re.compile(r"\bBY\s+(OTHERS|MECH\w*|PLUMB\w*|OWNER|GC|G\.C\.|DIV\w*|HVAC|FIRE|TELECOM)\b")
WIRING_RE = re.compile(r"\b(HOME ?RUN|CONDUIT|WIRING|RACEWAY|CABLE TRAY|CIRCUIT RUN|UNDERGROUND)\b")


@dataclass
class ReaderSettings:
    #: Widest symbol area searched left of the description, in px (1.2 in).
    max_symbol_width_px: float = 240.0
    #: Gap, in line heights, that ends a legend column.
    column_end_gap: float = 3.5
    #: Gap, in line heights, still treated as a wrapped description line.
    wrap_gap: float = 0.9
    #: Horizontal gap, in line heights, that separates two symbol drawings.
    cluster_gap: float = 0.7
    #: Text lines under a heading needed to call it a legend.
    min_rows: int = 2


@dataclass
class LegendCandidate:
    page_index: int
    heading: str
    rows: int
    score: float


@dataclass
class _Column:
    desc_x: float
    sym_x0: float
    sym_x1: float
    right: float
    tag_x: Optional[Tuple[float, float]] = None


@dataclass
class _Row:
    lines: List[TextLine]
    y0: float
    y1: float
    column: _Column
    section: Optional[str] = None
    rule_start: bool = False
    tag_text: Optional[str] = None


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _PageGeometry:
    """Drawing primitives of one page as px bounding boxes plus sample points."""

    def __init__(self, content) -> None:
        frame = content.frame
        self.frame = frame
        if content.n_segments:
            ctrl_px = frame.to_px(content.ctrl.reshape(-1, 2)).reshape(-1, 4, 2)
        else:
            ctrl_px = np.zeros((0, 4, 2))
        self.ctrl = ctrl_px
        self.kind = content.kind
        self.boxes = (
            np.stack([ctrl_px[:, :, 0].min(1), ctrl_px[:, :, 1].min(1), ctrl_px[:, :, 0].max(1), ctrl_px[:, :, 1].max(1)], 1)
            if len(ctrl_px) else np.zeros((0, 4))
        )

    def in_rect(self, rect: Box) -> np.ndarray:
        if not len(self.boxes):
            return np.zeros(0, dtype=int)
        x0, y0, x1, y1 = rect
        b = self.boxes
        cx, cy = (b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2
        return np.flatnonzero((cx >= x0) & (cx <= x1) & (cy >= y0) & (cy <= y1))

    def horizontal_rules(self, x0: float, x1: float, min_len: float) -> List[float]:
        """y of horizontal lines at least ``min_len`` long overlapping [x0, x1]."""
        if not len(self.boxes):
            return []
        b = self.boxes
        flat = (b[:, 3] - b[:, 1]) <= 1.5
        long_ = (b[:, 2] - b[:, 0]) >= min_len
        overlap = (b[:, 0] <= x1) & (b[:, 2] >= x0)
        ys = sorted(set(round(float(y), 1) for y in (b[flat & long_ & overlap, 1] + b[flat & long_ & overlap, 3]) / 2))
        return ys


def symbol_signature(geom: _PageGeometry, box: Box, ids: Optional[np.ndarray] = None, grid: int = 16) -> str:
    """Geometry fingerprint of a symbol drawing: which cells of a 16×16 grid
    over its box contain ink. Compares drawings across projects."""
    if ids is None:
        ids = geom.in_rect(box)
    cells = np.zeros((grid, grid), dtype=bool)
    if len(ids):
        pts, _ = sample(geom.ctrl[ids], step=1.0)
        x0, y0, x1, y1 = box
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        s = max(w, h)
        ox, oy = x0 - (s - w) / 2, y0 - (s - h) / 2  # keep aspect ratio
        ij = np.clip(((pts - [ox, oy]) / s * grid).astype(int), 0, grid - 1)
        cells[ij[:, 1], ij[:, 0]] = True
    return np.packbits(cells.ravel()).tobytes().hex()


def signature_similarity(a: str, b: str) -> float:
    """Jaccard similarity of two signatures (1.0 = same ink cells)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    ba = np.unpackbits(np.frombuffer(bytes.fromhex(a), dtype=np.uint8)).astype(bool)
    bb = np.unpackbits(np.frombuffer(bytes.fromhex(b), dtype=np.uint8)).astype(bool)
    union = (ba | bb).sum()
    return float((ba & bb).sum() / union) if union else 1.0


class LegendReader:
    def __init__(self, settings: Optional[ReaderSettings] = None) -> None:
        self.settings = settings or ReaderSettings()

    # ------------------------------------------------------------------
    # Public API

    def find(self, pdf_path: "str | os.PathLike[str]", pages: Optional[Sequence[int]] = None) -> List[LegendCandidate]:
        """Pages that look like they hold a legend, best first."""
        out: List[LegendCandidate] = []
        with open_pdf(pdf_path) as pdf:
            n = len(pdf.pages)
            # Page geometry only; drawings aren't read until the legend page is known.
            frames = {i: page_frame(get_page(pdf, i)) for i in (pages if pages is not None else range(n)) if 0 <= i < n}
        doc = open_document(pdf_path)
        try:
            per_page = {i: lines_from_document(doc, i, frame) for i, frame in frames.items()}
        finally:
            doc.close()
        for i, lines in per_page.items():
            for h in self._headings(lines):
                below = [t for t in lines if t.y0 > h.y1 and t.y0 < h.y1 + 40 * max(h.height, 1)]
                if len(below) >= self.settings.min_rows:
                    score = len(below) + (20 if any(DESC_HEADER_RE.match(t.text.upper()) for t in below) else 0)
                    out.append(LegendCandidate(i, h.text, len(below), float(score)))
        out.sort(key=lambda c: -c.score)
        return out

    def read(
        self,
        pdf_path: "str | os.PathLike[str]",
        page_index: Optional[int] = None,
        engineer: Optional[str] = None,
    ) -> Legend:
        """Read the legend on ``page_index`` (or the best page found)."""
        path = os.fspath(pdf_path)
        if page_index is None:
            found = self.find(path)
            if not found:
                raise LegendError(
                    "legend_not_found",
                    "No page has a heading like LEGEND or SYMBOLS. Pass the legend's page number, or add the symbols by hand.",
                )
            page_index = found[0].page_index
        with open_pdf(path) as pdf:
            if not 0 <= page_index < len(pdf.pages):
                raise LegendError("page_index_out_of_range", f"Page {page_index} is not in this {len(pdf.pages)}-page PDF.")
            content = read_page_content(pdf, page_index)
        kind = kind_from_content(content)
        if kind.kind == "raster":
            raise LegendError(
                "raster_page",
                f"Page {page_index} is a scanned image, so its legend text can't be read yet. Add the symbols by hand.",
            )
        geom = _PageGeometry(content)
        lines = extract_text_lines(path, page_index, content.frame)
        legend = self._read_page(lines, geom)
        legend.pdf_sha256 = _file_sha256(path)
        legend.page_index = page_index
        legend.coordinate_frame = content.frame.descriptor()
        legend.engineer = engineer
        if kind.kind == "mixed":
            legend.warnings.append("This page mixes scanned images and drawing; symbols inside images aren't read.")
        return legend

    # ------------------------------------------------------------------
    # Page analysis

    @staticmethod
    def _headings(lines: Sequence[TextLine]) -> List[TextLine]:
        out = []
        for t in lines:
            up = t.text.upper().strip(" :")
            if not HEADING_RE.match(up) or len(t.text.split()) > 6:
                continue
            # "SYMBOL" beside "DESCRIPTION" is a column header, not a heading.
            if SYM_HEADER_RE.match(up) and any(
                DESC_HEADER_RE.match(o.text.upper()) and abs(o.cy - t.cy) <= max(t.height, 1) for o in lines
            ):
                continue
            out.append(t)
        return out

    def _read_page(self, lines: List[TextLine], geom: _PageGeometry) -> Legend:
        heads = self._headings(lines)
        if not heads:
            raise LegendError("legend_not_found", "This page has no heading like LEGEND or SYMBOLS.")
        best: Optional[Tuple[Legend, int]] = None
        for h in heads:
            try:
                lg = self._read_under(h, lines, geom)
            except LegendError:
                continue
            if best is None or len(lg.entries) > best[1]:
                best = (lg, len(lg.entries))
        if best is None or best[1] < 1:
            raise LegendError("legend_not_found", "Found a legend heading but no symbol rows under it.")
        return best[0]

    def _read_under(self, head: TextLine, lines: List[TextLine], geom: _PageGeometry) -> Legend:
        st = self.settings
        lh = statistics.median([t.height for t in lines if t.height > 0] or [head.height]) or 20.0
        below = [t for t in lines if t.y0 >= head.y1 - 1 and t is not head]
        # Column headers just under the heading.
        header_zone = [t for t in below if t.y0 <= head.y1 + 6 * lh]
        desc_heads = sorted([t for t in header_zone if DESC_HEADER_RE.match(t.text.upper())], key=lambda t: t.x0)
        sym_heads = [t for t in header_zone if SYM_HEADER_RE.match(t.text.upper())]
        tag_heads = [t for t in header_zone if TAG_HEADER_RE.match(t.text.upper())]
        header_bottom = max([t.y1 for t in desc_heads + sym_heads + tag_heads], default=head.y1)
        body = [t for t in below if t.y0 > header_bottom - 1 and t not in sym_heads + tag_heads + desc_heads]
        if not body:
            raise LegendError("legend_not_found", "No text under the heading.")

        columns: List[_Column] = []
        if desc_heads:
            for i, d in enumerate(desc_heads):
                left_limit = desc_heads[i - 1].x0 + 40 if i else head.x0 - st.max_symbol_width_px
                syms = [s for s in sym_heads if left_limit - 20 <= s.x0 < d.x0]
                sym_x0 = min([s.x0 for s in syms], default=max(left_limit, d.x0 - st.max_symbol_width_px)) - lh * 0.5
                right = desc_heads[i + 1].x0 - st.max_symbol_width_px if i + 1 < len(desc_heads) else float("inf")
                tags = [t for t in tag_heads if sym_x0 - 20 <= t.x0 < d.x0]
                tag_x = (tags[0].x0 - lh * 0.5, tags[0].x1 + lh * 1.5) if tags else None
                columns.append(_Column(desc_x=d.x0, sym_x0=sym_x0, sym_x1=d.x0 - lh * 0.3, right=right, tag_x=tag_x))
        else:
            near = [t for t in body if head.x0 - st.max_symbol_width_px <= t.x0 <= head.x0 + st.max_symbol_width_px * 4 and t.y0 < head.y1 + 25 * lh]
            if len(near) < st.min_rows:
                raise LegendError("legend_not_found", "Too little text under the heading.")
            xs = sorted(round(t.x0 / (lh * 0.5)) for t in near)
            mode = max(set(xs), key=xs.count) * lh * 0.5
            columns.append(_Column(desc_x=mode, sym_x0=mode - st.max_symbol_width_px, sym_x1=mode - lh * 0.3, right=float("inf")))

        rows: List[_Row] = []
        for col in columns:
            rows.extend(self._rows_in_column(col, body, geom, lh, head))
        if len([r for r in rows if r.section is None]) < 1:
            raise LegendError("legend_not_found", "No symbol rows under the heading.")

        entries: List[LegendEntry] = []
        legend_rows: List[LegendRow] = []
        n = 0
        for r in rows:
            text = " ".join(t.text for t in r.lines).strip()
            legend_rows.append(LegendRow((r.column.sym_x0, r.y0, r.column.right if r.column.right != float("inf") else max(t.x1 for t in r.lines), r.y1), text, r.section))
            entry = self._entry_for(r, text, geom, lh, lines)
            if entry is None:
                continue
            n += 1
            entry.id = f"L{n}"
            entries.append(entry)

        # Tags: fill in generated ones last so they avoid engineer tags.
        taken = [e.tag for e in entries if e.tag]
        for e in entries:
            if not e.tag:
                e.tag = generated_tag(e.name, taken)
                e.tag_source = "generated"
                e.flags.append("generated_tag")
                taken.append(e.tag)

        xs0 = [c.sym_x0 for c in columns]
        region = (
            min(xs0 + [head.x0]),
            head.y0,
            max(t.x1 for r in rows for t in r.lines),
            max(r.y1 for r in rows),
        )
        return Legend(
            pdf_sha256="", page_index=-1, coordinate_frame={}, heading=head.text, region=region,
            entries=entries, rows=legend_rows,
        )

    def _rows_in_column(self, col: _Column, body: List[TextLine], geom: _PageGeometry, lh: float, head: TextLine) -> List[_Row]:
        st = self.settings
        tol = lh * 0.8
        cand = sorted([t for t in body if col.desc_x - tol <= t.x0 < col.right and t.x0 < col.desc_x + lh * 3], key=lambda t: t.y0)
        rules = geom.horizontal_rules(col.sym_x0, col.desc_x + lh * 4, min_len=(col.desc_x - col.sym_x0) + lh * 4)
        rows: List[_Row] = []
        section: Optional[str] = None
        last_y1 = head.y1
        for t in cand:
            up = t.text.upper()
            if rows and t.y0 - last_y1 > st.column_end_gap * lh:
                break
            if STOP_RE.search(up) and len(t.text.split()) <= 4 and t.x0 <= col.desc_x + tol:
                break
            band = (col.sym_x0, t.y0 - lh * 0.6, col.sym_x1, t.y1 + lh * 0.6)
            ids = self._symbol_ids(geom, band, col, lh)
            if rows and len(ids):
                # A drawing that starts above this line belongs to the row
                # above (a tall symbol beside a wrapped description). Judge
                # whole drawings: a circle is several arc pieces.
                wide = self._symbol_ids(geom, (col.sym_x0, rows[-1].y0 - lh, col.sym_x1, band[3]), col, lh)
                ids = np.array(
                    [i for g in self._touching(geom, wide, lh * 0.3) if geom.boxes[g, 1].min() >= last_y1 - 1
                     for i in g if i in set(ids.tolist())], dtype=int)
            has_graphic = len(ids) > 0
            rule_between = any(last_y1 - 1 <= y <= t.y0 + 1 for y in rules)
            gap = t.y0 - last_y1
            indented = t.x0 > col.desc_x + tol * 0.5
            if rows and not has_graphic and not rule_between and (gap <= st.wrap_gap * lh or indented) and rows[-1].section is None:
                rows[-1].lines.append(t)
                rows[-1].y1 = max(rows[-1].y1, t.y1)
                last_y1 = t.y1
                continue
            words = t.text.split()
            if not has_graphic and len(words) <= 3 and "," not in t.text and t.text.isupper() and not TAG_PREFIX_RE.match(up):
                section = t.text.strip(" :").title()
                rows.append(_Row([t], t.y0, t.y1, col, section="__section__"))
                last_y1 = t.y1
                continue
            rows.append(_Row([t], t.y0, t.y1, col, section=section, rule_start=rule_between))
            last_y1 = t.y1
        # Expand each row's band to the rules or halfway to its neighbours.
        real = [r for r in rows if r.section != "__section__"]
        for i, r in enumerate(real):
            above = [y for y in rules if y <= r.y0 + 1]
            below_r = [y for y in rules if y >= r.y1 - 1]
            prev_y1 = real[i - 1].y1 if i else r.y0 - lh
            next_y0 = real[i + 1].y0 if i + 1 < len(real) else r.y1 + lh
            r.y0 = max(above) if above and max(above) > prev_y1 - 1 else (r.y0 + prev_y1) / 2 if i else r.y0 - lh * 0.6
            r.y1 = min(below_r) if below_r and min(below_r) < next_y0 + 1 else (r.y1 + next_y0) / 2 if i + 1 < len(real) else r.y1 + lh * 0.6
        return real

    def _symbol_ids(self, geom: _PageGeometry, band: Box, col: _Column, lh: float) -> np.ndarray:
        ids = geom.in_rect(band)
        if not len(ids):
            return ids
        b = geom.boxes[ids]
        w, h = b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]
        width = col.sym_x1 - col.sym_x0
        # Drop table rules and column lines.
        keep = ~((h <= 1.5) & (w >= width * 0.8)) & ~((w <= 1.5) & (h >= (band[3] - band[1]) * 1.5))
        return ids[keep]

    def _entry_for(self, r: _Row, text: str, geom: _PageGeometry, lh: float, lines: List[TextLine]) -> Optional[LegendEntry]:
        st = self.settings
        col = r.column
        band = (col.sym_x0, r.y0, col.sym_x1, r.y1)
        ids = self._symbol_ids(geom, band, col, lh)
        # Short text drawn inside the symbol area (not the tag column).
        labels = [t for t in lines if col.sym_x0 <= t.x0 and t.x1 <= col.sym_x1 + lh and r.y0 <= t.cy <= r.y1
                  and LABEL_RE.match(t.text.upper()) and not (col.tag_x and col.tag_x[0] <= t.x0 <= col.tag_x[1])]
        tag_texts = [t for t in lines if col.tag_x and col.tag_x[0] <= t.x0 <= col.tag_x[1] and r.y0 <= t.cy <= r.y1]
        if tag_texts and len(ids):
            # Ink under the tag column belongs to the tag's text box, not the symbol.
            tb = geom.boxes[ids]
            ids = ids[~((tb[:, 0] >= col.tag_x[0]) & (tb[:, 2] <= col.tag_x[1]))]
        if not len(ids) and not labels:
            return None

        clusters = self._clusters(geom, ids, lh)
        boxes: List[Box] = []
        cluster_ids: List[np.ndarray] = []
        for c in clusters:
            b = geom.boxes[c]
            boxes.append((float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 2].max()), float(b[:, 3].max())))
            cluster_ids.append(c)
        label_texts = []
        for t in labels:
            label_texts.append(t.text.upper())
            if boxes:
                k = int(np.argmin([max(0.0, t.x0 - bx[2], bx[0] - t.x1) for bx in boxes]))
                bx = boxes[k]
                boxes[k] = (min(bx[0], t.x0), min(bx[1], t.y0), max(bx[2], t.x1), max(bx[3], t.y1))
            else:
                boxes.append(t.box)
                cluster_ids.append(np.zeros(0, dtype=int))

        # Engineer's tag.
        description = text
        tag, tag_source = "", ""
        if tag_texts:
            tag, tag_source = tag_texts[0].text.strip().upper(), "tag_column"
            description = " ".join(t.text for t in r.lines if t not in tag_texts).strip()
        else:
            m = TAG_PREFIX_RE.match(text.upper().strip())
            if m:
                tag, tag_source = m.group(1), "description"
                description = text.strip()[len(text.strip()) - len(m.group(2)):]
            elif label_texts and len(label_texts) == 1:
                tag, tag_source = label_texts[0], "symbol_label"

        name = nice_name(description)
        flags: List[str] = []
        if len(clusters) > 1:
            flags.append("combined")
        if len(r.lines) > 1:
            flags.append("wrapped")
        count, reason = True, None
        up = description.upper()
        union = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        w, h = union[2] - union[0], union[3] - union[1]
        if WIRING_RE.search(up) or (h > 0 and w / h > 4 and h < lh):
            count, reason = False, "A wiring line, not a device."
        elif BY_OTHERS_RE.search(up):
            count, reason = False, "Provided by another trade."
        group = r.section or guess_group(description)
        if not count and WIRING_RE.search(up):
            group = r.section or "Wiring"
        sigs = [symbol_signature(geom, bx, ids=c if len(c) else None) for bx, c in zip(boxes, cluster_ids)]
        return LegendEntry(
            id="", tag=tag, tag_source=tag_source, name=name, description=description, group=group,
            count=count, not_counted_reason=reason, symbol_boxes=boxes, labels=label_texts,
            row_box=(col.sym_x0, r.y0, max(t.x1 for t in r.lines), r.y1), flags=flags, signatures=sigs,
        )

    @staticmethod
    def _touching(geom: _PageGeometry, ids: np.ndarray, tol: float) -> List[np.ndarray]:
        """Group primitives whose boxes touch (within ``tol``) into drawings."""
        n = len(ids)
        if not n:
            return []
        b = geom.boxes[ids]
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            near = np.flatnonzero((b[:, 0] <= b[i, 2] + tol) & (b[:, 2] >= b[i, 0] - tol)
                                  & (b[:, 1] <= b[i, 3] + tol) & (b[:, 3] >= b[i, 1] - tol))
            for j in near:
                ri, rj = find(i), find(int(j))
                if ri != rj:
                    parent[ri] = rj
        groups: Dict[int, List[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        return [ids[np.array(g)] for g in groups.values()]

    def _clusters(self, geom: _PageGeometry, ids: np.ndarray, lh: float) -> List[np.ndarray]:
        if not len(ids):
            return []
        b = geom.boxes[ids]
        order = np.argsort(b[:, 0], kind="stable")
        gap = self.settings.cluster_gap * lh
        groups: List[List[int]] = []
        right = -np.inf
        for k in order:
            if not groups or b[k, 0] > right + gap:
                groups.append([k])
                right = b[k, 2]
            else:
                groups[-1].append(k)
                right = max(right, b[k, 2])
        return [ids[np.array(g)] for g in groups]


def read_legend(pdf_path: "str | os.PathLike[str]", page_index: Optional[int] = None,
                engineer: Optional[str] = None, settings: Optional[ReaderSettings] = None) -> Legend:
    return LegendReader(settings).read(pdf_path, page_index, engineer)


def find_legends(pdf_path: "str | os.PathLike[str]", pages: Optional[Sequence[int]] = None) -> List[LegendCandidate]:
    return LegendReader().find(pdf_path, pages)


def signature_for_box(pdf_path: "str | os.PathLike[str]", page_index: int, box: Box) -> str:
    """Signature of whatever is drawn inside ``box`` (for symbols added by hand)."""
    with open_pdf(pdf_path) as pdf:
        content = read_page_content(pdf, page_index)
    return symbol_signature(_PageGeometry(content), box)
