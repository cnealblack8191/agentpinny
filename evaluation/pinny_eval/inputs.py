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
from typing import Any, Dict, List, Optional, Tuple

from .matching import Point

DETECTIONS_FORMAT = "pinny.detections"
GROUND_TRUTH_FORMAT = "pinny.ground_truth"
SUPPORTED_VERSION = 1
CANONICAL_SPACE = "canonical_raster_px"
# `dpi` is optional in files; when present it must be CANONICAL_DPI. It is kept
# out of FRAME_KEYS and checked separately (see ``dpi_check``).
FRAME_KEYS = ("space", "width", "height", "origin", "y_axis")

# Item sources that mark a pin as a manual edit. None of these may appear
# in a detections file: corrections are not detector output.
CORRECTION_SOURCES = {"manual", "user", "user_added", "user_moved", "corrected", "edited"}

CANONICAL_DPI = 200  # docs/contracts.md section 2

VERIFICATION_STATUSES = {"verified", "unverified", "synthetic", "detector_assisted_reviewed"}
# Labelled by correcting a detector's output, then reviewed. Allowed only with an
# exhaustive miss check; reports carry a recall-may-be-overstated caveat.
DETECTOR_ASSISTED = "detector_assisted_reviewed"


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
    dpi: Optional[float] = None  # optional in files; when present it must be CANONICAL_DPI

    def as_dict(self) -> Dict[str, Any]:
        """The geometric frame (dpi excluded; see ``dpi``)."""
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
    boxes: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)
    runtime_seconds: Optional[float] = None
    runtime_source: Optional[str] = None
    # Optional scan-level provenance carried through from contracts section 4.
    scan_provenance: Dict[str, Any] = field(default_factory=dict)


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

    @property
    def source_scan_id(self) -> Optional[str]:
        return self.dataset["verification"].get("source_scan_id")


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
    dpi = fr.get("dpi")
    if dpi is not None:
        if isinstance(dpi, bool) or not isinstance(dpi, (int, float)) or dpi != CANONICAL_DPI:
            raise InputError(
                f"{where}.dpi is {dpi!r}; the canonical raster is {CANONICAL_DPI} DPI "
                "(docs/contracts.md section 2), so coordinates at another resolution cannot be "
                f"compared. Omit dpi or set it to {CANONICAL_DPI}"
            )
        dpi = float(dpi)
    return Frame(
        space=space,
        width=_positive_int(_req(fr, "width", where), f"{where}.width"),
        height=_positive_int(_req(fr, "height", where), f"{where}.height"),
        origin=origin,
        y_axis=y_axis,
        dpi=dpi,
    )


def _parse_box(value: Any, where: str) -> Tuple[float, float, float, float]:
    if not isinstance(value, dict):
        raise InputError(f"{where}: must be an object {{x, y, width, height}}")
    x = _finite(_req(value, "x", where), f"{where}.x")
    y = _finite(_req(value, "y", where), f"{where}.y")
    w = _finite(_req(value, "width", where), f"{where}.width")
    h = _finite(_req(value, "height", where), f"{where}.height")
    if w <= 0 or h <= 0:
        raise InputError(f"{where}: width and height must be > 0")
    return (x, y, w, h)


def _parse_runtime(data: Dict[str, Any], path: str) -> Tuple[Optional[float], Optional[str]]:
    """Optional detector runtime: top-level ``elapsed_seconds`` or ``runtime.elapsed_seconds``."""
    candidates = []
    if data.get("elapsed_seconds") is not None:
        candidates.append((data["elapsed_seconds"], "elapsed_seconds"))
    rt = data.get("runtime")
    if isinstance(rt, dict):
        if rt.get("elapsed_seconds") is not None:
            candidates.append((rt["elapsed_seconds"], "runtime.elapsed_seconds"))
    elif rt is not None:
        raise InputError(f"{path}: runtime must be an object with elapsed_seconds")
    if not candidates:
        return None, None
    values = []
    for raw, key in candidates:
        v = _finite(raw, f"{path}: {key}")
        if v < 0:
            raise InputError(f"{path}: {key} must be >= 0")
        values.append((v, key))
    if len({v for v, _ in values}) > 1:
        raise InputError(f"{path}: elapsed_seconds and runtime.elapsed_seconds disagree")
    return values[0]


def _parse_points(items: Any, frame: Frame, where: str, *, reject_corrections: bool) -> tuple:
    if not isinstance(items, list):
        raise InputError(f"{where}: must be a list")
    points: List[Point] = []
    confidences: Dict[str, Optional[float]] = {}
    boxes: Dict[str, Tuple[float, float, float, float]] = {}
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
            # contracts.md section 4: the raw `score` is copied into `confidence`
            # on export; accept `score` directly when `confidence` is absent.
            key = "confidence" if item.get("confidence") is not None else "score"
            conf = item.get(key)
            confidences[pid] = None if conf is None else _finite(conf, f"{iw}.{key}")
            if item.get("box") is not None:
                boxes[pid] = _parse_box(item["box"], f"{iw}.box")
        points.append(Point(pid, x, y))
    return points, confidences, boxes


# Scan-level fields carried into the report when present. `mode` is the
# Phase 2 scan mode (docs/phase2-contracts.md P7).
SCAN_PROVENANCE_KEYS = ("template", "created_at", "mode")


def _scan_provenance(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    out = {k: data[k] for k in SCAN_PROVENANCE_KEYS if k in data}
    if "mode" in out:
        _nonempty_str(out["mode"], f"{path}: mode")
    # P7: candidates a verifier dropped are listed under "suppressed". They are
    # not detector output for this scan and are never scored; only their count
    # is recorded.
    if "suppressed" in data:
        if not isinstance(data["suppressed"], list):
            raise InputError(f"{path}: suppressed must be a list")
        out["suppressed_count"] = len(data["suppressed"])
    return out


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
    points, confidences, boxes = _parse_points(
        _req(data, "detections", path), frame, f"{path}: detections", reject_corrections=True
    )
    runtime, runtime_source = _parse_runtime(data, path)
    return Detections(
        path=path,
        sha256=digest,
        scan_id=_nonempty_str(_req(data, "scan_id", path), f"{path}: scan_id"),
        identity=_parse_identity(data, path),
        frame=frame,
        detector=detector,
        points=points,
        confidences=confidences,
        boxes=boxes,
        runtime_seconds=runtime,
        runtime_source=runtime_source,
        scan_provenance=_scan_provenance(data, path),
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
    if status == DETECTOR_ASSISTED:
        _check_detector_assisted(verification, f"{dw}.verification")
    elif independent is not True:
        raise InputError(
            f"{dw}.verification.independent_of_detector must be true. Reference points derived from "
            "detector output or from a corrected pin set cannot be used as ground truth "
            f"(the only exception is status '{DETECTOR_ASSISTED}' with an exhaustive miss check)"
        )
    frame = _parse_frame(data, path)
    points, _, _ = _parse_points(
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


def _check_detector_assisted(verification: Dict[str, Any], where: str) -> None:
    """Rules for a reference bootstrapped from detector output and then reviewed.

    Correcting detector output tends to miss the receptacles the detector also
    missed, which overstates recall. So the page must have had an exhaustive
    miss check (a deliberate sweep of the whole page for unmarked receptacles),
    a named reviewer, and must say honestly that it is not independent.
    """
    if verification.get("independent_of_detector") is not False:
        raise InputError(
            f"{where}.independent_of_detector must be false for status '{DETECTOR_ASSISTED}' "
            "(the labels started from detector output)"
        )
    if verification.get("exhaustive_miss_check") is not True:
        raise InputError(
            f"{where}.exhaustive_miss_check must be true for status '{DETECTOR_ASSISTED}': the whole "
            "page must be swept for receptacles the detector missed. A corrected pin set without that "
            "check cannot be used as ground truth"
        )
    reviewer = verification.get("reviewed_by")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise InputError(f"{where}.reviewed_by must name the reviewer for status '{DETECTOR_ASSISTED}'")
    src = verification.get("source_scan_id")
    if src is not None:
        _nonempty_str(src, f"{where}.source_scan_id")


def check_pair(detections: Detections, ground_truth: GroundTruth) -> None:
    """Reject a detections/reference pair that doesn't describe the same page and frame."""
    check_consistency(ground_truth.identity, ground_truth.frame.width, ground_truth.frame.height,
                      detections, ground_truth)


def dpi_check(detections: Detections, ground_truth: Optional[GroundTruth]) -> Dict[str, Any]:
    """How the (optional) frame dpi was declared. Mismatches are rejected earlier."""
    declared = {"detections": detections.frame.dpi}
    if ground_truth is not None:
        declared["ground_truth"] = ground_truth.frame.dpi
    present = [v for v in declared.values() if v is not None]
    if len(present) == len(declared):
        note = f"all inputs declare {CANONICAL_DPI} DPI"
    elif present:
        missing = [k for k, v in declared.items() if v is None]
        note = f"dpi not declared by {', '.join(missing)} (legacy file); the other input declares {CANONICAL_DPI}"
    else:
        note = f"dpi not declared (legacy file); contracts v1 assumes {CANONICAL_DPI}"
    return {"declared": declared, "canonical_dpi": CANONICAL_DPI, "note": note}


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
    if (
        ground_truth is not None
        and detections.frame.dpi is not None
        and ground_truth.frame.dpi is not None
        and detections.frame.dpi != ground_truth.frame.dpi
    ):
        problems.append(
            f"dpi differs: detections {detections.frame.dpi:g} vs ground truth {ground_truth.frame.dpi:g}"
        )
    if problems:
        raise InputError("inputs do not describe the same page/frame:\n  - " + "\n  - ".join(problems))
