"""Loading and validation of evaluator inputs.

The file formats here are the evaluator's own interchange formats (see
evaluation/README.md). They are provisional until docs/contracts.md
defines the app's scan-result schema; an adapter from that schema into
`pinny.detections` v1 is expected to be the only change needed.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .matching import Point

DETECTIONS_FORMAT = "pinny.detections"
GROUND_TRUTH_FORMAT = "pinny.ground_truth"
SUPPORTED_VERSION = 1
CANONICAL_SPACE = "canonical_raster_px"
FRAME_KEYS = ("space", "width", "height", "origin", "y_axis")

# Item sources that mark a pin as a manual edit. None of these may appear
# in a detections file: corrections are not detector output.
CORRECTION_SOURCES = {"manual", "user", "user_added", "user_moved", "corrected", "edited"}

VERIFICATION_STATUSES = {"verified", "unverified", "synthetic"}


class InputError(Exception):
    """An input is malformed or inconsistent; the evaluation must not run."""


@dataclass
class Identity:
    document_id: str
    document_version: str
    page_index: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_version": self.document_version,
            "page_index": self.page_index,
        }


@dataclass
class Frame:
    space: str
    width: int
    height: int
    origin: str
    y_axis: str

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in FRAME_KEYS}


@dataclass
class Detections:
    path: str
    sha256: str
    scan_id: str
    identity: Identity
    frame: Frame
    detector: Dict[str, Any]
    points: List[Point]
    confidences: Dict[str, Optional[float]] = field(default_factory=dict)


@dataclass
class GroundTruth:
    path: str
    sha256: str
    dataset: Dict[str, Any]
    identity: Identity
    frame: Frame
    points: List[Point]

    @property
    def verification_status(self) -> str:
        return self.dataset["verification"]["status"]


def _load_json(path: str) -> tuple:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise InputError(f"{path}: cannot read file ({exc})") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path}: not valid UTF-8 JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: top level must be a JSON object")
    return data, hashlib.sha256(raw).hexdigest()


def _req(obj: Dict[str, Any], key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise InputError(f"{where}: missing required field '{key}'")
    return obj[key]


def _nonempty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where}: must be a non-empty string")
    return value


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InputError(f"{where}: must be a positive integer")
    return value


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputError(f"{where}: must be a finite number")
    return float(value)


def _check_format(data: Dict[str, Any], expected: str, path: str) -> None:
    fmt = _req(data, "format", path)
    if fmt != expected:
        raise InputError(f"{path}: format is '{fmt}', expected '{expected}'")
    ver = _req(data, "format_version", path)
    if ver != SUPPORTED_VERSION:
        raise InputError(f"{path}: unsupported format_version {ver!r} (supported: {SUPPORTED_VERSION})")


def _parse_identity(data: Dict[str, Any], path: str) -> Identity:
    doc = _req(data, "document", path)
    where = f"{path}: document"
    page = _req(doc, "page_index", where)
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise InputError(f"{where}.page_index: must be an integer >= 0")
    return Identity(
        document_id=_nonempty_str(_req(doc, "document_id", where), f"{where}.document_id"),
        document_version=_nonempty_str(_req(doc, "document_version", where), f"{where}.document_version"),
        page_index=page,
    )


def _parse_frame(data: Dict[str, Any], path: str) -> Frame:
    fr = _req(data, "coordinate_frame", path)
    where = f"{path}: coordinate_frame"
    space = _req(fr, "space", where)
    if space != CANONICAL_SPACE:
        raise InputError(f"{where}.space is '{space}'; only '{CANONICAL_SPACE}' is accepted")
    origin = _req(fr, "origin", where)
    y_axis = _req(fr, "y_axis", where)
    if origin != "top-left" or y_axis != "down":
        raise InputError(f"{where}: only origin 'top-left' with y_axis 'down' is accepted")
    return Frame(
        space=space,
        width=_positive_int(_req(fr, "width", where), f"{where}.width"),
        height=_positive_int(_req(fr, "height", where), f"{where}.height"),
        origin=origin,
        y_axis=y_axis,
    )


def _parse_points(items: Any, frame: Frame, where: str, *, reject_corrections: bool) -> tuple:
    if not isinstance(items, list):
        raise InputError(f"{where}: must be a list")
    points: List[Point] = []
    confidences: Dict[str, Optional[float]] = {}
    seen = set()
    for i, item in enumerate(items):
        iw = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise InputError(f"{iw}: must be an object")
        pid = _nonempty_str(_req(item, "id", iw), f"{iw}.id")
        if pid in seen:
            raise InputError(f"{iw}: duplicate id '{pid}'")
        seen.add(pid)
        x = _finite(_req(item, "x", iw), f"{iw}.x")
        y = _finite(_req(item, "y", iw), f"{iw}.y")
        if not (0 <= x <= frame.width and 0 <= y <= frame.height):
            raise InputError(
                f"{iw} ('{pid}'): ({x}, {y}) lies outside the {frame.width}x{frame.height} canonical raster"
            )
        if reject_corrections:
            src = item.get("source", "detector")
            if src != "detector":
                raise InputError(
                    f"{iw} ('{pid}'): source is '{src}'. Only original detector output may be scored; "
                    "manually corrected pins are not detector results"
                    if src in CORRECTION_SOURCES
                    else f"{iw} ('{pid}'): unknown source '{src}'; expected 'detector'"
                )
            conf = item.get("confidence")
            confidences[pid] = None if conf is None else _finite(conf, f"{iw}.confidence")
        points.append(Point(pid, x, y))
    return points, confidences


def load_detections(path: str) -> Detections:
    data, digest = _load_json(path)
    _check_format(data, DETECTIONS_FORMAT, path)
    provenance = _req(data, "provenance", path)
    if provenance != "original_detector_output":
        raise InputError(
            f"{path}: provenance is '{provenance}'. The evaluator scores only "
            "'original_detector_output'; a corrected pin set is not detector accuracy"
        )
    frame = _parse_frame(data, path)
    detector = _req(data, "detector", path)
    if not isinstance(detector, dict):
        raise InputError(f"{path}: detector must be an object")
    _nonempty_str(_req(detector, "name", f"{path}: detector"), f"{path}: detector.name")
    _nonempty_str(_req(detector, "version", f"{path}: detector"), f"{path}: detector.version")
    settings = _req(detector, "settings", f"{path}: detector")
    if not isinstance(settings, dict):
        raise InputError(f"{path}: detector.settings must be an object (use {{}} if none)")
    points, confidences = _parse_points(
        _req(data, "detections", path), frame, f"{path}: detections", reject_corrections=True
    )
    return Detections(
        path=path,
        sha256=digest,
        scan_id=_nonempty_str(_req(data, "scan_id", path), f"{path}: scan_id"),
        identity=_parse_identity(data, path),
        frame=frame,
        detector=detector,
        points=points,
        confidences=confidences,
    )


def load_ground_truth(path: str) -> GroundTruth:
    data, digest = _load_json(path)
    _check_format(data, GROUND_TRUTH_FORMAT, path)
    dataset = _req(data, "dataset", path)
    dw = f"{path}: dataset"
    _nonempty_str(_req(dataset, "dataset_id", dw), f"{dw}.dataset_id")
    _nonempty_str(_req(dataset, "labeled_by", dw), f"{dw}.labeled_by")
    verification = _req(dataset, "verification", dw)
    status = _req(verification, "status", f"{dw}.verification")
    if status not in VERIFICATION_STATUSES:
        raise InputError(f"{dw}.verification.status must be one of {sorted(VERIFICATION_STATUSES)}")
    independent = _req(verification, "independent_of_detector", f"{dw}.verification")
    if independent is not True:
        raise InputError(
            f"{dw}.verification.independent_of_detector must be true. Reference points derived from "
            "detector output or from a corrected pin set cannot be used as ground truth"
        )
    frame = _parse_frame(data, path)
    points, _ = _parse_points(
        _req(data, "receptacles", path), frame, f"{path}: receptacles", reject_corrections=False
    )
    return GroundTruth(
        path=path,
        sha256=digest,
        dataset=dataset,
        identity=_parse_identity(data, path),
        frame=frame,
        points=points,
    )


def check_consistency(
    expected_identity: Identity,
    expected_width: int,
    expected_height: int,
    detections: Detections,
    ground_truth: Optional[GroundTruth],
) -> None:
    """Reject any mismatch in document version, page or coordinate frame."""
    problems: List[str] = []
    sources = [("detections", detections)]
    if ground_truth is not None:
        sources.append(("ground truth", ground_truth))
    for label, src in sources:
        for key in ("document_id", "document_version", "page_index"):
            got = getattr(src.identity, key)
            want = getattr(expected_identity, key)
            if got != want:
                problems.append(f"{label} {key} is {got!r}, expected {want!r}")
        if src.frame.width != expected_width or src.frame.height != expected_height:
            problems.append(
                f"{label} raster is {src.frame.width}x{src.frame.height}, "
                f"expected {expected_width}x{expected_height}"
            )
    if ground_truth is not None and detections.frame.as_dict() != ground_truth.frame.as_dict():
        problems.append(
            f"coordinate frames differ: detections {detections.frame.as_dict()} "
            f"vs ground truth {ground_truth.frame.as_dict()}"
        )
    if problems:
        raise InputError("inputs do not describe the same page/frame:\n  - " + "\n  - ".join(problems))
