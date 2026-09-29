"""One learned package per symbol type, trained from the reviews.

A symbol type is a legend tag (the ``class_label`` pins from a whole-set
scan carry). :func:`train_symbol` builds that type's package from its
reviewed matches, checks it on reviews it was not trained on
(:mod:`.judge`) and says whether it may be switched on.

:class:`SymbolIndex` keeps the packages under ``<data>/models/symbols/``::

    index.json          {"format", "version", "symbols": {tag: entry}}
    <key>.pinny         the package of one tag (key: sha256 of the tag)

An entry records the package file and its sha256, when and from how many
reviews it was trained, the check, and whether it is active. Only active
packages are used by the whole-set scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .format import ModelPackageError
from .judge import CheckResult, Example, cross_check, gate
from .package import ModelPackage
from .train import train_model

INDEX_FORMAT = "pinny.symbol_models"
INDEX_VERSION = 1
SUBDIR = "symbols"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def symbol_key(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()[:32]


@dataclass
class SymbolTraining:
    package: Optional[ModelPackage]
    check: CheckResult
    active: bool
    reason: str
    notes: List[str] = field(default_factory=list)


def _package(tag: str, original: np.ndarray, examples: Sequence[Example], notes: Optional[List[str]] = None,
             provenance: Optional[Dict[str, Any]] = None) -> ModelPackage:
    res = train_model(
        name=f"symbol-{tag}", version=_now(), symbol_class=tag, original_template=original,
        positive_crops=[e.crop for e in examples if e.approved],
        negative_crops=[e.crop for e in examples if not e.approved],
        description=f"Learned from reviews of legend symbol {tag}.",
        renderer_version="pinny-canonical-200dpi", provenance=provenance or {})
    if notes is not None:
        notes.extend(res.notes)
    return res.package


def train_symbol(tag: str, original: np.ndarray, examples: Sequence[Example],
                 provenance: Optional[Dict[str, Any]] = None) -> SymbolTraining:
    """Train ``tag``'s package from its reviewed ``examples`` (§6 crops of
    approved and rejected machine matches); ``original`` is the legend's
    drawing of the symbol, as the whole-set scan cut it."""
    if not isinstance(tag, str) or not tag:
        raise ModelPackageError("invalid_model", "A symbol type needs a tag.")
    approved = sum(e.approved for e in examples)
    if not approved:
        return SymbolTraining(None, CheckResult(groups=len({e.group for e in examples}), approved=0,
                                                rejected=len(examples), skipped="needs approved matches"),
                              False, "Approve some correct matches of this symbol first.")
    check = cross_check(examples, lambda ex: _package(tag, original, ex))
    verdict = gate(check)
    notes: List[str] = []
    prov = dict(provenance or {})
    prov.update({"approved": approved, "rejected": len(examples) - approved,
                 "groups": len({e.group for e in examples}), "check": check.to_dict()})
    package = _package(tag, original, examples, notes, prov)
    return SymbolTraining(package, check, bool(verdict["ok"]), str(verdict["reason"]), notes)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SymbolIndex:
    """The packages of every symbol type in one data directory."""

    def __init__(self, data_dir: os.PathLike) -> None:
        self.root = Path(data_dir) / "models" / SUBDIR
        self.index_path = self.root / "index.json"

    def read(self) -> Dict[str, Dict[str, Any]]:
        try:
            d = json.loads(self.index_path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise ModelPackageError("invalid_index", f"{self.index_path} is not valid JSON.") from exc
        if d.get("format") != INDEX_FORMAT or d.get("version") != INDEX_VERSION:
            raise ModelPackageError("invalid_index", f"{self.index_path} is not a {INDEX_FORMAT} v{INDEX_VERSION} index.")
        return dict(d.get("symbols") or {})

    def _write(self, symbols: Dict[str, Dict[str, Any]]) -> None:
        body = {"format": INDEX_FORMAT, "version": INDEX_VERSION, "symbols": symbols}
        _atomic_write(self.index_path, json.dumps(body, indent=2, sort_keys=True).encode())

    def path(self, tag: str) -> Path:
        return self.root / f"{symbol_key(tag)}.pinny"

    def save(self, tag: str, training: SymbolTraining, *, trained_by: Optional[str] = None) -> Dict[str, Any]:
        """Store ``tag``'s newly trained package and its entry (replacing any
        earlier one). Returns the entry."""
        entry: Dict[str, Any] = {"tag": tag, "trained_at": _now(), "trained_by": trained_by,
                                 "active": training.active, "reason": training.reason,
                                 "check": training.check.to_dict(), "notes": list(training.notes),
                                 "file": None, "sha256": None, "templates": 0, "negatives": 0,
                                 "verifier": False}
        if training.package is not None:
            p = self.path(tag)
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".pinny")
            os.close(fd)
            try:
                training.package.save(tmp)
                data = Path(tmp).read_bytes()
                os.replace(tmp, p)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            entry.update(file=p.name, sha256=hashlib.sha256(data).hexdigest(),
                         templates=len(training.package.templates), negatives=len(training.package.negatives),
                         verifier=training.package.verifier is not None)
        symbols = self.read()
        symbols[tag] = entry
        self._write(symbols)
        return entry

    def set_active(self, tag: str, active: bool) -> Dict[str, Any]:
        """Switch a package off, or back on if it passed its check."""
        symbols = self.read()
        entry = symbols.get(tag)
        if entry is None or not entry.get("file"):
            raise ModelPackageError("unknown_symbol_model", f"No learned package for {tag} yet.")
        if active and not gate_ok(entry):
            raise ModelPackageError("symbol_model_not_checked", entry.get("reason") or "It did not pass its check.")
        entry["active"] = bool(active)
        entry["switched_at"] = _now()
        self._write(symbols)
        return entry

    def active(self) -> Dict[str, Dict[str, Any]]:
        """Active entries whose package file is present and unchanged."""
        out = {}
        for tag, e in self.read().items():
            if not e.get("active") or not e.get("file"):
                continue
            try:
                data = (self.root / e["file"]).read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() == e.get("sha256"):
                out[tag] = e
        return out


def gate_ok(entry: Dict[str, Any]) -> bool:
    """Whether an index entry's stored check passes the gate."""
    try:
        check = CheckResult(**entry["check"])
    except (KeyError, TypeError):
        return False
    return bool(gate(check)["ok"])
