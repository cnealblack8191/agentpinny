"""Scan a drawing set for every counted symbol in its legend.

For each sheet (every page except the legend page, unless told otherwise):

* **Vector sheets** load once, and each legend symbol is matched against
  them with the vector matcher: exact block reuse where the export kept
  blocks, otherwise flattened line work, at any quarter turn or mirror.
* **Scanned sheets** are rendered at 200 DPI and matched with the raster
  template matcher, using a crop of the legend symbol as the template.
* **Mixed sheets** get both, merged.

Legend symbols often contain one another: a GFCI is a duplex with a "GFI"
label, a dedicated receptacle is a duplex with a triangle. So after matching,
each location keeps one symbol:

1. A symbol whose legend drawing has a label ("GFI", "WP") only counts where
   that label is written next to it on the sheet.
2. Where several symbols match the same spot, a symbol whose label is found
   there beats one without a label; then the more detailed drawing wins
   (more line work in its legend drawing), then the higher score.

If a legend symbol finds nothing anywhere at its legend size, other sizes
are tried (legends are sometimes drawn larger or smaller than the sheets).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from pinny.detection import BoundingBox, OpenCVTemplateDetector, ScanSettings, Template
from pinny.detection.types import DetectionError
from pinny.legend.model import Legend, LegendEntry
from pinny.legend.text import TextLine, lines_from_document, open_document
from pinny.vector import (
    VectorExemplar,
    VectorMatchError,
    VectorSettings,
    classify_page,
    make_exemplar,
    match_prepared,
    prepare_page,
)
from pinny.vector.content import get_page, open_pdf, page_frame
from pinny.vector.geometry import control_length

from .render import RENDERER_VERSION, render_page

FORMAT = "pinny.set_scan"
FORMAT_VERSION = 1


class ScanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"ScanError({self.code!r}, {str(self)!r})"


@dataclass(frozen=True)
class SetScanSettings:
    #: Pages to scan (0-based). ``None`` = every page.
    pages: Optional[Tuple[int, ...]] = None
    #: Leave out the legend page, whose symbols would count themselves.
    skip_legend_page: bool = True
    vector: VectorSettings = field(default_factory=VectorSettings)
    raster: ScanSettings = field(default_factory=ScanSettings)
    #: Try other sizes for a symbol that matched nothing at legend size.
    scale_search: bool = True
    scale_candidates: Tuple[float, ...] = (0.5, 2 / 3, 0.75, 0.8, 1.25, 4 / 3, 1.5, 2.0)
    #: Vector sheets used to choose a symbol's size before searching all sheets.
    scale_sample_pages: int = 3
    #: A label counts if its text is within this many symbol sizes of the symbol.
    label_radius: float = 1.5
    #: Padding, in px, around a legend symbol when cutting a raster template.
    template_pad_px: int = 4
    #: Two matches closer than this fraction of the smaller symbol are one location.
    same_spot_ratio: float = 0.5
    #: At one location, only symbols scoring within this of the best match
    #: compete on label and detail (so a detailed template that fits worse
    #: doesn't take a plain symbol's spot).
    score_margin: float = 0.05
    #: The same for scanned sheets, where detailed templates score a little lower.
    raster_score_margin: float = 0.10
    #: A label cut from the legend counts as present at this match score.
    label_match_threshold: float = 0.6


@dataclass(frozen=True)
class SheetDetection:
    entry_id: str
    tag: str
    page_index: int
    box: BoundingBox
    score: float
    rotation: int
    mirrored: bool
    source: str
    label: Optional[str] = None
    scale: float = 1.0

    @property
    def center(self) -> Tuple[float, float]:
        return self.box.center

    def to_dict(self) -> dict:
        cx, cy = self.center
        d = {
            "entry_id": self.entry_id, "tag": self.tag, "box": self.box.to_dict(), "x": cx, "y": cy,
            "score": round(float(self.score), 6), "rotation": self.rotation, "mirrored": self.mirrored,
            "source": self.source,
        }
        if self.label:
            d["label"] = self.label
        if self.scale != 1.0:
            d["scale"] = self.scale
        return d


@dataclass
class SheetResult:
    page_index: int
    method: str  # "vector", "raster" or "mixed"
    detections: List[SheetDetection]
    frame: dict
    elapsed_seconds: float
    warnings: List[str] = field(default_factory=list)
    #: Matches dropped because a required label was missing, or another symbol won the spot.
    dropped: Dict[str, int] = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self.detections:
            out[d.tag] = out.get(d.tag, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "page_index": self.page_index, "method": self.method, "coordinate_frame": self.frame,
            "counts": self.counts(), "detections": [d.to_dict() for d in self.detections],
            "dropped": dict(self.dropped), "elapsed_seconds": round(self.elapsed_seconds, 3),
            "warnings": list(self.warnings),
        }


@dataclass
class SetScanResult:
    pdf_sha256: str
    legend_page: int
    entries: List[Dict[str, object]]
    sheets: List[SheetResult]
    scales: Dict[str, float]
    settings: SetScanSettings
    elapsed_seconds: float
    warnings: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))

    def counts(self) -> Dict[str, int]:
        """Project total per tag, in legend order (zero included)."""
        out = {str(e["tag"]): 0 for e in self.entries}
        for s in self.sheets:
            for tag, n in s.counts().items():
                out[tag] = out.get(tag, 0) + n
        return out

    def counts_by_sheet(self) -> Dict[int, Dict[str, int]]:
        return {s.page_index: s.counts() for s in self.sheets}

    def detections(self) -> List[SheetDetection]:
        return [d for s in self.sheets for d in s.detections]

    def to_dict(self) -> dict:
        return {
            "format": FORMAT, "format_version": FORMAT_VERSION, "pdf_sha256": self.pdf_sha256,
            "legend_page": self.legend_page, "created_at": self.created_at, "renderer_version": RENDERER_VERSION,
            "entries": self.entries, "counts": self.counts(),
            "sheets": [s.to_dict() for s in self.sheets], "scales": dict(self.scales),
            "settings": {"vector": self.settings.vector.to_dict(), "raster": self.settings.raster.to_dict(),
                         "scale_search": self.settings.scale_search, "label_radius": self.settings.label_radius},
            "elapsed_seconds": round(self.elapsed_seconds, 3), "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Per-symbol material taken from the legend


@dataclass
class _Symbol:
    entry: LegendEntry
    vector: List[VectorExemplar]
    templates: List[np.ndarray]
    labels: List[str]
    detail: float  # amount of vector drawing in the legend symbol; more = more specific
    raster_detail: float = 0.0  # dark pixels in its raster template
    label_templates: List[np.ndarray] = field(default_factory=list)  # label text pixels from the legend
    order: int = 0  # position in the legend (earlier wins exact ties)


def _detail(vectors: Sequence[VectorExemplar], templates: Sequence[np.ndarray]) -> float:
    """How much drawing a legend symbol has: total line length (px) of its
    vector drawing, else the dark pixels of its raster template. A symbol
    that contains another (duplex + triangle) always has more."""
    if vectors:
        return max(float(control_length(v.ctrl).sum()) for v in vectors)
    if templates:
        return max(float((np.asarray(t).min(axis=2) < 128).sum()) for t in templates)
    return 0.0


def _pad(box: Sequence[float], pad: float, w: int, h: int) -> Tuple[int, int, int, int]:
    x0 = max(0, int(math.floor(box[0] - pad)))
    y0 = max(0, int(math.floor(box[1] - pad)))
    x1 = min(w, int(math.ceil(box[2] + pad)))
    y1 = min(h, int(math.ceil(box[3] + pad)))
    return x0, y0, x1, y1


def _keep_near_lines(crop: np.ndarray, ctrl: np.ndarray, origin: Tuple[int, int], reach_px: int = 3) -> np.ndarray:
    """Whiten every pixel farther than ``reach_px`` from the symbol's own
    line work (``ctrl`` in legend px)."""
    import cv2

    from pinny.vector.geometry import sample

    mask = np.zeros(crop.shape[:2], dtype=np.uint8)
    pts, owner = sample(np.asarray(ctrl, dtype=float), 0.5)
    pts = np.round(pts - np.asarray(origin, dtype=float)).astype(np.int32)
    for k in np.unique(owner):
        seg = pts[owner == k].reshape(-1, 1, 2)
        cv2.polylines(mask, [seg], False, 255, thickness=1)
    mask = cv2.dilate(mask, np.ones((2 * reach_px + 1, 2 * reach_px + 1), np.uint8))
    out = crop.copy()
    out[mask == 0] = 255
    return out


def _labels_of(entry: LegendEntry) -> List[str]:
    labels = [l.upper() for l in entry.labels if l]
    return list(dict.fromkeys(labels))


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------


def scan_set(
    pdf_path: "str | os.PathLike[str]",
    legend: Legend,
    settings: SetScanSettings = SetScanSettings(),
    progress: Optional[Callable[[int, int, int], None]] = None,
    matched: Optional[Callable[[int, int, int], None]] = None,
) -> SetScanResult:
    """Find every counted legend symbol on the set's sheets.

    ``progress(page_index, done, total)`` is called after each sheet is
    finished. ``matched(page_index, done, total)`` is called as each sheet's
    matching (the slow part) finishes, before the size search, so a caller
    can show progress while the sheets are searched.
    """
    start = time.perf_counter()
    path = os.fspath(pdf_path)
    sha = _file_sha256(path)
    if legend.pdf_sha256 and legend.pdf_sha256 != sha:
        raise ScanError(
            "legend_mismatch",
            "This legend was read from a different PDF (or another version of it). Read the legend from this file first.",
        )
    entries = [e for e in legend.counted() if e.symbol_boxes]
    if not entries:
        raise ScanError("nothing_to_count", "The legend has no counted symbols with a drawing. Turn on Count for at least one.")
    if legend.needs_review():
        tags = ", ".join(e.tag for e in legend.needs_review())
        raise ScanError("legend_needs_review", f"Check these legend symbols before scanning: {tags}.")

    with open_pdf(path) as pdf:
        n_pages = len(pdf.pages)
        frames = {i: page_frame(get_page(pdf, i)) for i in range(n_pages)}
    pages = list(settings.pages) if settings.pages is not None else list(range(n_pages))
    bad = [p for p in pages if not 0 <= p < n_pages]
    if bad:
        raise ScanError("page_index_out_of_range", f"Pages {bad} are not in this {n_pages}-page PDF.")
    if settings.skip_legend_page:
        pages = [p for p in pages if p != legend.page_index]
    warnings: List[str] = []

    doc = open_document(path)
    try:
        symbols = _legend_symbols(path, legend, entries, settings, doc, warnings)
        _warn_lookalikes(symbols, warnings)
        sheets: List[SheetResult] = []
        raw_by_page: Dict[int, Tuple[str, list, dict, List[str], float]] = {}
        images: Dict[int, np.ndarray] = {}
        # Scanned sheets are finished at once (the size search is for vector
        # sheets only), so only one page raster is held at a time.
        finished: Dict[int, SheetResult] = {}
        for k, p in enumerate(pages):
            t0 = time.perf_counter()
            kind = classify_page(path, p).kind
            sheet_warnings: List[str] = []
            found: List[Tuple[_Symbol, SheetDetection]] = []
            if kind in ("vector", "mixed"):
                try:
                    prepared = prepare_page(path, p, settings.vector)
                    sheet_warnings.extend(prepared.warnings)
                    for sym in symbols:
                        found.extend((sym, d) for d in _vector_matches(prepared, sym, sym.vector, settings, p, 1.0))
                    del prepared
                except VectorMatchError as exc:
                    sheet_warnings.append(f"Vector matching failed ({exc.code}): {exc}")
            if kind in ("raster", "mixed"):
                img = render_page(path, p, doc)
                for sym in symbols:
                    found.extend((sym, d) for d in _raster_matches(img, sym, settings, p, sheet_warnings))
                if kind == "raster":
                    kept, dropped = _resolve(found, [], settings, text_available=False, img=img)
                    kept.sort(key=lambda d: (d.tag, d.box.y, d.box.x))
                    finished[p] = SheetResult(p, kind, kept, frames[p].descriptor(), time.perf_counter() - t0,
                                              sheet_warnings, dropped)
                else:
                    images[p] = img
                del img
            raw_by_page[p] = (kind, found, frames[p].descriptor(), sheet_warnings, time.perf_counter() - t0)
            if matched:
                matched(p, k + 1, len(pages))

        # Symbols that found nothing anywhere: try other sizes. The size is
        # chosen on a few sample sheets, then every vector sheet is searched
        # once at that size (one sheet in memory at a time).
        scales: Dict[str, float] = {s.entry.id: 1.0 for s in symbols}
        vector_pages = [p for p in pages if raw_by_page[p][0] in ("vector", "mixed")]
        if settings.scale_search and vector_pages:
            missing = [sym for sym in symbols if sym.vector
                       and not any(s is sym for _k, found, *_ in raw_by_page.values() for s, _d in found)]
            if missing:
                sample = vector_pages[: settings.scale_sample_pages]
                tally: Dict[str, Dict[float, int]] = {sym.entry.id: {} for sym in missing}
                for p in sample:
                    prepared = prepare_page(path, p, settings.vector)
                    for sym in missing:
                        for f in settings.scale_candidates:
                            n = len(_vector_matches(prepared, sym, [v.scaled(f) for v in sym.vector], settings, p, f))
                            tally[sym.entry.id][f] = tally[sym.entry.id].get(f, 0) + n
                    del prepared
                chosen = {}
                for sid, counts in tally.items():
                    f, n = max(counts.items(), key=lambda kv: (kv[1], -abs(math.log(kv[0]))), default=(None, 0))
                    if n > 0:
                        chosen[sid] = f
                if chosen:
                    for p in vector_pages:
                        prepared = prepare_page(path, p, settings.vector)
                        for sym in missing:
                            f = chosen.get(sym.entry.id)
                            if f is not None:
                                raw_by_page[p][1].extend(
                                    (sym, d) for d in _vector_matches(prepared, sym, [v.scaled(f) for v in sym.vector], settings, p, f))
                        del prepared
                    for sym in missing:
                        f = chosen.get(sym.entry.id)
                        if f is not None:
                            scales[sym.entry.id] = f
                            warnings.append(f"{sym.entry.tag} ({sym.entry.name}) is drawn at {f:.2f}× its legend size on the sheets.")

        for k, p in enumerate(pages):
            if p in finished:
                sheets.append(finished.pop(p))
                if progress:
                    progress(p, k + 1, len(pages))
                continue
            kind, found, frame, sheet_warnings, elapsed = raw_by_page[p]
            t1 = time.perf_counter()
            lines = lines_from_document(doc, p, frames[p]) if kind in ("vector", "mixed") else []
            img = images.pop(p, None)
            kept, dropped = _resolve(found, lines, settings, text_available=bool(kind != "raster"), img=img)
            kept.sort(key=lambda d: (d.tag, d.box.y, d.box.x))
            sheets.append(SheetResult(p, kind, kept, frame, elapsed + time.perf_counter() - t1, sheet_warnings, dropped))
            if progress:
                progress(p, k + 1, len(pages))
    finally:
        doc.close()

    for sym in symbols:
        if not any(d.entry_id == sym.entry.id for s in sheets for d in s.detections):
            warnings.append(f"{sym.entry.tag} ({sym.entry.name}) was not found on any sheet.")
    entries_out = [
        {"entry_id": s.entry.id, "tag": s.entry.tag, "name": s.entry.name, "group": s.entry.group,
         "labels": s.labels, "vector_exemplars": len(s.vector), "raster_templates": len(s.templates)}
        for s in symbols
    ]
    return SetScanResult(sha, legend.page_index, entries_out, sheets, scales, settings,
                         time.perf_counter() - start, warnings)


def _legend_symbols(path: str, legend: Legend, entries: List[LegendEntry], settings: SetScanSettings,
                    doc, warnings: List[str]) -> List[_Symbol]:
    legend_img = None
    out: List[_Symbol] = []
    for e in entries:
        vectors: List[VectorExemplar] = []
        templates: List[np.ndarray] = []
        for box in e.symbol_boxes:
            x0, y0, x1, y1 = (float(v) for v in box)
            bb = BoundingBox(int(math.floor(x0)) - 2, int(math.floor(y0)) - 2,
                             int(math.ceil(x1 - x0)) + 4, int(math.ceil(y1 - y0)) + 4)
            drawing_box = box
            try:
                vx = make_exemplar(path, legend.page_index, bb, settings.vector)
                vectors.append(vx)
                # The raster template covers the drawing only (the legend box
                # may include a label, which moves around on the sheets).
                pts = vx.ctrl.reshape(-1, 2)
                drawing_box = (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
            except (VectorMatchError, DetectionError) as exc:
                warnings.append(f"{e.tag}: no vector drawing to match ({exc.code}); scanned sheets only.")
            if legend_img is None:
                legend_img = render_page(path, legend.page_index, doc)
            H, W = legend_img.shape[:2]
            cx0, cy0, cx1, cy1 = _pad(drawing_box, settings.template_pad_px, W, H)
            crop = legend_img[cy0:cy1, cx0:cx1].copy()
            # The template is the drawing only: labels move around on the
            # sheets (they are checked separately), and table lines touching
            # the symbol in the legend aren't part of it.
            for lb in e.label_boxes:
                lx0, ly0, lx1, ly1 = _pad(lb, 1, W, H)
                crop[max(0, ly0 - cy0):max(0, ly1 - cy0), max(0, lx0 - cx0):max(0, lx1 - cx0)] = 255
            if vectors and drawing_box is not box:
                crop = _keep_near_lines(crop, vectors[-1].ctrl, (cx0, cy0))
            if crop.size and crop.shape[0] >= settings.raster.min_template_side and crop.shape[1] >= settings.raster.min_template_side:
                templates.append(crop)
        label_tpls = []
        if e.label_boxes:
            if legend_img is None:
                legend_img = render_page(path, legend.page_index, doc)
            H, W = legend_img.shape[:2]
            for lb in e.label_boxes:
                lx0, ly0, lx1, ly1 = _pad(lb, 2, W, H)
                if lx1 - lx0 >= 4 and ly1 - ly0 >= 4:
                    label_tpls.append(legend_img[ly0:ly1, lx0:lx1].copy())
        out.append(_Symbol(e, vectors, templates, _labels_of(e), _detail(vectors, []), _detail([], templates),
                           label_tpls, len(out)))
    return out


def _warn_lookalikes(symbols: Sequence[_Symbol], warnings: List[str]) -> None:
    """Two legend symbols drawn the same, with no label to tell them apart,
    can't be separated on the sheets; say so (ties go to legend order)."""
    from pinny.legend.reader import signature_similarity

    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            if a.labels or b.labels or not a.entry.signatures or not b.entry.signatures:
                continue
            same_detail = abs(a.detail - b.detail) <= 0.02 * max(a.detail, b.detail, 1.0)
            sim = max(signature_similarity(x, y) for x in a.entry.signatures for y in b.entry.signatures)
            if same_detail and sim >= 0.95:
                warnings.append(
                    f"{a.entry.tag} and {b.entry.tag} are drawn the same in the legend and have no label, so "
                    f"Pinny can't tell them apart; matches count as {a.entry.tag}. Merge them or add a label."
                )


def _vector_matches(prepared, sym: _Symbol, variants: Sequence[VectorExemplar], settings: SetScanSettings,
                    page_index: int, scale: float) -> List[SheetDetection]:
    out: List[SheetDetection] = []
    for ex in variants:
        try:
            res = match_prepared(prepared, ex, settings.vector)
        except VectorMatchError:
            continue
        for d in res.detections:
            out.append(SheetDetection(sym.entry.id, sym.entry.tag, page_index, d.box, d.score, d.rotation,
                                      d.mirrored, d.source, scale=scale))
    return out


def _raster_matches(img: np.ndarray, sym: _Symbol, settings: SetScanSettings, page_index: int,
                    warnings: List[str]) -> List[SheetDetection]:
    out: List[SheetDetection] = []
    det = OpenCVTemplateDetector()
    for tpl in sym.templates:
        try:
            res = det.detect(img, Template(tpl), settings.raster)
        except DetectionError as exc:
            warnings.append(f"{sym.entry.tag}: raster matching failed ({exc.code}): {exc}")
            continue
        for c in res.candidates:
            out.append(SheetDetection(sym.entry.id, sym.entry.tag, page_index, c.box, float(c.score), c.rotation,
                                      bool(getattr(c, "mirrored", False)), "raster"))
    return out


def _norm_label(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _label_near(d: SheetDetection, labels: Sequence[str], lines: Sequence[TextLine], radius_ratio: float) -> Optional[str]:
    if not labels:
        return None
    cx, cy = d.center
    r = radius_ratio * max(d.box.width, d.box.height)
    wanted = {_norm_label(l): l for l in labels}
    for t in lines:
        if abs(t.cy - cy) > r or abs((t.x0 + t.x1) / 2 - cx) > r + (t.x1 - t.x0) / 2:
            continue
        for token in re.split(r"\s+", t.text):
            if _norm_label(token) in wanted:
                return wanted[_norm_label(token)]
    return None


def _label_in_image(img: np.ndarray, d: SheetDetection, templates: Sequence[np.ndarray],
                    radius_ratio: float, threshold: float) -> bool:
    """Is one of the legend's label images written next to this match?"""
    import cv2

    cx, cy = d.center
    H, W = img.shape[:2]
    for tpl in templates:
        th, tw = tpl.shape[:2]
        r = radius_ratio * max(d.box.width, d.box.height)
        x0, y0 = max(0, int(cx - r - tw)), max(0, int(cy - r - th))
        x1, y1 = min(W, int(cx + r + tw)), min(H, int(cy + r + th))
        if x1 - x0 < tw or y1 - y0 < th:
            continue
        win = cv2.cvtColor(np.ascontiguousarray(img[y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY)
        t = cv2.cvtColor(np.ascontiguousarray(tpl), cv2.COLOR_RGB2GRAY)
        if float(t.std()) < 4:
            continue
        res = cv2.matchTemplate(cv2.GaussianBlur(win, (0, 0), 0.8), cv2.GaussianBlur(t, (0, 0), 0.8), cv2.TM_CCOEFF_NORMED)
        if np.isfinite(res).any() and float(np.nanmax(res)) >= threshold:
            return True
    return False


def _resolve(found: List[Tuple[_Symbol, SheetDetection]], lines: Sequence[TextLine], settings: SetScanSettings,
             text_available: bool, img: Optional[np.ndarray] = None) -> Tuple[List[SheetDetection], Dict[str, int]]:
    """One symbol per location; see the module docstring for the rules."""
    dropped: Dict[str, int] = {}

    def drop(tag: str) -> None:
        dropped[tag] = dropped.get(tag, 0) + 1

    cands: List[Tuple[_Symbol, SheetDetection]] = []
    for sym, d in found:
        if sym.labels:
            label = None
            if d.source != "raster" and text_available:
                label = _label_near(d, sym.labels, lines, settings.label_radius)
            if label is None and img is not None and sym.label_templates and _label_in_image(
                    img, d, sym.label_templates, settings.label_radius, settings.label_match_threshold):
                label = sym.labels[0]
            if label is None and (text_available or (img is not None and sym.label_templates)):
                drop(sym.entry.tag)
                continue
            if label is not None:
                d = SheetDetection(d.entry_id, d.tag, d.page_index, d.box, d.score, d.rotation, d.mirrored,
                                   d.source, label, d.scale)
        cands.append((sym, d))

    # Group matches at the same spot (greedy, strongest first).
    order = sorted(range(len(cands)), key=lambda i: (-cands[i][1].score, cands[i][1].box.y, cands[i][1].box.x))
    groups: List[List[int]] = []
    centres: List[Tuple[float, float, float]] = []
    for i in order:
        d = cands[i][1]
        cx, cy = d.center
        short = min(d.box.width, d.box.height)
        for g, (gx, gy, gs) in zip(groups, centres):
            if math.hypot(cx - gx, cy - gy) < max(settings.same_spot_ratio * min(short, gs), 1.0):
                g.append(i)
                break
        else:
            groups.append([i])
            centres.append((cx, cy, short))

    kept: List[SheetDetection] = []
    for g in groups:
        best = max(cands[i][1].score for i in g)
        margin = settings.raster_score_margin if any(cands[i][1].source == "raster" for i in g) else settings.score_margin
        contenders = [i for i in g if cands[i][1].score >= best - margin]

        def key(i: int):
            sym, d = cands[i]
            detail = sym.raster_detail if d.source == "raster" else sym.detail
            return (1 if d.label else 0, round(detail * 50) / 50 if detail else 0.0, round(d.score, 2), -sym.order)

        win = max(contenders, key=key)
        kept.append(cands[win][1])
        for i in g:
            if i != win:
                drop(cands[i][1].tag)
    return kept, dropped
