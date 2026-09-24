"""Model artifact save/load, ``pinny.model`` v1 (docs/phase2-contracts.md P5).

Owned by chat B. This is a minimal version written by the detector chat
before B landed; it implements exactly the P5 behaviour and the
coordinator reconciles it with B's version.

Layout: ``<models_dir>/<model_id>/{model.json, weights.pt}``. Weights are a
``state_dict`` only and are loaded with ``torch.load(weights_only=True)``
after the sha256 in ``model.json`` is checked. Artifacts are immutable:
saving never overwrites an existing ``model_id``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pinny.errors import ConflictError, NotFoundError, PinnyError

FORMAT = "pinny.model"
FORMAT_VERSION = 1
KINDS = ("verifier", "detector")


class ArtifactError(PinnyError):
    pass


def default_models_dir() -> Path:
    return Path(os.environ.get("PINNY_DATA_DIR", "pinny-data")) / "models"


def code_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                             cwd=Path(__file__).resolve().parent).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"git:{sha or 'unknown'}"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def save_model(models_dir: str | os.PathLike, *, kind: str, arch: str, state_dict: Mapping[str, Any],
               input: Mapping[str, Any], dataset_id: str, synthetic_only: bool,
               train_config: Mapping[str, Any], operating_point: Mapping[str, Any],
               metrics: Mapping[str, Any], created_at: dt.datetime | None = None) -> Path:
    """Write a new immutable artifact and return its directory."""
    import torch

    if kind not in KINDS:
        raise ArtifactError("invalid_model_kind", f"kind must be one of {KINDS}, got {kind!r}")
    buf = io.BytesIO()
    torch.save(dict(state_dict), buf)
    weights = buf.getvalue()
    sha = hashlib.sha256(weights).hexdigest()
    now = created_at or _utc_now()
    model_id = f"{kind}-{now.strftime('%Y%m%dT%H%M%SZ')}-{sha[:8]}"
    meta = {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "model_id": model_id, "kind": kind, "arch": arch,
        "input": dict(input), "dataset_id": dataset_id, "synthetic_only": bool(synthetic_only),
        "train_config": dict(train_config), "weights_sha256": sha, "code_version": code_version(),
        "operating_point": dict(operating_point), "metrics": dict(metrics),
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }
    root = Path(models_dir)
    final = root / model_id
    if final.exists():
        raise ConflictError("model_exists", f"model {model_id} already exists; artifacts are immutable")
    tmp = root / f".tmp-{model_id}-{os.getpid()}"
    tmp.mkdir(parents=True)
    (tmp / "weights.pt").write_bytes(weights)
    (tmp / "model.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, final)
    return final


def read_metadata(model_dir: str | os.PathLike) -> dict[str, Any]:
    path = Path(model_dir) / "model.json"
    if not path.is_file():
        raise NotFoundError("model_not_found", f"no model.json in {model_dir}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError("model_unreadable", f"cannot read {path}: {exc}") from exc
    if meta.get("format") != FORMAT or meta.get("format_version") != FORMAT_VERSION:
        raise ArtifactError("model_format_unsupported",
                            f"{path} is not {FORMAT} v{FORMAT_VERSION}")
    return meta


def load_model(model_dir: str | os.PathLike, *, kind: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(metadata, state_dict)`` after verifying ``weights_sha256``."""
    import torch

    meta = read_metadata(model_dir)
    if kind is not None and meta.get("kind") != kind:
        raise ArtifactError("model_kind_mismatch", f"expected a {kind} model, got {meta.get('kind')!r}")
    wpath = Path(model_dir) / "weights.pt"
    if not wpath.is_file():
        raise NotFoundError("model_weights_missing", f"no weights.pt in {model_dir}")
    data = wpath.read_bytes()
    if hashlib.sha256(data).hexdigest() != meta.get("weights_sha256"):
        raise ArtifactError("model_weights_corrupt",
                            f"{wpath} does not match weights_sha256 in model.json; retrain or restore it")
    state = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
    return meta, state
