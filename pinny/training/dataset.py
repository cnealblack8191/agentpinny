"""Build a ``pinny.dataset`` v1 training dataset (docs/phase2-contracts.md P3, P4).

    build_dataset(export, render, datasets_root) -> DatasetResult

``export`` is a ``LearningStore.export()`` document (``pinny.learning.export``,
the version the installed store writes, exported *with* unlabeled pins). ``render`` is anything with
``render_page(document_version, page_index) -> uint8 RGB (H, W, 3)``, normally
the real :class:`pinny.render.RenderService`.

Layout under ``datasets_root`` (normally ``$PINNY_DATA_DIR/datasets``)::

    <dataset_id>/manifest.json
    <dataset_id>/verifier/<split>/<sample_id>.png   96x96 grayscale crops
    <dataset_id>/detector/<split>/<page_key>.png    whole canonical page, grayscale

Everything is deterministic: ids are content hashes, entries are sorted,
``created_at`` defaults to the newest source timestamp, and the manifest is
written as sorted, indented JSON. The same export and rasters give the same
``dataset_id`` and byte-identical files. stdlib + numpy + opencv only; no torch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from pinny.errors import PinnyError

FORMAT = "pinny.dataset"
FORMAT_VERSION = 1
EXPORT_SCHEMA = "pinny.learning.export"
CROP_PX = 96
PAD_VALUE = 255
SPLITS = ("train", "val", "test")
DEFAULT_FRACTIONS = {"train": 0.7, "val": 0.15, "test": 0.15}
SPLIT_METHOD = "document_id_hash"
MANIFEST = "manifest.json"
_ID_HEX = 24  # sample_id / page_key length


class DatasetError(PinnyError):
    pass


class PageRenderer(Protocol):
    def render_page(self, document_version: str, page_index: int) -> np.ndarray: ...


@dataclass(frozen=True)
class DatasetResult:
    dataset_id: str
    path: Path
    manifest: dict[str, Any]
    reused: bool  # True when an identical dataset already existed


# ---------------------------------------------------------------- primitives


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def export_sha256(export: Mapping[str, Any]) -> str:
    """sha256 of the export's canonical JSON, ignoring ``exported_at``.

    Two exports of an unchanged store differ only in ``exported_at``, so they
    get the same hash (and therefore the same ``dataset_id``).
    """
    return sha256_hex(canonical_json({k: v for k, v in export.items() if k != "exported_at"}))


def split_value(document_id: str, seed: int = 0) -> float:
    """``sha256(document_id) -> [0, 1)`` from the first 8 digest bytes.

    ``seed`` 0 hashes the bare id (the contract default); any other seed
    hashes ``f"{seed}:{document_id}"``.
    """
    key = document_id if seed == 0 else f"{seed}:{document_id}"
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") / 2.0**64


def split_for(document_id: str, fractions: Mapping[str, float] = DEFAULT_FRACTIONS, seed: int = 0) -> str:
    """The split of every page and pin of ``document_id``. Never by page or pin."""
    u, cum = split_value(document_id, seed), 0.0
    for name in SPLITS:
        cum += fractions[name]
        if u < cum:
            return name
    return SPLITS[-1]  # float rounding when the fractions sum to ~1


def check_fractions(fractions: Mapping[str, float]) -> dict[str, float]:
    if set(fractions) != set(SPLITS) or any(not (0 <= float(v) <= 1) for v in fractions.values()):
        raise DatasetError("invalid_split_fractions", f"Split fractions need exactly {SPLITS}, each in [0, 1].")
    if not math.isclose(sum(fractions.values()), 1.0, abs_tol=1e-9):
        raise DatasetError("invalid_split_fractions", "Split fractions must sum to 1.")
    return {k: float(fractions[k]) for k in SPLITS}


def to_gray(page: np.ndarray) -> np.ndarray:
    """uint8 grayscale (H, W) from a 1/3-channel uint8 raster (RGB for 3)."""
    if page.dtype != np.uint8:
        raise DatasetError("invalid_raster", f"Expected a uint8 raster, got {page.dtype}.")
    if page.ndim == 2:
        return page
    if page.ndim == 3 and page.shape[2] == 1:
        return page[:, :, 0]
    if page.ndim == 3 and page.shape[2] == 3:
        return cv2.cvtColor(page, cv2.COLOR_RGB2GRAY)
    raise DatasetError("invalid_raster", f"Expected (H, W) or (H, W, 3), got {page.shape}.")


def crop_window(x: float, y: float, size: int = CROP_PX) -> tuple[int, int]:
    """Top-left of the whole-pixel ``size`` window whose centre is nearest (x, y).

    Same rule as the learning store's manual crop: ``x0 = floor(x - size/2 + 0.5)``.
    """
    return math.floor(x - size / 2 + 0.5), math.floor(y - size / 2 + 0.5)


def crop_centered(gray: np.ndarray, x: float, y: float, size: int = CROP_PX) -> np.ndarray:
    """``size`` x ``size`` crop centred on (x, y); outside the page is white (255)."""
    h, w = gray.shape
    x0, y0 = crop_window(x, y, size)
    out = np.full((size, size), PAD_VALUE, np.uint8)
    sx0, sy0, sx1, sy1 = max(x0, 0), max(y0, 0), min(x0 + size, w), min(y0 + size, h)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = gray[sy0:sy1, sx0:sx1]
    return out


def encode_png(gray: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(gray))
    if not ok:
        raise DatasetError("png_encode_failed", "Could not encode a dataset image as PNG.")
    return buf.tobytes()


def decode_png_gray(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 2:
        raise DatasetError("dataset_image_corrupt", "A dataset image is unreadable or not grayscale.")
    return img


def default_datasets_root(data_dir: os.PathLike | str | None = None) -> Path:
    from pinny.learning import default_data_dir

    return Path(data_dir if data_dir is not None else default_data_dir()) / "datasets"


# ------------------------------------------------------------------- writer


class DatasetWriter:
    """Stages files in ``<root>/.staging-<uuid>``; ``finish()`` names the directory by its id.

    Shared by the real builder and the synthetic generator so both write the
    exact same format.
    """

    def __init__(self, datasets_root: os.PathLike | str, *, source: Mapping[str, Any],
                 fractions: Mapping[str, float] = DEFAULT_FRACTIONS, seed: int = 0,
                 created_at: str) -> None:
        self.root = Path(datasets_root)
        self.fractions = check_fractions(fractions)
        self.seed = int(seed)
        self.source = dict(source)
        self.created_at = created_at
        self.verifier: list[dict[str, Any]] = []
        self.detector: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging = self.root / f".staging-{uuid.uuid4().hex}"
        self.staging.mkdir()

    def split(self, document_id: str) -> str:
        return split_for(document_id, self.fractions, self.seed)

    def _write(self, rel: str, png: bytes) -> str:
        if rel in self._ids:
            raise DatasetError("duplicate_dataset_entry", f"Two dataset entries map to {rel}.")
        self._ids.add(rel)
        path = self.staging / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return sha256_hex(png)

    def add_verifier(self, *, sample_id: str, label: int, crop: np.ndarray, canonical_page_id: str,
                     document_id: str, x: float, y: float, source_example_id: str) -> None:
        if crop.shape != (CROP_PX, CROP_PX) or crop.dtype != np.uint8:
            raise DatasetError("invalid_crop", f"Verifier crops are {CROP_PX}x{CROP_PX} uint8.")
        split = self.split(document_id)
        rel = f"verifier/{split}/{sample_id}.png"
        self.verifier.append({
            "sample_id": sample_id, "split": split, "path": rel, "label": int(label),
            "canonical_page_id": canonical_page_id, "document_id": document_id,
            "x": float(x), "y": float(y), "source_example_id": source_example_id,
            "sha256": self._write(rel, encode_png(crop)),
        })

    def add_detector(self, *, page_key: str, gray: np.ndarray, canonical_page_id: str, document_id: str,
                     points: Iterable[tuple[float, float]], source_scan_id: str | None = None) -> None:
        split = self.split(document_id)
        rel = f"detector/{split}/{page_key}.png"
        h, w = gray.shape
        entry = {
            "page_key": page_key, "split": split, "path": rel,
            "canonical_page_id": canonical_page_id, "document_id": document_id,
            "width": int(w), "height": int(h),
            "points": [{"x": float(px), "y": float(py)} for px, py in sorted(set(points), key=lambda p: (p[1], p[0]))],
            "sha256": self._write(rel, encode_png(gray)),
        }
        if source_scan_id is not None:
            entry["source_scan_id"] = source_scan_id
        self.detector.append(entry)

    def manifest(self) -> dict[str, Any]:
        verifier = sorted(self.verifier, key=lambda e: e["sample_id"])
        detector = sorted(self.detector, key=lambda e: e["page_key"])
        body = {
            "format": FORMAT, "format_version": FORMAT_VERSION,
            "created_at": self.created_at, "source": self.source,
            "split": {"method": SPLIT_METHOD, "seed": self.seed, "fractions": self.fractions},
            "verifier": verifier, "detector": detector,
            "counts": compute_counts(verifier, detector),
        }
        return {**body, "dataset_id": sha256_hex(canonical_json(body))}

    def finish(self) -> DatasetResult:
        manifest = self.manifest()
        text = manifest_bytes(manifest)
        (self.staging / MANIFEST).write_bytes(text)
        final = self.root / manifest["dataset_id"]
        try:
            if final.exists():
                existing = final / MANIFEST
                if existing.is_file() and existing.read_bytes() == text and not verify_dataset(final):
                    return DatasetResult(manifest["dataset_id"], final, manifest, reused=True)
                raise DatasetError("dataset_conflict",
                                   f"{final} exists but does not match this build; move it aside and rebuild.")
            os.replace(self.staging, final)
            return DatasetResult(manifest["dataset_id"], final, manifest, reused=False)
        finally:
            self.abort()

    def abort(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def compute_counts(verifier: Sequence[Mapping[str, Any]], detector: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, Any] = {"verifier": {s: {"pos": 0, "neg": 0} for s in SPLITS},
                              "detector": {s: {"pages": 0, "points": 0} for s in SPLITS}}
    for e in verifier:
        counts["verifier"][e["split"]]["pos" if e["label"] == 1 else "neg"] += 1
    for e in detector:
        counts["detector"][e["split"]]["pages"] += 1
        counts["detector"][e["split"]]["points"] += len(e["points"])
    return counts


# ------------------------------------------------------------------ builder


def _latest_timestamp(export: Mapping[str, Any]) -> str:
    stamps = [s.get("recorded_at") or s.get("created_at") for s in export.get("scans", [])]
    stamps += [e.get("created_at") for e in export.get("events", [])]
    stamps = [s for s in stamps if s]
    if not stamps:
        return export.get("exported_at") or "1970-01-01T00:00:00Z"
    return max(stamps)  # RFC3339 UTC strings with the same shape sort chronologically


def build_dataset(export: Mapping[str, Any], render: PageRenderer, datasets_root: os.PathLike | str, *,
                  fractions: Mapping[str, float] = DEFAULT_FRACTIONS, seed: int = 0,
                  created_at: str | None = None) -> DatasetResult:
    """Build a dataset from a learning-store export and the canonical rasters (P3, P4).

    * Verifier: every labelled pin of every scan. Approved machine and added
      manual pins are ``label 1``; rejected machine pins are ``label 0``.
      Removed manual pins and unreviewed pins are not used.
    * Detector: one entry per page, from the page's **latest** scan, and only
      when that scan has zero unreviewed pins. Its points are the approved
      and added pins; everything else on the page is background.
    """
    # P3 names the export "v1", but the learning store writes EXPORT_SCHEMA_VERSION (2 today);
    # accept exactly what the installed store produces.
    from pinny.learning import contract as learning_contract

    want = learning_contract.EXPORT_SCHEMA_VERSION
    if export.get("schema") != EXPORT_SCHEMA or export.get("schema_version") != want:
        raise DatasetError("unsupported_export",
                           f"Expected a {EXPORT_SCHEMA} v{want} export, got {export.get('schema')!r} "
                           f"v{export.get('schema_version')!r}.")
    if not (export.get("filter") or {}).get("include_unlabeled", True):
        raise DatasetError("export_missing_unlabeled",
                           "This export left out unlabeled pins, so page completeness cannot be checked. "
                           "Export again without --labeled-only.")

    scans = {s["scan_id"]: s for s in export["scans"]}
    examples_by_scan: dict[str, list[Mapping[str, Any]]] = {}
    for ex in export["examples"]:
        if ex["scan_id"] not in scans:
            raise DatasetError("malformed_export", f"Example {ex['example_id']} names an unknown scan.")
        examples_by_scan.setdefault(ex["scan_id"], []).append(ex)

    # Latest scan per page: the export lists scans in record order (recorded_at, rowid).
    order = {sid: i for i, sid in enumerate(scans)}
    latest: dict[str, str] = {}
    for sid, s in scans.items():
        cur = latest.get(s["canonical_page_id"])
        if cur is None or (s.get("recorded_at") or "", order[sid]) >= (scans[cur].get("recorded_at") or "", order[cur]):
            latest[s["canonical_page_id"]] = sid

    writer = DatasetWriter(
        datasets_root, fractions=fractions, seed=seed,
        created_at=created_at or _latest_timestamp(export),
        source={"export_sha256": export_sha256(export),
                "store_schema_version": export.get("store_schema_version"), "synthetic": False},
    )
    try:
        by_page: dict[str, list[str]] = {}
        for sid, s in scans.items():
            by_page.setdefault(s["canonical_page_id"], []).append(sid)
        for page_id in sorted(by_page):
            _build_page(writer, render, page_id, [scans[sid] for sid in by_page[page_id]],
                        examples_by_scan, scans[latest[page_id]])
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


def _build_page(writer: DatasetWriter, render: PageRenderer, page_id: str, page_scans: list[Mapping[str, Any]],
                examples_by_scan: Mapping[str, list[Mapping[str, Any]]], latest: Mapping[str, Any]) -> None:
    labelled = [ex for s in page_scans for ex in examples_by_scan.get(s["scan_id"], [])
                if ex["label"] in ("positive", "negative")]
    latest_examples = examples_by_scan.get(latest["scan_id"], [])
    complete = not any(ex["label_status"] == "unlabeled" for ex in latest_examples)
    if not labelled and not complete:
        return  # nothing to take from this page; skip rendering it

    doc = latest["document"]
    frame = latest["coordinate_frame"]
    page = render.render_page(doc["document_version"], int(doc["page_index"]))
    gray = to_gray(np.asarray(page))
    if gray.shape != (frame["height"], frame["width"]):
        raise DatasetError("raster_frame_mismatch",
                           f"{page_id} renders as {gray.shape[1]}x{gray.shape[0]} px but was scanned as "
                           f"{frame['width']}x{frame['height']} px.")
    document_id = doc["document_id"]
    for s in page_scans:
        if s["document"]["document_id"] != document_id:
            raise DatasetError("malformed_export", f"Scans of {page_id} disagree on document_id.")

    for ex in labelled:
        pt = ex["point"]
        writer.add_verifier(
            sample_id=sha256_hex(ex["example_id"])[:_ID_HEX], label=1 if ex["label"] == "positive" else 0,
            crop=crop_centered(gray, pt["x"], pt["y"]), canonical_page_id=page_id, document_id=document_id,
            x=pt["x"], y=pt["y"], source_example_id=ex["example_id"])

    if complete:
        points = [(ex["point"]["x"], ex["point"]["y"]) for ex in latest_examples if ex["label"] == "positive"]
        writer.add_detector(page_key=sha256_hex(page_id)[:_ID_HEX], gray=gray, canonical_page_id=page_id,
                            document_id=document_id, points=points, source_scan_id=latest["scan_id"])


# ----------------------------------------------------------- reading back


def resolve_dataset(ref: os.PathLike | str, datasets_root: os.PathLike | str | None = None) -> Path:
    """A dataset directory, a manifest path, or a dataset id under ``datasets_root``."""
    p = Path(ref)
    if p.is_file() and p.name == MANIFEST:
        return p.parent
    if (p / MANIFEST).is_file():
        return p
    if datasets_root is not None and (Path(datasets_root) / str(ref) / MANIFEST).is_file():
        return Path(datasets_root) / str(ref)
    raise DatasetError("dataset_not_found", f"No {MANIFEST} at {ref}.")


def load_manifest(path: os.PathLike | str) -> dict[str, Any]:
    d = resolve_dataset(path)
    manifest = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
        raise DatasetError("unsupported_dataset", f"{d} is not a {FORMAT} v{FORMAT_VERSION} dataset.")
    return manifest


def verify_dataset(path: os.PathLike | str) -> list[str]:
    """Check id, counts, files and image shapes. Returns problems (empty when valid)."""
    d = resolve_dataset(path)
    m = load_manifest(d)
    problems: list[str] = []
    body = {k: v for k, v in m.items() if k != "dataset_id"}
    if sha256_hex(canonical_json(body)) != m.get("dataset_id"):
        problems.append("dataset_id does not match the manifest content")
    if d.name != m.get("dataset_id"):
        problems.append(f"directory name {d.name} is not the dataset_id")
    if compute_counts(m["verifier"], m["detector"]) != m["counts"]:
        problems.append("counts do not match the entries")
    listed = set()
    for kind, entries in (("verifier", m["verifier"]), ("detector", m["detector"])):
        for e in entries:
            rel = e["path"]
            listed.add(rel)
            if not rel.startswith(f"{kind}/{e['split']}/") or ".." in rel.split("/"):
                problems.append(f"{rel}: bad path")
                continue
            f = d / rel
            if not f.is_file():
                problems.append(f"{rel}: missing")
                continue
            data = f.read_bytes()
            if e.get("sha256") and sha256_hex(data) != e["sha256"]:
                problems.append(f"{rel}: sha256 mismatch")
            try:
                img = decode_png_gray(data)
            except DatasetError:
                problems.append(f"{rel}: unreadable")
                continue
            want = (CROP_PX, CROP_PX) if kind == "verifier" else (e["height"], e["width"])
            if img.shape != want:
                problems.append(f"{rel}: shape {img.shape} != {want}")
    on_disk = {p.relative_to(d).as_posix() for k in ("verifier", "detector") if (d / k).is_dir()
               for p in (d / k).rglob("*") if p.is_file()}
    for extra in sorted(on_disk - listed):
        problems.append(f"{extra}: not in the manifest")
    return problems


def summarize(manifest: Mapping[str, Any]) -> dict[str, Any]:
    docs = {s: sorted({e["document_id"] for e in manifest["verifier"] + manifest["detector"] if e["split"] == s})
            for s in SPLITS}
    return {"dataset_id": manifest["dataset_id"], "created_at": manifest["created_at"],
            "synthetic": bool(manifest["source"].get("synthetic")), "split": manifest["split"],
            "counts": manifest["counts"], "documents": {s: len(v) for s, v in docs.items()}}
