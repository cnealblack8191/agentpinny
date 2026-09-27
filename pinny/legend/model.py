"""The editable legend: what Pinny read, plus every change a person made.

A :class:`Legend` is saved as JSON (``pinny.legend`` v1). Coordinates are
canonical raster pixels on the legend page (contracts §2). Every edit is
appended to ``edits`` so the learning store can tell what Pinny read
correctly from what a person had to fix.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import LegendError

FORMAT = "pinny.legend"
FORMAT_VERSION = 1

Box = Tuple[float, float, float, float]

#: Flags that need a person to look before scanning.
CHECK_FLAGS = ("combined", "wrapped", "duplicate_tag")
#: Informational flags.
INFO_FLAGS = ("generated_tag",)
TAG_SOURCES = ("tag_column", "description", "symbol_label", "generated", "user")
STATUSES = ("ok", "check", "added")

_ACRONYMS = {
    "GFCI", "GFI", "USB", "NEMA", "AFF", "NEC", "WP", "AC", "DC", "TR", "IG", "UON", "U.O.N.", "CAT6", "CAT5E",
    "EMT", "PVC", "HVAC", "VAV", "AHU", "RTU", "EF", "UPS", "ATS", "MCC", "TV", "CATV", "AV", "POE", "PA",
    "EV", "EVSE", "KVA", "KW", "HP", "MLO", "MCB", "GC", "EC", "CO", "PIR", "LED", "EM", "ADA", "FACP", "NFPA",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def nice_name(description: str) -> str:
    """Readable name from a legend description: its first clause in title
    case, keeping acronyms, ratings and codes as written."""
    first = re.split(r",|;|\s-\s|\s–\s", description, maxsplit=1)[0].strip()
    words = []
    for w in first.split():
        core = w.strip("()\"'.")
        if core.upper() in _ACRONYMS or any(ch.isdigit() for ch in core) or (len(core) <= 3 and core.isupper() and not core.isalpha()):
            words.append(w.upper() if core.upper() in _ACRONYMS else w)
        elif w.isupper() or w.islower():
            words.append(w.lower())
        else:
            words.append(w)
    name = " ".join(words)
    return name[:1].upper() + name[1:] if name else "Symbol"


def generated_tag(name: str, taken: Sequence[str]) -> str:
    """Initials of the name, made unique against ``taken``."""
    plain = re.sub(r"\([^)]*\)", " ", name)
    letters = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", plain))[:3].upper() or "S"
    tag, n = letters, 2
    while tag in taken:
        tag = f"{letters}{n}"
        n += 1
    return tag


@dataclass
class LegendEntry:
    id: str
    tag: str
    tag_source: str
    name: str
    description: str
    group: str
    count: bool = True
    not_counted_reason: Optional[str] = None
    #: One box per symbol drawing, on the legend page (canonical px).
    symbol_boxes: List[Box] = field(default_factory=list)
    #: Short text drawn inside the symbol area (e.g. "GFI", "WP").
    labels: List[str] = field(default_factory=list)
    row_box: Optional[Box] = None
    flags: List[str] = field(default_factory=list)
    #: "read" (from the legend) or "user" (added or split by a person).
    source: str = "read"
    confirmed: bool = False
    #: One geometry signature per symbol box (see ``reader.symbol_signature``).
    signatures: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.source == "user" and not any(f in CHECK_FLAGS for f in self.flags):
            return "added"
        if not self.confirmed and any(f in CHECK_FLAGS for f in self.flags):
            return "check"
        return "ok"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["symbol_boxes"] = [list(b) for b in self.symbol_boxes]
        d["row_box"] = list(self.row_box) if self.row_box else None
        d["status"] = self.status
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LegendEntry":
        d = dict(d)
        d.pop("status", None)
        d["symbol_boxes"] = [tuple(float(v) for v in b) for b in d.get("symbol_boxes", [])]
        d["row_box"] = tuple(float(v) for v in d["row_box"]) if d.get("row_box") else None
        return cls(**d)


@dataclass
class LegendRow:
    """A legend row's text, kept so a symbol added by hand can take its
    description from the row it sits in."""

    row_box: Box
    description: str
    section: Optional[str] = None


@dataclass
class Legend:
    pdf_sha256: str
    page_index: int
    coordinate_frame: Dict[str, Any]
    heading: str
    region: Box
    entries: List[LegendEntry]
    rows: List[LegendRow] = field(default_factory=list)
    engineer: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    edits: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    _next_id: int = 0

    def __post_init__(self) -> None:
        nums = [int(e.id[1:]) for e in self.entries if re.fullmatch(r"L\d+", e.id)]
        self._next_id = max([self._next_id, *nums], default=0)
        self.recheck_tags()

    # -- lookups --------------------------------------------------------

    def entry(self, entry_id: str) -> LegendEntry:
        for e in self.entries:
            if e.id == entry_id:
                return e
        raise LegendError("unknown_entry", f"There is no legend entry {entry_id!r}.")

    def counted(self) -> List[LegendEntry]:
        return [e for e in self.entries if e.count]

    def needs_review(self) -> List[LegendEntry]:
        return [e for e in self.entries if e.status == "check"]

    def by_tag(self) -> Dict[str, LegendEntry]:
        return {e.tag: e for e in self.entries}

    def _new_id(self) -> str:
        self._next_id += 1
        return f"L{self._next_id}"

    def _log(self, op: str, entry_id: Optional[str], **details: Any) -> None:
        self.edits.append({"op": op, "entry": entry_id, "at": _now(), **details})

    def recheck_tags(self) -> None:
        seen: Dict[str, int] = {}
        for e in self.entries:
            seen[e.tag] = seen.get(e.tag, 0) + 1
        for e in self.entries:
            dup = seen[e.tag] > 1
            if dup and "duplicate_tag" not in e.flags:
                e.flags.append("duplicate_tag")
                e.confirmed = False
            elif not dup and "duplicate_tag" in e.flags:
                e.flags.remove("duplicate_tag")

    # -- edits ----------------------------------------------------------

    def rename(self, entry_id: str, name: str) -> LegendEntry:
        e = self.entry(entry_id)
        name = name.strip()
        if not name:
            raise LegendError("invalid_name", "A symbol name can't be empty.")
        self._log("rename", entry_id, old=e.name, new=name)
        e.name = name
        if "wrapped" in e.flags:
            e.confirmed = True  # checking the joined name was the point of the flag
        return e

    def set_tag(self, entry_id: str, tag: str) -> LegendEntry:
        e = self.entry(entry_id)
        tag = tag.strip().upper()
        if not tag or len(tag) > 8 or not re.fullmatch(r"[A-Z0-9][A-Z0-9\-./]*", tag):
            raise LegendError("invalid_tag", f"{tag!r} isn't a usable tag. Use up to 8 letters, digits, '-', '.' or '/'.")
        self._log("set_tag", entry_id, old=e.tag, new=tag)
        e.tag, e.tag_source = tag, "user"
        if "generated_tag" in e.flags:
            e.flags.remove("generated_tag")
        self.recheck_tags()
        return e

    def set_group(self, entry_id: str, group: str) -> LegendEntry:
        e = self.entry(entry_id)
        group = group.strip()
        if not group:
            raise LegendError("invalid_group", "A group name can't be empty.")
        self._log("set_group", entry_id, old=e.group, new=group)
        e.group = group
        return e

    def set_count(self, entry_id: str, count: bool, reason: Optional[str] = None) -> LegendEntry:
        e = self.entry(entry_id)
        self._log("set_count", entry_id, old=e.count, new=bool(count))
        e.count = bool(count)
        e.not_counted_reason = None if count else (reason or e.not_counted_reason or "Turned off by a person.")
        return e

    def confirm(self, entry_id: str) -> LegendEntry:
        """Accept an entry flagged for a look as it is."""
        e = self.entry(entry_id)
        self._log("confirm", entry_id, flags=list(e.flags))
        e.confirmed = True
        return e

    def delete(self, entry_id: str) -> None:
        e = self.entry(entry_id)
        self._log("delete", entry_id, tag=e.tag, name=e.name)
        self.entries.remove(e)
        self.recheck_tags()

    def split(self, entry_id: str) -> List[LegendEntry]:
        """Turn an entry with several symbol drawings into one entry per
        drawing. A description written as ``A / B`` is split the same way."""
        e = self.entry(entry_id)
        k = len(e.symbol_boxes)
        if k < 2:
            raise LegendError("cannot_split", f"{e.name} has only one symbol drawing, so there is nothing to split.")
        parts = [p.strip() for p in re.split(r"\s+/\s+", e.description) if p.strip()]
        if len(parts) != k:
            parts = [e.description] * k
        order = sorted(range(k), key=lambda i: e.symbol_boxes[i][0])
        new: List[LegendEntry] = []
        taken = [x.tag for x in self.entries if x is not e]
        for n, i in enumerate(order):
            desc = re.sub(r"\((LEFT|RIGHT|TOP|BOTTOM|UPPER|LOWER)\)", "", parts[n], flags=re.I).strip(" ,")
            name = nice_name(desc)
            if n == 0 and e.tag_source != "generated":
                tag, src = e.tag, e.tag_source
            else:
                tag, src = generated_tag(name, taken), "generated"
            taken.append(tag)
            new.append(
                LegendEntry(
                    id=self._new_id(), tag=tag, tag_source=src, name=name, description=desc, group=e.group,
                    count=e.count, not_counted_reason=e.not_counted_reason, symbol_boxes=[e.symbol_boxes[i]],
                    labels=list(e.labels) if n == 0 else [], row_box=e.row_box,
                    flags=[f for f in e.flags if f not in ("combined", "wrapped", "duplicate_tag")] + (["generated_tag"] if src == "generated" else []),
                    source="user", confirmed=True,
                    signatures=[e.signatures[i]] if i < len(e.signatures) else [],
                )
            )
        at = self.entries.index(e)
        self.entries[at : at + 1] = new
        self._log("split", entry_id, into=[x.id for x in new])
        self.recheck_tags()
        return new

    def merge(self, entry_id: str, into_id: str) -> LegendEntry:
        """Count ``entry_id``'s drawings as ``into_id`` (two drawings of one symbol)."""
        if entry_id == into_id:
            raise LegendError("cannot_merge", "Choose a different symbol to merge into.")
        src, dst = self.entry(entry_id), self.entry(into_id)
        dst.symbol_boxes.extend(src.symbol_boxes)
        dst.signatures.extend(src.signatures)
        dst.labels.extend(l for l in src.labels if l not in dst.labels)
        dst.count = dst.count or src.count
        self.entries.remove(src)
        self._log("merge", entry_id, into=into_id)
        self.recheck_tags()
        return dst

    def row_at(self, y: float) -> Optional[LegendRow]:
        for r in self.rows:
            if r.row_box[1] - 1 <= y <= r.row_box[3] + 1:
                return r
        return None

    def add(
        self,
        box: Box,
        *,
        name: Optional[str] = None,
        tag: Optional[str] = None,
        group: Optional[str] = None,
        signature: Optional[str] = None,
        force: bool = False,
    ) -> LegendEntry:
        """Add a symbol Pinny missed, from a box drawn around its drawing on
        the legend page. Its description comes from the row it sits in."""
        x0, y0, x1, y1 = (float(v) for v in box)
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise LegendError("invalid_box", "Draw a box around the whole symbol drawing.")
        row = self.row_at((y0 + y1) / 2)
        if row is not None and not force:
            for e in self.entries:
                if e.row_box == row.row_box and e.source == "read":
                    raise LegendError(
                        "row_already_read",
                        f"That row is already read as {e.name} ({e.tag}). Edit that symbol, or pass force=True to add another.",
                    )
        description = row.description if row else ""
        nm = name or (nice_name(description) if description else "New symbol")
        taken = [e.tag for e in self.entries]
        if tag:
            tag_value, src = tag.strip().upper(), "user"
        else:
            tag_value, src = generated_tag(nm, taken), "generated"
        e = LegendEntry(
            id=self._new_id(), tag=tag_value, tag_source=src, name=nm, description=description,
            group=group or (row.section if row and row.section else guess_group(description)),
            symbol_boxes=[(x0, y0, x1, y1)], row_box=row.row_box if row else None,
            flags=["generated_tag"] if src == "generated" else [], source="user", confirmed=True,
            signatures=[signature] if signature else [],
        )
        self.entries.append(e)
        self._log("add", e.id, box=[x0, y0, x1, y1])
        self.recheck_tags()
        return e

    # -- persistence ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "pdf_sha256": self.pdf_sha256,
            "page_index": self.page_index,
            "coordinate_frame": self.coordinate_frame,
            "engineer": self.engineer,
            "heading": self.heading,
            "region": list(self.region),
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
            "rows": [{"row_box": list(r.row_box), "description": r.description, "section": r.section} for r in self.rows],
            "warnings": list(self.warnings),
            "edits": list(self.edits),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Legend":
        if d.get("format") != FORMAT:
            raise LegendError("not_a_legend", f"Not a Pinny legend file (format={d.get('format')!r}).")
        if not isinstance(d.get("format_version"), int) or d["format_version"] > FORMAT_VERSION:
            raise LegendError("unsupported_version", f"Legend format version {d.get('format_version')!r} is newer than this Pinny reads.")
        return cls(
            pdf_sha256=d["pdf_sha256"],
            page_index=int(d["page_index"]),
            coordinate_frame=d["coordinate_frame"],
            heading=d.get("heading", ""),
            region=tuple(float(v) for v in d["region"]),
            entries=[LegendEntry.from_dict(e) for e in d["entries"]],
            rows=[LegendRow(tuple(float(v) for v in r["row_box"]), r["description"], r.get("section")) for r in d.get("rows", [])],
            engineer=d.get("engineer"),
            warnings=list(d.get("warnings", [])),
            edits=list(d.get("edits", [])),
            created_at=d.get("created_at", _now()),
        )

    def save(self, path: "str | os.PathLike[str]") -> None:
        tmp = f"{os.fspath(path)}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: "str | os.PathLike[str]") -> "Legend":
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegendError("unreadable_legend", f"Can't read legend file {os.fspath(path)!r}: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, LegendError):
                raise
            raise LegendError("unreadable_legend", f"Legend file {os.fspath(path)!r} is missing a field: {exc!r}") from exc


_GROUP_RULES = [
    ("Wiring", r"\b(HOME ?RUN|CONDUIT|WIRING|RACEWAY|CABLE TRAY|CIRCUIT RUN)\b"),
    ("Receptacles", r"\b(RECEPTACLE|OUTLET, (DUPLEX|QUAD)|DUPLEX|QUADRUPLEX|GFCI|GFI)\b"),
    ("Low voltage", r"\b(DATA|TELEPHONE|TEL|VOICE|CATV|TV OUTLET|SPEAKER|CAMERA|CARD READER|ACCESS CONTROL|WAP|WIRELESS)\b"),
    ("Fire alarm", r"\b(SMOKE|HEAT DETECTOR|PULL STATION|HORN|STROBE|FIRE ALARM|FACP)\b"),
    ("Lighting", r"\b(LUMINAIRE|FIXTURE|LIGHT|EXIT SIGN|SWITCH, (SINGLE|3-WAY|THREE)|DIMMER|OCCUPANCY SENSOR|VACANCY SENSOR)\b"),
    ("Equipment", r"\b(PANEL|PANELBOARD|SWITCHBOARD|TRANSFORMER|DISCONNECT|MOTOR|STARTER|CONTACTOR|GENERATOR|ATS|UPS)\b"),
    ("Power", r"\b(JUNCTION BOX|J-BOX|POWER POLE|FLOOR BOX|POKE-THROUGH|CONNECTION)\b"),
]


def guess_group(description: str) -> str:
    up = description.upper()
    for group, pattern in _GROUP_RULES:
        if re.search(pattern, up):
            return group
    return "Other"
