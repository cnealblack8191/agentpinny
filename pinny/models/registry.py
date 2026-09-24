"""Model registry and promotion (docs/phase2-contracts.md P8).

Stdlib only: the registry never imports torch, so the viewer can ask which
models are active on a base install.

Layout under ``$PINNY_DATA_DIR/models/``::

    <model_id>/model.json        pinny.model v1 metadata (P5), immutable
    <model_id>/weights.pt        state_dict (only the model classes load it)
    active.json                  the promoted model per kind (written atomically)
    promotions/<model_id>-<sha8>.json   a copy of each accepted promotion report

``promote`` is the only way to make a model active. It refuses unless the
evidence file is a ``pinny.promotion`` v1 report that names this model and
says ``"promote": true`` (P9).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from pinny.errors import PinnyError

MODEL_FORMAT = "pinny.model"
PROMOTION_FORMAT = "pinny.promotion"
ACTIVE_FORMAT = "pinny.models.active"
FORMAT_VERSION = 1
KINDS = ("verifier", "detector")

#: The scan mode (P7) whose benchmark result can promote each kind.
MODE_FOR_KIND = {"verifier": "template+verifier", "detector": "model"}

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_LOCK = threading.Lock()


class RegistryError(PinnyError):
    """A registry call was refused. ``code`` says why; the message says what to do."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(code, message)
        self.http_status = http_status


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _default_data_dir() -> Path:
    from pinny.learning.store import default_data_dir  # stdlib-only module
    return default_data_dir()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise RegistryError("invalid_model_kind",
                            f"Model kind must be one of {', '.join(KINDS)}; got {kind!r}.")


def _check_model_id(model_id: Any) -> str:
    if not isinstance(model_id, str) or not _MODEL_ID.match(model_id):
        raise RegistryError("invalid_model_id", f"{model_id!r} is not a valid model id.")
    return model_id


def _write_json_atomic(path: Path, obj: Any) -> None:
    """Write ``obj`` so a reader sees either the old file or the new one, never
    a partial write: temp file in the same directory, fsync, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:  # make the rename itself durable (not supported on Windows)
        dfd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _report_model_ids(report: Dict[str, Any]) -> List[str]:
    """Every model id a promotion report names as its candidate. Accepts
    ``model_id``/``model_ids`` at the top level or under ``candidate``, and
    ``+``-joined ids (P7 ``detector.version``)."""
    ids: List[str] = []
    for src in (report, report.get("candidate") if isinstance(report.get("candidate"), dict) else {}):
        vals = [src.get("model_id")]
        many = src.get("model_ids")
        if isinstance(many, list):
            vals.extend(many)
        for v in vals:
            if isinstance(v, str):
                ids.extend(p for p in v.split("+") if p)
    return ids


def _report_mode(report: Dict[str, Any]) -> Optional[str]:
    cand = report.get("candidate")
    if isinstance(cand, dict) and isinstance(cand.get("mode"), str):
        return cand["mode"]
    for key in ("candidate_mode", "mode"):
        if isinstance(report.get(key), str):
            return report[key]
    return None


class ModelRegistry:
    """The models under one data directory."""

    def __init__(self, data_dir: Optional[os.PathLike] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
        self.root = self.data_dir / "models"
        self.active_path = self.root / "active.json"

    # ---------------------------------------------------------------- models
    def model_dir(self, model_id: str) -> Path:
        return self.root / _check_model_id(model_id)

    def list_models(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Metadata of every readable ``pinny.model`` v1 artifact, oldest first."""
        if kind is not None:
            _check_kind(kind)
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not _MODEL_ID.match(d.name):
                continue
            try:
                meta = self._read_meta(d.name)
            except RegistryError:
                continue  # half-written or foreign directory; get() reports why
            if kind is None or meta["kind"] == kind:
                out.append(meta)
        out.sort(key=lambda m: (str(m.get("created_at", "")), m["model_id"]))
        return out

    def get(self, model_id: str) -> Dict[str, Any]:
        """Artifact metadata (``model.json``) of one model."""
        return self._read_meta(_check_model_id(model_id))

    def _read_meta(self, model_id: str) -> Dict[str, Any]:
        path = self.root / model_id / "model.json"
        if not path.is_file():
            raise RegistryError("unknown_model",
                                f"No model {model_id!r} in {self.root}.", 404)
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError("invalid_model", f"{path} is not readable JSON: {exc}.") from None
        if not isinstance(meta, dict) or meta.get("format") != MODEL_FORMAT \
                or meta.get("format_version") != FORMAT_VERSION:
            raise RegistryError("invalid_model",
                                f"{path} is not a {MODEL_FORMAT} v{FORMAT_VERSION} artifact.")
        if meta.get("model_id") != model_id:
            raise RegistryError("invalid_model",
                                f"{path} says model_id {meta.get('model_id')!r}, but its "
                                f"directory is {model_id!r}.")
        if meta.get("kind") not in KINDS:
            raise RegistryError("invalid_model", f"{path} has unknown kind {meta.get('kind')!r}.")
        return meta

    # ---------------------------------------------------------------- active
    def _read_active(self) -> Dict[str, Any]:
        if not self.active_path.exists():
            return {"format": ACTIVE_FORMAT, "format_version": FORMAT_VERSION,
                    "active": {}, "history": []}
        try:
            doc = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError("registry_unreadable",
                                f"{self.active_path} is not readable JSON ({exc}). Fix or "
                                "delete it, then promote the models again.", 500) from None
        if not isinstance(doc, dict) or doc.get("format") != ACTIVE_FORMAT \
                or not isinstance(doc.get("active"), dict):
            raise RegistryError("registry_unreadable",
                                f"{self.active_path} is not a {ACTIVE_FORMAT} file.", 500)
        doc.setdefault("history", [])
        return doc

    def active(self, kind: str) -> Optional[str]:
        """The promoted ``model_id`` for ``kind``, or ``None``."""
        _check_kind(kind)
        entry = self._read_active()["active"].get(kind)
        return entry.get("model_id") if isinstance(entry, dict) else None

    def active_entry(self, kind: str) -> Optional[Dict[str, Any]]:
        """The full ``active.json`` record for ``kind`` (who, when, evidence)."""
        _check_kind(kind)
        entry = self._read_active()["active"].get(kind)
        return dict(entry) if isinstance(entry, dict) else None

    def promote(self, model_id: str, evidence_path: os.PathLike) -> Dict[str, Any]:
        """Make ``model_id`` the active model of its kind.

        Refused unless ``evidence_path`` is a ``pinny.promotion`` v1 report for
        this model with ``"promote": true``, and the model's weights still
        match their recorded sha256. Returns the new ``active.json`` entry.
        """
        meta = self.get(model_id)
        kind = meta["kind"]
        report, raw = self._read_evidence(Path(evidence_path))
        if model_id not in _report_model_ids(report):
            named = ", ".join(_report_model_ids(report)) or "no model"
            raise RegistryError("evidence_wrong_model",
                                f"The promotion report is for {named}, not {model_id}.")
        mode = _report_mode(report)
        if mode is not None and mode != MODE_FOR_KIND[kind]:
            raise RegistryError("evidence_wrong_mode",
                                f"A {kind} is promoted on a {MODE_FOR_KIND[kind]!r} benchmark; "
                                f"this report is for mode {mode!r}.")
        if report.get("promote") is not True:
            why = report.get("reasons") or report.get("reason")
            raise RegistryError("promotion_not_recommended",
                                f"The promotion report does not recommend {model_id} "
                                f"(\"promote\": {json.dumps(report.get('promote'))})"
                                + (f": {why}" if why else "."))
        weights = self.root / model_id / "weights.pt"
        want = meta.get("weights_sha256")
        if not weights.is_file():
            raise RegistryError("invalid_model", f"{weights} is missing.")
        if not isinstance(want, str) or _sha256_file(weights) != want:
            raise RegistryError("weights_mismatch",
                                f"{weights} does not match the weights_sha256 in model.json. "
                                "The artifact was changed after training; retrain it.")

        evidence_sha = hashlib.sha256(raw).hexdigest()
        copy = self.root / "promotions" / f"{model_id}-{evidence_sha[:8]}.json"
        entry = {"model_id": model_id, "kind": kind, "promoted_at": _now(),
                 "evidence_path": str(Path(evidence_path).resolve()),
                 "evidence_copy": str(copy.relative_to(self.root)),
                 "evidence_sha256": evidence_sha,
                 "dataset_id": report.get("dataset_id", meta.get("dataset_id"))}
        with _LOCK:
            if not copy.exists():
                copy.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(copy, report)
            doc = self._read_active()
            previous = doc["active"].get(kind)
            doc["active"][kind] = entry
            doc["history"].append({"action": "promote", "at": entry["promoted_at"],
                                   "kind": kind, "model_id": model_id,
                                   "previous": previous.get("model_id")
                                   if isinstance(previous, dict) else None,
                                   "evidence_sha256": evidence_sha})
            doc["format"], doc["format_version"] = ACTIVE_FORMAT, FORMAT_VERSION
            _write_json_atomic(self.active_path, doc)
        return dict(entry)

    def deactivate(self, kind: str) -> Optional[str]:
        """Clear the active model of ``kind``; returns the id that was active."""
        _check_kind(kind)
        with _LOCK:
            doc = self._read_active()
            previous = doc["active"].pop(kind, None)
            if previous is None:
                return None
            doc["history"].append({"action": "deactivate", "at": _now(), "kind": kind,
                                   "model_id": previous.get("model_id")})
            _write_json_atomic(self.active_path, doc)
        return previous.get("model_id")

    @staticmethod
    def _read_evidence(path: Path):
        if not path.is_file():
            raise RegistryError("evidence_missing",
                                f"No promotion report at {path}. Run the benchmark first "
                                "(docs/phase2-contracts.md P9).")
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise RegistryError("evidence_invalid", f"{path} is too large to be a promotion report.")
        raw = path.read_bytes()
        try:
            report = json.loads(raw)
        except ValueError:
            raise RegistryError("evidence_invalid", f"{path} is not JSON.") from None
        if not isinstance(report, dict) or report.get("format") != PROMOTION_FORMAT \
                or report.get("format_version") != FORMAT_VERSION:
            raise RegistryError("evidence_invalid",
                                f"{path} is not a {PROMOTION_FORMAT} v{FORMAT_VERSION} report.")
        return report, raw


# ------------------------------------------------ module-level API (P8)
def _reg(data_dir: Optional[os.PathLike]) -> ModelRegistry:
    return ModelRegistry(data_dir)


def list_models(kind: Optional[str] = None, *, data_dir: Optional[os.PathLike] = None):
    return _reg(data_dir).list_models(kind)


def get(model_id: str, *, data_dir: Optional[os.PathLike] = None) -> Dict[str, Any]:
    return _reg(data_dir).get(model_id)


def active(kind: str, *, data_dir: Optional[os.PathLike] = None) -> Optional[str]:
    return _reg(data_dir).active(kind)


def promote(model_id: str, evidence_path: os.PathLike, *,
            data_dir: Optional[os.PathLike] = None) -> Dict[str, Any]:
    return _reg(data_dir).promote(model_id, evidence_path)


def deactivate(kind: str, *, data_dir: Optional[os.PathLike] = None) -> Optional[str]:
    return _reg(data_dir).deactivate(kind)


def main(argv=None) -> int:
    """``python -m pinny.models.registry list|active|promote|deactivate``."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m pinny.models.registry")
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--kind", choices=KINDS)
    sub.add_parser("active")
    p = sub.add_parser("promote")
    p.add_argument("model_id")
    p.add_argument("--evidence", required=True, help="pinny.promotion v1 report")
    p = sub.add_parser("deactivate")
    p.add_argument("kind", choices=KINDS)
    args = ap.parse_args(argv)
    reg = ModelRegistry(args.data_dir)
    try:
        if args.cmd == "list":
            for m in reg.list_models(args.kind):
                act = " (active)" if reg.active(m["kind"]) == m["model_id"] else ""
                thr = (m.get("operating_point") or {}).get("threshold")
                print(f"{m['model_id']}\t{m['kind']}\tthreshold={thr}"
                      f"\tsynthetic_only={m.get('synthetic_only')}{act}")
        elif args.cmd == "active":
            for k in KINDS:
                print(f"{k}\t{reg.active(k) or '-'}")
        elif args.cmd == "promote":
            e = reg.promote(args.model_id, args.evidence)
            print(f"{e['kind']} {e['model_id']} is now active.")
        else:
            print(f"deactivated {reg.deactivate(args.kind) or 'nothing'}")
    except PinnyError as exc:
        print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
