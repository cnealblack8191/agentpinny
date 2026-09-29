"""The app's OCR setting, stored with Pinny's local data.

``$PINNY_DATA_DIR/settings.json`` holds ``{"ocr": {"engine": "<name>"}}``.
Other top-level keys belong to other modules and are preserved on write. A
missing file or key means OCR is off.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Union

from pinny.learning.store import default_data_dir

from .types import OcrError

SETTINGS_FILENAME = "settings.json"
PathLike = Union[str, "os.PathLike[str]"]


def settings_path(data_dir: Optional[PathLike] = None) -> Path:
    return Path(data_dir) if data_dir is not None else default_data_dir()


def _file(data_dir: Optional[PathLike]) -> Path:
    return settings_path(data_dir) / SETTINGS_FILENAME


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise OcrError("invalid_config", f"Could not read {path}: {exc}.") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OcrError("invalid_config", f"{path} is not valid JSON ({exc}). Fix or delete it.") from exc
    if not isinstance(data, dict):
        raise OcrError("invalid_config", f"{path} must contain a JSON object. Fix or delete it.")
    return data


def stored_engine(data_dir: Optional[PathLike] = None) -> Optional[str]:
    """The engine saved in the app settings, or ``None`` if never set."""
    path = _file(data_dir)
    ocr = _read(path).get("ocr")
    if ocr is None:
        return None
    engine = ocr.get("engine") if isinstance(ocr, dict) else None
    if not isinstance(engine, str):
        raise OcrError("invalid_config", f'{path}: "ocr.engine" must be a string. Fix or delete it.')
    return engine


def store_engine(name: str, data_dir: Optional[PathLike] = None) -> Path:
    """Save the engine choice atomically. The caller validates ``name``."""
    path = _file(data_dir)
    data = _read(path)
    ocr = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
    data["ocr"] = {**ocr, "engine": name}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path
