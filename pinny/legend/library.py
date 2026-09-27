"""Saved legends, one per engineer, reused across projects.

When a new drawing set comes in, its legend is compared with the engineer's
saved one: each symbol is a match, new, or changed (different wording or a
different drawing). Saved symbols missing from this set are listed too.
After review, the corrected legend updates the saved one, so the next
project starts from it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .errors import LegendError
from .model import Legend, LegendEntry
from .reader import signature_similarity

FORMAT = "pinny.legend_library"
FORMAT_VERSION = 1
#: Below this signature similarity a symbol's drawing counts as changed.
DRAWING_CHANGED_BELOW = 0.6


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


@dataclass
class LibraryEntry:
    tag: str
    name: str
    description: str
    group: str
    count: bool = True
    signatures: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Difference:
    entry_id: Optional[str]
    tag: str
    name: str
    #: "match", "new", "changed" or "missing" (saved, not in this legend).
    kind: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class EngineerLibrary:
    engineer: str
    entries: List[LibraryEntry] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)

    def _find(self, e: LegendEntry) -> Optional[LibraryEntry]:
        for le in self.entries:
            if le.tag == e.tag and e.tag_source != "generated":
                return le
        d = _norm(e.description)
        for le in self.entries:
            if d and _norm(le.description) == d:
                return le
        return None

    def _pair(self, legend: Legend) -> Dict[str, Optional[LibraryEntry]]:
        """Match legend entries to saved ones: exact tag or wording first,
        then, for what's left, similar wording and drawing."""
        pairs: Dict[str, Optional[LibraryEntry]] = {}
        used: set = set()
        for e in legend.entries:
            le = self._find(e)
            if le is not None and id(le) not in used:
                pairs[e.id] = le
                used.add(id(le))
        for e in legend.entries:
            if e.id in pairs:
                continue
            best, best_score = None, 0.0
            words = set(_norm(e.description).split())
            for le in self.entries:
                if id(le) in used:
                    continue
                other = set(_norm(le.description).split())
                jac = len(words & other) / len(words | other) if words | other else 0.0
                sig = max((signature_similarity(a, b) for a in le.signatures for b in e.signatures), default=0.0)
                score = jac + 0.5 * sig
                if (jac >= 0.5 or (jac >= 0.3 and sig >= 0.9)) and score > best_score:
                    best, best_score = le, score
            pairs[e.id] = best
            if best is not None:
                used.add(id(best))
        return pairs

    def compare(self, legend: Legend) -> List[Difference]:
        out: List[Difference] = []
        used = set()
        pairs = self._pair(legend)
        for e in legend.entries:
            le = pairs.get(e.id)
            if le is None:
                out.append(Difference(e.id, e.tag, e.name, "new", ["Not in the saved legend."]))
                continue
            used.add(id(le))
            notes = []
            if _norm(le.description) != _norm(e.description):
                notes.append(f"Wording changed: was “{le.description}”.")
            if le.tag != e.tag:
                notes.append(f"Tag changed: was {le.tag}.")
            if le.signatures and e.signatures:
                best = max(signature_similarity(a, b) for a in le.signatures for b in e.signatures)
                if best < DRAWING_CHANGED_BELOW:
                    notes.append("The symbol is drawn differently from the saved one.")
            if le.count != e.count:
                notes.append(f"Saved legend {'counts' if le.count else 'does not count'} this symbol.")
            out.append(Difference(e.id, e.tag, e.name, "changed" if notes else "match", notes))
        for le in self.entries:
            if id(le) not in used:
                out.append(Difference(None, le.tag, le.name, "missing", ["In the saved legend but not in this set."]))
        return out

    def update_from(self, legend: Legend, project: Optional[str] = None) -> None:
        """Take this legend's (reviewed) symbols into the saved legend. Saved
        symbols this set doesn't use are kept."""
        pairs = self._pair(legend)
        for e in legend.entries:
            le = pairs.get(e.id)
            if le is None:
                le = LibraryEntry(e.tag, e.name, e.description, e.group, e.count)
                self.entries.append(le)
            le.tag, le.name, le.description, le.group, le.count = e.tag, e.name, e.description, e.group, e.count
            for s in e.signatures:
                if s and s not in le.signatures:
                    le.signatures.append(s)
            le.signatures = le.signatures[-8:]
            if project and project not in le.projects:
                le.projects.append(project)
            le.updated_at = _now()
        self.updated_at = _now()

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "engineer": self.engineer,
            "updated_at": self.updated_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EngineerLibrary":
        if d.get("format") != FORMAT:
            raise LegendError("not_a_library", f"Not a Pinny legend library (format={d.get('format')!r}).")
        if not isinstance(d.get("format_version"), int) or d["format_version"] > FORMAT_VERSION:
            raise LegendError("unsupported_version", "This legend library was written by a newer Pinny.")
        return cls(d["engineer"], [LibraryEntry(**e) for e in d.get("entries", [])], d.get("updated_at", _now()))

    def save(self, path: "str | os.PathLike[str]") -> None:
        tmp = f"{os.fspath(path)}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: "str | os.PathLike[str]") -> "EngineerLibrary":
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LegendError("unreadable_library", f"Can't read legend library {os.fspath(path)!r}: {exc}") from exc


def compare(legend: Legend, library: Optional[EngineerLibrary]) -> Dict[str, List[Difference]]:
    """Differences grouped by kind; everything is "new" without a library."""
    diffs = library.compare(legend) if library else [Difference(e.id, e.tag, e.name, "new") for e in legend.entries]
    out: Dict[str, List[Difference]] = {"match": [], "new": [], "changed": [], "missing": []}
    for d in diffs:
        out[d.kind].append(d)
    return out
