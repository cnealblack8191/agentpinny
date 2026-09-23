"""Shared-contract geometry used by the learning store (docs/contracts.md v1).

* Coordinates are canonical raster pixels: the page rendered at
  ``CANONICAL_DPI`` after ``/Rotate``, origin top-left, y down (section 2).
* Boxes are ``{x, y, width, height}`` with an exclusive right/bottom edge
  (section 3). Internally the store keeps edges ``(x0, y0, x1, y1)`` and
  converts here, at its boundary.
* Training crops follow section 6 (crop spec v2).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Tuple

Edges = Tuple[float, float, float, float]

STORE_SCHEMA_VERSION = 2
EXPORT_SCHEMA = "pinny.learning.export"
EXPORT_SCHEMA_VERSION = 2

CANONICAL_DPI = 200
FRAME_SPACE = "canonical_raster_px"

CROP_SPEC_VERSION = 2
# Side of the square crop centred on a manual pin, in canonical px.
MANUAL_CROP_SIZE_PX = 128
# Context added on every side of a machine detection box, in canonical px.
DETECTION_CROP_MARGIN_PX = 24
# Float geometry is rounded to this many decimals before storing/hashing so
# float noise cannot produce distinct pins or crops.
GEOMETRY_DECIMALS = 3


def _r(v: float) -> float:
    return round(float(v), GEOMETRY_DECIMALS) + 0.0  # +0.0 folds -0.0


def canonical_page_id(document_version: str, page_index: int) -> str:
    """Section 1: derived, never assigned."""
    return f"{document_version}#p{int(page_index)}"


def frame_descriptor(width: int, height: int) -> dict:
    return {"space": FRAME_SPACE, "dpi": CANONICAL_DPI, "width": int(width), "height": int(height),
            "origin": "top-left", "y_axis": "down"}


def validate_frame(frame: Mapping[str, Any]) -> Tuple[int, int]:
    """Check a section-2 frame descriptor; returns ``(width, height)``."""
    if frame.get("space", FRAME_SPACE) != FRAME_SPACE:
        raise ValueError(f"coordinate_frame.space must be {FRAME_SPACE!r}")
    if int(frame.get("dpi", CANONICAL_DPI)) != CANONICAL_DPI:
        raise ValueError(f"coordinate_frame.dpi must be {CANONICAL_DPI}")
    if frame.get("origin", "top-left") != "top-left" or frame.get("y_axis", "down") != "down":
        raise ValueError("coordinate_frame must be origin top-left, y_axis down")
    w, h = frame["width"], frame["height"]
    if int(w) != w or int(h) != h or w <= 0 or h <= 0:
        raise ValueError("coordinate_frame width/height must be positive integers")
    return int(w), int(h)


@dataclass(frozen=True)
class Box:
    """Section-3 box. Integers for detector output, floats allowed elsewhere."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if not (self.width > 0 and self.height > 0):
            raise ValueError(f"box must have positive width/height: {self!r}")

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def edges(self) -> Edges:
        return (self.x, self.y, self.x2, self.y2)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_edges(cls, e: Edges) -> "Box":
        return cls(_num(e[0]), _num(e[1]), _num(e[2] - e[0]), _num(e[3] - e[1]))

    @classmethod
    def coerce(cls, obj: Any) -> "Box":
        """Accepts ``Box``, a ``{x, y, width, height}`` mapping, or any object
        with those attributes (e.g. ``pinny.detection.BoundingBox``)."""
        if isinstance(obj, Box):
            return cls.normalized(obj)
        if isinstance(obj, Mapping):
            vals = (obj["x"], obj["y"], obj["width"], obj["height"])
        else:
            vals = (obj.x, obj.y, obj.width, obj.height)
        return cls.normalized(cls(*vals))

    @classmethod
    def normalized(cls, b: "Box") -> "Box":
        return cls(_num(b.x), _num(b.y), _num(b.width), _num(b.height))


def _num(v: float):
    """Round floats; keep integral values as ``int`` so JSON stays tidy."""
    v = _r(v)
    return int(v) if v == int(v) else v


def normalize_point(x: float, y: float) -> Tuple[float, float]:
    return (_r(x), _r(y))


@dataclass(frozen=True)
class CropSpec:
    """Everything needed to cut one crop from the canonical raster (spec v2).

    The render service cuts ``box`` (integer canonical px, exclusive right/
    bottom edge) from ``render_page(document_version, page_index)``.
    """

    spec_version: int
    document_version: str
    page_index: int
    canonical_page_id: str
    frame_width: int
    frame_height: int
    dpi: int
    unclipped_box: Box  # may extend past the raster
    box: Box  # unclipped_box intersected with the raster
    clipped_left: bool
    clipped_top: bool
    clipped_right: bool
    clipped_bottom: bool
    method: str  # "manual_point_square" | "detection_box_margin"

    @property
    def clipped(self) -> bool:
        return self.clipped_left or self.clipped_top or self.clipped_right or self.clipped_bottom

    def to_dict(self) -> dict:
        d = asdict(self)
        d["clipped"] = self.clipped
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CropSpec":
        d = dict(d)
        d.pop("clipped", None)
        if d.get("spec_version") != CROP_SPEC_VERSION:
            raise ValueError(f"unsupported crop spec_version {d.get('spec_version')!r}")
        d["unclipped_box"] = Box(**d["unclipped_box"])
        d["box"] = Box(**d["box"])
        return cls(**d)


def _build(document_version: str, page_index: int, frame_w: int, frame_h: int,
           ux0: int, uy0: int, ux1: int, uy1: int, method: str) -> CropSpec:
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError("frame dimensions must be positive")
    x0, y0, x1, y1 = max(ux0, 0), max(uy0, 0), min(ux1, frame_w), min(uy1, frame_h)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"crop ({ux0},{uy0})-({ux1},{uy1}) lies outside the {frame_w}x{frame_h} raster")
    return CropSpec(
        spec_version=CROP_SPEC_VERSION,
        document_version=document_version,
        page_index=int(page_index),
        canonical_page_id=canonical_page_id(document_version, page_index),
        frame_width=int(frame_w),
        frame_height=int(frame_h),
        dpi=CANONICAL_DPI,
        unclipped_box=Box(ux0, uy0, ux1 - ux0, uy1 - uy0),
        box=Box(x0, y0, x1 - x0, y1 - y0),
        clipped_left=ux0 < 0, clipped_top=uy0 < 0,
        clipped_right=ux1 > frame_w, clipped_bottom=uy1 > frame_h,
        method=method,
    )


def manual_pin_crop(document_version: str, page_index: int, frame_w: int, frame_h: int,
                    x: float, y: float, size: int = MANUAL_CROP_SIZE_PX) -> CropSpec:
    """``size``×``size`` px window whose centre is nearest the pin.

    ``x0 = floor(x - size/2 + 0.5)`` (round half up), ``x1 = x0 + size``;
    same for y. Then clipped to the raster.
    """
    x, y = normalize_point(x, y)
    x0 = math.floor(x - size / 2.0 + 0.5)
    y0 = math.floor(y - size / 2.0 + 0.5)
    return _build(document_version, page_index, frame_w, frame_h,
                  x0, y0, x0 + size, y0 + size, "manual_point_square")


def detection_crop(document_version: str, page_index: int, frame_w: int, frame_h: int,
                   box: Any, margin: int = DETECTION_CROP_MARGIN_PX) -> CropSpec:
    """Detection box expanded to whole pixels, plus ``margin`` on every side,
    then clipped to the raster."""
    b = Box.coerce(box)
    return _build(document_version, page_index, frame_w, frame_h,
                  math.floor(b.x) - margin, math.floor(b.y) - margin,
                  math.ceil(b.x2) + margin, math.ceil(b.y2) + margin, "detection_box_margin")


def pin_crop(document_version: str, page_index: int, frame_w: int, frame_h: int,
             x: float, y: float, box: Any) -> CropSpec:
    if box is not None:
        return detection_crop(document_version, page_index, frame_w, frame_h, box)
    return manual_pin_crop(document_version, page_index, frame_w, frame_h, x, y)
