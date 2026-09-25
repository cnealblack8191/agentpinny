"""The ``.pinny`` model package file format (``pinny.model`` v1).

A package is a ZIP archive holding a ``manifest.json``, the component files it
lists, and an optional ``signature.json``:

```
manifest.json            what the model is, how to run it, where it came from
templates/000.png ...    template bank (grayscale or RGB PNG, native size)
negatives/000.png ...    rejected-crop bank for the negative veto (optional)
verifier/features.npy    kNN verifier examples (optional)
verifier/labels.npy
verifier/embedder.onnx   embedding model for an ONNX verifier (optional)
signature.json           HMAC-SHA256 over manifest.json (optional)
```

Integrity: ``manifest["files"]`` maps every component path to its sha256,
and the loader refuses a package whose files don't match, that has files the
manifest doesn't list, or that lists files it doesn't contain. The manifest
itself is covered by the optional signature. Archives are written
deterministically (sorted entries, fixed timestamps) so the same model gives
the same bytes and the same package digest.

Loading is defensive because packages travel between machines: entry names
are checked against an allow-list (no absolute paths, no ``..``), sizes are
capped before decompression, and arrays are read with ``allow_pickle=False``.
Nothing in a package is ever executed.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import zipfile
from typing import Dict, Mapping, Optional, Tuple

import numpy as np

FORMAT = "pinny.model"
FORMAT_VERSION = 1
#: The shared-contract version the package's inputs and outputs follow.
CONTRACTS_VERSION = "1.1"
FILE_EXTENSION = ".pinny"

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "signature.json"

#: Loader limits. A real package is a few MB; these stop hostile archives.
MAX_ENTRIES = 4096
MAX_ENTRY_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

_ENTRY_RE = re.compile(
    r"^(manifest\.json|signature\.json|templates/\d{3,6}\.png|negatives/\d{3,6}\.png|"
    r"verifier/(features|labels)\.npy|verifier/embedder\.onnx)$"
)
_FIXED_DATE = (2020, 1, 1, 0, 0, 0)


class ModelPackageError(ValueError):
    """A package can't be built, read or run. ``code`` is a stable id; the
    message says what to do. Same shape as the other Pinny module errors
    (``code``, message) until ``pinny.errors.PinnyError`` lands."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"ModelPackageError({self.code!r}, {str(self)!r})"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: object) -> bytes:
    """Stable JSON bytes: sorted keys, no NaN, UTF-8, trailing newline."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def encode_png(image: np.ndarray) -> bytes:
    import cv2

    img = np.ascontiguousarray(image)
    if img.dtype != np.uint8 or img.ndim not in (2, 3):
        raise ModelPackageError("invalid_image", "Package images must be uint8 (H, W) or (H, W, C) arrays.")
    if img.ndim == 3 and img.shape[2] == 3:
        img = img[:, :, ::-1]  # RGB -> BGR for OpenCV
    elif img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, [2, 1, 0, 3]]
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ModelPackageError("invalid_image", "Could not encode an image as PNG.")
    return buf.tobytes()


def decode_png(data: bytes, name: str) -> np.ndarray:
    import cv2

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.dtype != np.uint8:
        raise ModelPackageError("corrupt_package", f"{name} is not a valid 8-bit PNG.")
    if img.ndim == 3 and img.shape[2] == 3:
        img = img[:, :, ::-1]
    elif img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, [2, 1, 0, 3]]
    return np.ascontiguousarray(img)


def encode_npy(array: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, np.ascontiguousarray(array), allow_pickle=False)
    return buf.getvalue()


def decode_npy(data: bytes, name: str) -> np.ndarray:
    try:
        return np.load(io.BytesIO(data), allow_pickle=False)
    except (ValueError, OSError) as exc:
        raise ModelPackageError("corrupt_package", f"{name} is not a valid .npy array: {exc}") from exc


def sign(manifest_bytes: bytes, key: bytes) -> dict:
    if not key:
        raise ModelPackageError("invalid_key", "The signing key must not be empty.")
    return {
        "algorithm": "hmac-sha256",
        "manifest_sha256": sha256_hex(manifest_bytes),
        "mac": hmac.new(key, manifest_bytes, hashlib.sha256).hexdigest(),
    }


def verify_signature(manifest_bytes: bytes, signature: Optional[dict], key: bytes) -> None:
    if signature is None:
        raise ModelPackageError(
            "signature_missing", "The package is unsigned but a signature was required."
        )
    if signature.get("algorithm") != "hmac-sha256":
        raise ModelPackageError(
            "signature_unsupported", f"Unsupported signature algorithm {signature.get('algorithm')!r}."
        )
    expected = hmac.new(key, manifest_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature.get("mac", ""))):
        raise ModelPackageError(
            "signature_invalid",
            "The package signature does not match. It was modified or signed with a different key.",
        )


def write_archive(path: str, manifest: dict, files: Mapping[str, bytes], signing_key: Optional[bytes]) -> str:
    """Write the archive; return the package digest (sha256 of the file)."""
    for name in files:
        if not _ENTRY_RE.match(name) or name in (MANIFEST_NAME, SIGNATURE_NAME):
            raise ModelPackageError("invalid_entry", f"Invalid package entry name {name!r}.")
    manifest = dict(manifest)
    manifest["files"] = {name: sha256_hex(data) for name, data in sorted(files.items())}
    manifest_bytes = canonical_json(manifest)
    entries: Dict[str, bytes] = {MANIFEST_NAME: manifest_bytes, **files}
    if signing_key is not None:
        entries[SIGNATURE_NAME] = canonical_json(sign(manifest_bytes, signing_key))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, entries[name])
    data = buf.getvalue()
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return sha256_hex(data)


def read_archive(path: str) -> Tuple[dict, bytes, Optional[dict], Dict[str, bytes], str]:
    """Read and integrity-check an archive.

    Returns ``(manifest, manifest_bytes, signature, files, package_sha256)``.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ModelPackageError("package_not_found", f"Can't read model package {path!r}: {exc}") from exc
    digest = sha256_hex(raw)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ModelPackageError("corrupt_package", f"{path!r} is not a Pinny model package (not a ZIP).") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ModelPackageError("package_too_large", f"The package has more than {MAX_ENTRIES} entries.")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise ModelPackageError("corrupt_package", "The package contains duplicate entries.")
        total = 0
        for info in infos:
            if not _ENTRY_RE.match(info.filename):
                raise ModelPackageError("invalid_entry", f"Unexpected package entry {info.filename!r}.")
            if info.file_size > MAX_ENTRY_BYTES:
                raise ModelPackageError("package_too_large", f"Entry {info.filename!r} is too large.")
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise ModelPackageError("package_too_large", "The package's uncompressed size is too large.")
        if MANIFEST_NAME not in names:
            raise ModelPackageError("corrupt_package", "The package has no manifest.json.")
        if zf.getinfo(MANIFEST_NAME).file_size > MAX_MANIFEST_BYTES:
            raise ModelPackageError("package_too_large", "manifest.json is too large.")

        def read(name: str) -> bytes:
            data = zf.read(name)
            if len(data) != zf.getinfo(name).file_size:
                raise ModelPackageError("corrupt_package", f"Entry {name!r} has the wrong size.")
            return data

        manifest_bytes = read(MANIFEST_NAME)
        signature = None
        if SIGNATURE_NAME in names:
            signature = _parse_json(read(SIGNATURE_NAME), SIGNATURE_NAME)
        manifest = _parse_json(manifest_bytes, MANIFEST_NAME)
        check_header(manifest)
        listed = manifest.get("files")
        if not isinstance(listed, dict):
            raise ModelPackageError("corrupt_package", "manifest.json has no 'files' table.")
        present = set(names) - {MANIFEST_NAME, SIGNATURE_NAME}
        if set(listed) != present:
            missing = sorted(set(listed) - present)
            extra = sorted(present - set(listed))
            raise ModelPackageError(
                "corrupt_package",
                f"Package contents don't match its manifest (missing {missing}, unlisted {extra}).",
            )
        files: Dict[str, bytes] = {}
        for name in sorted(present):
            data = read(name)
            if sha256_hex(data) != listed[name]:
                raise ModelPackageError(
                    "corrupt_package", f"{name} does not match its sha256 in the manifest."
                )
            files[name] = data
    return manifest, manifest_bytes, signature, files, digest


def check_header(manifest: dict) -> None:
    if manifest.get("format") != FORMAT:
        raise ModelPackageError("not_a_model", f"Not a Pinny model package (format={manifest.get('format')!r}).")
    version = manifest.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ModelPackageError("corrupt_package", "manifest.json has no integer format_version.")
    if version > FORMAT_VERSION:
        raise ModelPackageError(
            "unsupported_version",
            f"This package uses format version {version}; this Pinny reads up to {FORMAT_VERSION}. "
            "Upgrade Pinny to load it.",
        )
    if version < 1:
        raise ModelPackageError("unsupported_version", f"Invalid format_version {version}.")


def _parse_json(data: bytes, name: str) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelPackageError("corrupt_package", f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ModelPackageError("corrupt_package", f"{name} must be a JSON object.")
    return obj
