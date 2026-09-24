"""Model artifacts, ``pinny.model`` v1 (docs/phase2-contracts.md P5).

Shared by every learned model (verifier, point detector). An artifact is a
directory ``<models_dir>/<model_id>/`` holding ``model.json`` (metadata)
and ``weights.pt`` (a ``state_dict`` only). Artifacts are immutable:
:func:`save_artifact` refuses to overwrite, and retraining writes a new
``model_id``.

Weights are loaded only after their sha256 matches ``weights_sha256``, and
only with ``torch.load(..., weights_only=True)``, from the exact bytes that
were hashed. Nothing else is ever unpickled.

torch is imported lazily so that ``read_metadata`` works without it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pinny.errors import PinnyError

FORMAT = "pinny.model"
FORMAT_VERSION = 1
METADATA_FILE = "model.json"
WEIGHTS_FILE = "weights.pt"
KINDS = ("verifier", "detector")

REQUIRED_KEYS = (
    "format", "format_version", "model_id", "kind", "arch", "input", "dataset_id",
    "synthetic_only", "train_config", "weights_sha256", "code_version",
    "operating_point", "metrics", "created_at",
)
_MODEL_ID_RE = re.compile(r"^(verifier|detector)-\d{8}T\d{6}Z-[0-9a-f]{8}$")


class ModelArtifactError(PinnyError):
    """A model artifact is missing, malformed or fails its integrity check."""


def default_models_dir() -> Path:
    """``<data dir>/models``, beside the learning store (``$PINNY_DATA_DIR``)."""
    from pinny.learning import default_data_dir

    return Path(default_data_dir()) / "models"


def git_code_version() -> str:
    """``git:<sha>`` of the working tree this code runs from, or ``git:unknown``."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        sha = ""
    return f"git:{sha}" if sha else "git:unknown"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_model_id(kind: str, weights_sha256: str, when: dt.datetime) -> str:
    stamp = when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{stamp}-{weights_sha256[:8]}"


def serialize_state_dict(state_dict: Mapping[str, Any]) -> bytes:
    """``torch.save`` a state_dict (tensors moved to CPU) into bytes."""
    import torch

    clean = {str(k): v.detach().cpu().clone() if isinstance(v, torch.Tensor) else v
             for k, v in state_dict.items()}
    buf = io.BytesIO()
    torch.save(clean, buf)
    return buf.getvalue()


def save_artifact(models_dir: str | os.PathLike, *, kind: str, arch: str,
                  state_dict: Mapping[str, Any], metadata: Mapping[str, Any],
                  created_at: dt.datetime | None = None) -> Path:
    """Write a new artifact and return its directory.

    ``metadata`` supplies the model-specific fields (``input``, ``dataset_id``,
    ``synthetic_only``, ``train_config``, ``operating_point``, ``metrics`` and
    optionally ``code_version``). ``format``, ``model_id``, ``kind``, ``arch``,
    ``weights_sha256`` and ``created_at`` are filled in here. The directory is
    written under a temporary name and renamed into place, so a reader never
    sees a half-written artifact.
    """
    if kind not in KINDS:
        raise ModelArtifactError("invalid_model_kind", f"kind must be one of {KINDS}, got {kind!r}.")
    when = (created_at or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)
    weights = serialize_state_dict(state_dict)
    digest = sha256_hex(weights)
    model_id = make_model_id(kind, digest, when)

    meta: dict[str, Any] = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "model_id": model_id,
        "kind": kind, "arch": arch,
        "input": {}, "dataset_id": None, "synthetic_only": False, "train_config": {},
        "weights_sha256": digest, "code_version": git_code_version(),
        "operating_point": {}, "metrics": {"val": {}, "test": {}},
        "created_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key, value in metadata.items():
        if key in ("format", "format_version", "model_id", "kind", "arch", "weights_sha256", "created_at"):
            raise ModelArtifactError("reserved_metadata_key", f"{key!r} is set by save_artifact, not by the caller.")
        meta[key] = value
    _validate_metadata(meta)

    root = Path(models_dir)
    root.mkdir(parents=True, exist_ok=True)
    final = root / model_id
    if final.exists():
        raise ModelArtifactError("model_exists", f"Model {model_id} already exists at {final}; artifacts are immutable.")
    tmp = Path(tempfile.mkdtemp(prefix=f".{model_id}.", dir=root))
    try:
        (tmp / WEIGHTS_FILE).write_bytes(weights)
        (tmp / METADATA_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def read_metadata(model_dir: str | os.PathLike) -> dict[str, Any]:
    """Read and validate ``model.json``. Does not touch the weights."""
    path = Path(model_dir) / METADATA_FILE
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModelArtifactError("model_not_found", f"No {METADATA_FILE} in {model_dir}.") from None
    except (OSError, ValueError) as exc:
        raise ModelArtifactError("invalid_model_metadata", f"Cannot read {path}: {exc}") from None
    if not isinstance(meta, dict):
        raise ModelArtifactError("invalid_model_metadata", f"{path} must hold a JSON object.")
    _validate_metadata(meta)
    return meta


def read_verified_weights(model_dir: str | os.PathLike, meta: Mapping[str, Any] | None = None) -> bytes:
    """Return the raw ``weights.pt`` bytes after checking them against ``weights_sha256``."""
    meta = meta if meta is not None else read_metadata(model_dir)
    path = Path(model_dir) / WEIGHTS_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ModelArtifactError("model_weights_missing", f"No {WEIGHTS_FILE} in {model_dir}.") from None
    actual = sha256_hex(data)
    if actual != meta["weights_sha256"]:
        raise ModelArtifactError(
            "model_weights_mismatch",
            f"{path} has sha256 {actual}, but model.json records {meta['weights_sha256']}. "
            "The weights were modified or corrupted; retrain or restore the artifact.")
    return data


def load_artifact(model_dir: str | os.PathLike, *, kind: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(metadata, state_dict)``, verified. ``kind`` (if given) must match."""
    import torch

    meta = read_metadata(model_dir)
    if kind is not None and meta["kind"] != kind:
        raise ModelArtifactError("wrong_model_kind", f"{model_dir} holds a {meta['kind']!r} model, expected {kind!r}.")
    data = read_verified_weights(model_dir, meta)
    try:
        state = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    except Exception as exc:  # noqa: BLE001 - any unpickling refusal is a bad artifact
        raise ModelArtifactError("invalid_model_weights", f"Cannot load weights from {model_dir}: {exc}") from None
    if not isinstance(state, dict):
        raise ModelArtifactError("invalid_model_weights", f"{model_dir}/{WEIGHTS_FILE} is not a state_dict.")
    return meta, state


def _validate_metadata(meta: Mapping[str, Any]) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in meta]
    if missing:
        raise ModelArtifactError("invalid_model_metadata", f"model.json is missing {missing}.")
    if meta["format"] != FORMAT or meta["format_version"] != FORMAT_VERSION:
        raise ModelArtifactError(
            "unsupported_model_format",
            f"Expected {FORMAT} v{FORMAT_VERSION}, got {meta['format']!r} v{meta['format_version']!r}.")
    if meta["kind"] not in KINDS:
        raise ModelArtifactError("invalid_model_kind", f"kind must be one of {KINDS}, got {meta['kind']!r}.")
    if not _MODEL_ID_RE.fullmatch(str(meta["model_id"])) or not str(meta["model_id"]).startswith(meta["kind"] + "-"):
        raise ModelArtifactError("invalid_model_id", f"Malformed model_id {meta['model_id']!r}.")
    digest = str(meta["weights_sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not str(meta["model_id"]).endswith(digest[:8]):
        raise ModelArtifactError("invalid_model_metadata", "weights_sha256 is malformed or does not match model_id.")
