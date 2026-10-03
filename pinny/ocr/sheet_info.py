"""Read the sheet number, sheet title and revision from a title block.

The reader OCRs the likely title-block area (or a region the caller gives)
and looks for field labels such as ``SHEET NO``, ``SHEET TITLE`` and ``REV``.
A value is the text after the label on the same line, beside it, or below
it. Geometry is compared in each rotation's reading frame, so title blocks
set vertically along the sheet edge read the same way as upright ones.

Every field records *how* it was found. ``label`` is the most reliable.
``table`` is the highest entry under a ``REV`` column. ``pattern`` is the
tallest sheet-number-shaped text when no label was read. The viewer should
show these for confirmation, never treat them as ground truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import geometry
from .interface import OcrEngine
from .types import OcrBox, OcrError, OcrResult, OcrSettings, OcrWord

#: Default title-block search area as fractions of the page (x0, y0, x1, y1):
#: the bottom-right corner, which holds the sheet number in both the
#: right-edge strip and the bottom-strip layouts.
DEFAULT_TITLE_BLOCK_FRACTIONS: Tuple[float, float, float, float] = (0.65, 0.55, 1.0, 1.0)

SHEET_NUMBER_RE = re.compile(r"^[A-Z]{1,3}[-.]?\d{1,4}(?:[.-]\d{1,3})?[A-Z]?$")
REVISION_RE = re.compile(r"^(?:[A-Z]?\d{1,2}|[A-Z])$")

_TITLE_LABELS = ("SHEET TITLE", "DRAWING TITLE", "SHEET NAME", "DRAWING NAME", "TITLE")
_NUMBER_LABELS = (
    "SHEET NUMBER", "SHEET NO", "DRAWING NUMBER", "DRAWING NO", "DWG NUMBER", "DWG NO", "SHEET",
)
_REVISION_LABELS = ("REVISION NUMBER", "REVISION NO", "REV NUMBER", "REV NO", "REVISION", "REV")
# Other common title-block labels: they end a multi-line title.
_OTHER_LABELS = (
    "DATE", "SCALE", "DRAWN BY", "DRAWN", "CHECKED BY", "CHECKED", "APPROVED BY", "PROJECT NUMBER",
    "PROJECT NO", "PROJECT", "JOB NUMBER", "JOB NO", "CLIENT", "OWNER", "ISSUED FOR", "ISSUE", "SEAL",
    "DESCRIPTION",
)
_ALL_LABELS = tuple(sorted({*_TITLE_LABELS, *_NUMBER_LABELS, *_REVISION_LABELS, *_OTHER_LABELS},
                           key=lambda s: -len(s.split())))
_METHOD_RANK = {"label": 0, "table": 1, "pattern": 2}


@dataclass(frozen=True)
class SheetField:
    value: str
    #: Page-frame box around the words the value came from.
    box: OcrBox
    #: Mean engine confidence (0-100) of those words.
    confidence: float
    #: ``label``, ``table`` or ``pattern`` (see the module docstring).
    method: str
    rotation: int
    #: Other distinct values that were found for this field.
    alternatives: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "value": self.value, "box": self.box.to_dict(), "confidence": self.confidence,
            "method": self.method, "rotation": self.rotation, "alternatives": list(self.alternatives),
        }


@dataclass(frozen=True)
class SheetInfo:
    sheet_number: Optional[SheetField]
    sheet_title: Optional[SheetField]
    revision: Optional[SheetField]
    region: Optional[OcrBox]
    ocr: OcrResult
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        def f(x: Optional[SheetField]) -> Optional[dict]:
            return x.to_dict() if x else None
        return {
            "sheet_number": f(self.sheet_number),
            "sheet_title": f(self.sheet_title),
            "revision": f(self.revision),
            "region": self.region.to_dict() if self.region else None,
            "engine": {"name": self.ocr.engine, "version": self.ocr.engine_version,
                       "settings": self.ocr.settings.to_dict()},
            "warnings": list(self.warnings),
        }


def title_block_region(width: int, height: int,
                       fractions: Tuple[float, float, float, float] = DEFAULT_TITLE_BLOCK_FRACTIONS) -> OcrBox:
    fx0, fy0, fx1, fy1 = fractions
    if not (0.0 <= fx0 < fx1 <= 1.0 and 0.0 <= fy0 < fy1 <= 1.0):
        raise OcrError("invalid_region", f"Title-block fractions must satisfy 0 <= x0 < x1 <= 1 and the same for y, got {fractions}.")
    x0, y0 = int(width * fx0), int(height * fy0)
    x1, y1 = max(int(round(width * fx1)), x0 + 1), max(int(round(height * fy1)), y0 + 1)
    return OcrBox(x0, y0, x1 - x0, y1 - y0)


def read_sheet_info(
    page: np.ndarray,
    engine: Optional[OcrEngine] = None,
    settings: OcrSettings = OcrSettings(),
    region: Optional[OcrBox] = None,
    full_page: bool = False,
) -> SheetInfo:
    """OCR the title block of ``page`` and parse its fields.

    ``engine`` defaults to the app's OCR setting. OCR is off by default, so
    this raises ``ocr_disabled`` until an engine is chosen. ``region``
    overrides the default bottom-right search area. ``full_page`` searches
    the whole sheet, which is slower.
    """
    if engine is None:
        from .registry import get_engine
        engine = get_engine()
        if engine is None:
            raise OcrError(
                "ocr_disabled",
                "OCR is turned off. Choose an OCR engine in the app settings to read sheet info.",
            )
    if region is None and not full_page:
        if not isinstance(page, np.ndarray) or page.ndim not in (2, 3):
            raise OcrError("invalid_page", "Page raster must be a 2-D or 3-D numpy array.")
        region = title_block_region(page.shape[1], page.shape[0])
    result = engine.recognize(page, settings, region=region)
    return parse_sheet_info(result)


# -- parsing (engine-independent) ---------------------------------------------

@dataclass(frozen=True)
class _Line:
    words: Tuple[OcrWord, ...]
    #: Reading-frame boxes, parallel to ``words``.
    boxes: Tuple[OcrBox, ...]
    tokens: Tuple[str, ...]  # normalised, parallel to ``words`` ("" for punctuation only)

    @property
    def box(self) -> OcrBox:
        return _union(self.boxes)

    @property
    def height(self) -> float:
        hs = sorted(b.height for b in self.boxes)
        return float(hs[len(hs) // 2])


@dataclass(frozen=True)
class _Candidate:
    value: str
    words: Tuple[OcrWord, ...]
    method: str
    rotation: int
    rank: float  # lower is better within a method

    def field(self, alternatives: Sequence[str]) -> SheetField:
        return SheetField(
            value=self.value,
            box=_union([w.box for w in self.words]),
            confidence=sum(w.confidence for w in self.words) / len(self.words),
            method=self.method,
            rotation=self.rotation,
            alternatives=tuple(a for a in dict.fromkeys(alternatives) if a != self.value),
        )


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9#]", "", text.upper()).replace("#", "")


def _clean(text: str) -> str:
    return text.strip().strip(".:,;()[]{}|'\"").upper()


def _union(boxes: Sequence[OcrBox]) -> OcrBox:
    x0 = min(b.x for b in boxes)
    y0 = min(b.y for b in boxes)
    x1 = max(b.x2 for b in boxes)
    y1 = max(b.y2 for b in boxes)
    return OcrBox(x0, y0, x1 - x0, y1 - y0)


def _label_at(line: _Line, start: int, labels: Sequence[str]) -> int:
    """Number of words (from ``start``) that spell one of ``labels``, else 0."""
    for label in labels:
        parts = label.split()
        i, j = start, 0
        while i < len(line.tokens) and j < len(parts):
            if line.tokens[i] == "":
                i += 1
                continue
            if line.tokens[i] != parts[j]:
                break
            i += 1
            j += 1
        if j == len(parts):
            return i - start
    return 0


def _is_label_line(line: _Line) -> bool:
    return _label_at(line, 0, _ALL_LABELS) > 0 or line.words[-1].text.rstrip().endswith(":")


def _reading_lines(result: OcrResult) -> Dict[int, List[_Line]]:
    """Lines per rotation with boxes in that rotation's reading frame."""
    if not result.words:
        return {}
    size = (max(w.box.x2 for w in result.words) + 1, max(w.box.y2 for w in result.words) + 1)
    out: Dict[int, List[_Line]] = {}
    for line in result.lines():
        rot = line.rotation
        boxes = tuple(geometry.rotate_box(w.box, size, rot) for w in line.words)
        tokens = tuple(_norm(w.text) for w in line.words)
        out.setdefault(rot, []).append(_Line(line.words, boxes, tokens))
    return out


def _neighbours(label: OcrBox, lines: Sequence[_Line], skip: _Line) -> List[Tuple[float, _Line]]:
    """Lines right of or below ``label``, nearest first, as (distance in label heights, line)."""
    h = max(label.height, 1)
    found: List[Tuple[float, _Line]] = []
    for line in lines:
        if line is skip:
            continue
        b = line.box
        mid = b.y + b.height / 2
        if label.y - 0.5 * h <= mid <= label.y2 + 0.5 * h and b.x >= label.x2 and b.x - label.x2 <= 12 * h:
            found.append(((b.x - label.x2) / h, line))
        elif b.y >= label.y2 - 0.3 * h and b.y - label.y2 <= 8 * h and b.x < label.x2 + 6 * h and b.x2 > label.x - 6 * h:
            found.append(((b.y - label.y2) / h + abs(b.x - label.x) / (4 * h), line))
    found.sort(key=lambda t: t[0])
    return found


def _match_in(line: _Line, start: int, pattern: re.Pattern) -> Optional[Tuple[str, Tuple[OcrWord, ...]]]:
    words = line.words[start:]
    for i, word in enumerate(words):
        value = _clean(word.text)
        if pattern.match(value):
            return value, (word,)
        if i + 1 < len(words):  # "E 101" read as two words
            joined = value + _clean(words[i + 1].text)
            if value.isalpha() and pattern.match(joined):
                return joined, (word, words[i + 1])
    return None


def _labelled(lines: Sequence[_Line], labels: Sequence[str]):
    for line in lines:
        for start in range(len(line.words)):
            n = _label_at(line, start, labels)
            if n:
                yield line, start, n


def _find_number(rot: int, lines: Sequence[_Line]) -> List[_Candidate]:
    cands: List[_Candidate] = []
    for line, start, n in _labelled(lines, _NUMBER_LABELS):
        if _label_at(line, start, _TITLE_LABELS):  # "SHEET TITLE" is not a number label
            continue
        inline = _match_in(line, start + n, SHEET_NUMBER_RE)
        if inline:
            cands.append(_Candidate(inline[0], inline[1], "label", rot, 0.0))
            continue
        label_box = _union(line.boxes[start:start + n])
        for dist, other in _neighbours(label_box, lines, line):
            m = _match_in(other, 0, SHEET_NUMBER_RE)
            if m:
                cands.append(_Candidate(m[0], m[1], "label", rot, dist))
                break
    for line in lines:  # fallback: tallest number-shaped text
        m = _match_in(line, 0, SHEET_NUMBER_RE)
        if m:
            cands.append(_Candidate(m[0], m[1], "pattern", rot, -float(line.height)))
    return cands


_TABLE_HEADER_TOKENS = frozenset({"DATE", "DESCRIPTION", "DESC", "BY", "ISSUE", "REMARKS"})


def _find_revision(rot: int, lines: Sequence[_Line]) -> List[_Candidate]:
    cands: List[_Candidate] = []
    for line, start, n in _labelled(lines, _REVISION_LABELS):
        label_box = _union(line.boxes[start:start + n])
        h = max(label_box.height, 1)
        rest = [t for t in line.tokens[start + n:] if t]
        inline = _match_in(line, start + n, REVISION_RE)
        if inline and len(rest) == 1:
            cands.append(_Candidate(inline[0], inline[1], "label", rot, 0.0))
            continue
        # Revision-shaped words in the column under the label.
        column: List[Tuple[str, OcrWord]] = []
        row_tokens = set()
        for other in lines:
            for word, box, token in zip(other.words, other.boxes, other.tokens):
                mid = box.y + box.height / 2
                if label_box.y - 0.5 * h <= mid <= label_box.y2 + 0.5 * h:
                    row_tokens.add(token)
                elif (box.y >= label_box.y2 - 0.3 * h and box.y - label_box.y2 <= 15 * h
                      and box.x < label_box.x2 + h and box.x2 > label_box.x - h
                      and REVISION_RE.match(_clean(word.text))):
                    column.append((_clean(word.text), word))
        # "REV  DATE  DESCRIPTION" over rows: a revision table, latest = highest.
        if column and (row_tokens & _TABLE_HEADER_TOKENS or len(column) >= 2):
            best = max(column, key=lambda t: _revision_key(t[0]))
            cands.append(_Candidate(best[0], (best[1],), "table", rot, 0.0))
            continue
        for dist, other in _neighbours(label_box, lines, line):
            m = _match_in(other, 0, REVISION_RE)
            if m:
                cands.append(_Candidate(m[0], m[1], "label", rot, dist))
                break
    return cands


def _revision_key(value: str) -> Tuple[int, int, str]:
    digits = "".join(c for c in value if c.isdigit())
    return (1, int(digits), value) if digits else (0, 0, value)


def _find_title(rot: int, lines: Sequence[_Line], number_words: Sequence[OcrWord]) -> List[_Candidate]:
    cands: List[_Candidate] = []
    for line, start, n in _labelled(lines, _TITLE_LABELS):
        rest = line.words[start + n:]
        rest = tuple(w for w in rest if _norm(w.text))
        if rest:
            cands.append(_Candidate(" ".join(w.text for w in rest), rest, "label", rot, 0.0))
            continue
        label_box = _union(line.boxes[start:start + n])
        below = [(d, o) for d, o in _neighbours(label_box, lines, line)
                 if o.box.y >= label_box.y2 - 0.3 * label_box.height and not _is_label_line(o)
                 and not any(w in number_words for w in o.words)]
        if not below:
            continue
        dist, first = below[0]
        picked = [first]
        remaining = sorted((o for _, o in below[1:]), key=lambda o: o.box.y)
        for other in remaining:
            last = picked[-1]
            gap = other.box.y - last.box.y2
            if gap > 1.2 * last.height or not (0.6 <= other.height / max(last.height, 1) <= 1.6):
                continue
            if other.box.x >= last.box.x2 or other.box.x2 <= last.box.x:
                continue
            picked.append(other)
            if len(picked) == 3:
                break
        words = tuple(w for ln in picked for w in ln.words)
        cands.append(_Candidate(" ".join(w.text for w in words), words, "label", rot, dist))
    return cands


def _touches_edge(box: OcrBox, region: OcrBox) -> bool:
    # The engine drops a glyph cut in half, so a clipped word can stop up to
    # about half a character short of the edge.
    margin = max(2.0, 0.5 * min(box.width, box.height))
    return (box.x - region.x <= margin or box.y - region.y <= margin
            or region.x2 - box.x2 <= margin or region.y2 - box.y2 <= margin)


def _best(cands: List[_Candidate]) -> Optional[SheetField]:
    if not cands:
        return None
    ordered = sorted(cands, key=lambda c: (_METHOD_RANK[c.method], c.rank,
                                           -sum(w.confidence for w in c.words) / len(c.words)))
    return ordered[0].field([c.value for c in ordered])


def parse_sheet_info(result: OcrResult) -> SheetInfo:
    by_rotation = _reading_lines(result)
    numbers: List[_Candidate] = []
    revisions: List[_Candidate] = []
    titles: List[_Candidate] = []
    for rot, lines in by_rotation.items():
        rot_numbers = _find_number(rot, lines)
        numbers += rot_numbers
        revisions += _find_revision(rot, lines)
        titles += _find_title(rot, lines, [w for c in rot_numbers for w in c.words])
    number = _best(numbers)
    warnings: List[str] = []
    if number is None:
        warnings.append("No sheet number found. Check the title-block region.")
    elif number.method == "pattern":
        warnings.append("No sheet-number label was read; the number is the tallest number-shaped text.")
    title = _best(titles)
    if title is None:
        warnings.append("No sheet-title label was read.")
    revision = _best(revisions)
    if revision is None:
        warnings.append("No revision label was read.")
    if result.region is not None:
        for name, found in (("sheet number", number), ("sheet title", title), ("revision", revision)):
            if found is not None and _touches_edge(found.box, result.region):
                warnings.append(
                    f"The {name} touches the edge of the search region and may be cut off. "
                    "Pass a larger region or full_page=True."
                )
    return SheetInfo(number, title, revision, result.region, result, tuple(warnings) + result.warnings)
