"""Public API: :class:`VectorMatcher`, :class:`VectorSettings`, :class:`VectorResult`."""

from __future__ import annotations

import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

from pinny.detection.types import ALLOWED_ROTATIONS, BoundingBox

from ._types import RawDetection
from .classify import PageKind, kind_from_content
from .content import open_pdf, read_page_content
from .errors import RasterPageError, VectorMatchError
from .frame import PT_TO_PX, PageFrame, apply
from .geometry import box_to_int
from .path_matcher import PageIndex, PathParams, match_paths, suppress
from .xobject_matcher import match_xobjects, primitive_boxes_px

DETECTOR_NAME = "pinny-vector"
DETECTOR_VERSION = "1"
STRATEGIES = ("auto", "xobject", "paths")


@dataclass(frozen=True)
class VectorSettings:
    #: Minimum path-match score in [0, 1] (XObject matches always score 1.0).
    threshold: float = 0.9
    #: Geometric tolerance in PDF points (0.5 pt = 1.4 canonical px).
    tolerance_pt: float = 0.5
    #: "auto" tries block (XObject) reuse first, then flattened paths.
    strategy: str = "auto"
    #: The exemplar box must overlap a placement's ink box by this IoU to count
    #: as "the user boxed this XObject".
    xobject_min_iou: float = 0.5
    rotations: Tuple[int, ...] = ALLOWED_ROTATIONS
    allow_mirrored: bool = True
    #: Path fast path: allowed relative size difference (0.02 = 2%).
    scale_tolerance: float = 0.02
    #: Placements more than this far from a quarter turn get a warning and an
    #: exact ``angle`` field.
    angle_tolerance_deg: float = 1.0
    #: Distinct exemplar primitives used to generate pose hypotheses.
    max_anchors: int = 2
    max_hypotheses: int = 2_000_000
    max_exemplar_primitives: int = 2000
    max_detections: int = 5000
    #: Extra primitive inside a candidate counts as "explained" if this
    #: fraction of it lies on the exemplar geometry.
    extra_match_fraction: float = 0.8
    #: Signature quantum, in units of the symbol's RMS radius.
    descriptor_quantum: float = 0.02
    #: Two detections closer than this fraction of the exemplar's short side
    #: are duplicates.
    duplicate_center_ratio: float = 0.5
    #: Run on "mixed" pages (images covering part of the page) with a warning.
    allow_mixed: bool = True
    max_runtime_seconds: float = 60.0

    def validate(self) -> None:
        def fail(msg: str) -> None:
            raise VectorMatchError("invalid_settings", msg)

        def num(v) -> bool:
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

        if not num(self.threshold) or not 0.0 < self.threshold <= 1.0:
            fail(f"threshold must be in (0, 1], got {self.threshold!r}.")
        if not num(self.tolerance_pt) or not 0.01 <= self.tolerance_pt <= 10:
            fail(f"tolerance_pt must be in [0.01, 10] points, got {self.tolerance_pt!r}.")
        if self.strategy not in STRATEGIES:
            fail(f"strategy must be one of {STRATEGIES}, got {self.strategy!r}.")
        if not num(self.xobject_min_iou) or not 0.0 < self.xobject_min_iou <= 1.0:
            fail(f"xobject_min_iou must be in (0, 1], got {self.xobject_min_iou!r}.")
        if not self.rotations or any(isinstance(r, bool) or r not in ALLOWED_ROTATIONS for r in self.rotations):
            fail(f"rotations must be a non-empty subset of {ALLOWED_ROTATIONS}.")
        for name in ("scale_tolerance", "angle_tolerance_deg", "extra_match_fraction",
                     "descriptor_quantum", "duplicate_center_ratio", "max_runtime_seconds"):
            v = getattr(self, name)
            if not num(v) or v < 0:
                fail(f"{name} must be a non-negative number, got {v!r}.")
        if self.max_runtime_seconds <= 0 or self.descriptor_quantum <= 0:
            fail("max_runtime_seconds and descriptor_quantum must be > 0.")
        for name in ("max_anchors", "max_hypotheses", "max_exemplar_primitives", "max_detections"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                fail(f"{name} must be a positive integer, got {v!r}.")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rotations"] = list(self.rotations)
        return d


@dataclass(frozen=True)
class VectorDetection:
    id: str
    box: BoundingBox
    score: float
    rotation: int
    mirrored: bool
    source: str
    angle: Optional[float] = None

    @property
    def center(self) -> Tuple[float, float]:
        return self.box.center

    def to_dict(self) -> dict:
        cx, cy = self.center
        d = {
            "id": self.id,
            "box": self.box.to_dict(),
            "x": cx,
            "y": cy,
            "score": round(float(self.score), 6),
            "rotation": self.rotation,
            "source": self.source,
        }
        if self.mirrored:
            d["mirrored"] = True
        if self.angle is not None:
            d["angle"] = round(self.angle, 3)
        return d


@dataclass(frozen=True)
class VectorResult:
    frame: PageFrame
    detections: Tuple[VectorDetection, ...]
    strategy: str  # "xobject" or "paths"
    exemplar_box: BoundingBox
    settings: VectorSettings
    page_kind: PageKind
    elapsed_seconds: float
    truncated: bool = False
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    stats: dict = field(default_factory=dict)

    @property
    def coordinate_frame(self) -> dict:
        return self.frame.descriptor()

    def to_dict(self) -> dict:
        """Mirrors the scan-result shape of contracts section 4 (the fields the
        matcher knows; the app adds scan_id, document and created_at)."""
        return {
            "coordinate_frame": self.coordinate_frame,
            "template": {"box": self.exemplar_box.to_dict()},
            "detector": {
                "name": f"{DETECTOR_NAME}-{self.strategy}",
                "version": DETECTOR_VERSION,
                "settings": self.settings.to_dict(),
            },
            "detections": [d.to_dict() for d in self.detections],
            "strategy": self.strategy,
            "page_kind": self.page_kind.to_dict(),
            "threshold": self.settings.threshold,
            "truncated": self.truncated,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
        }


def _as_box(box) -> BoundingBox:
    if isinstance(box, BoundingBox):
        return box
    try:
        if isinstance(box, dict):
            vals = (box["x"], box["y"], box["width"], box["height"])
        else:
            vals = tuple(box)
            if len(vals) != 4:
                raise ValueError
        return BoundingBox(*(int(round(float(v))) for v in vals))
    except VectorMatchError:
        raise
    except Exception:
        raise VectorMatchError(
            "invalid_exemplar_box",
            f"exemplar_box_px must be a BoundingBox, a {{x, y, width, height}} dict or "
            f"an (x, y, width, height) tuple in canonical px, got {box!r}.",
        ) from None


class VectorMatcher:
    """Finds repeats of a symbol by matching PDF drawing primitives."""

    name = DETECTOR_NAME

    def detect(
        self,
        pdf_path: "str | os.PathLike[str]",
        page_index: int,
        exemplar_box_px,
        settings: VectorSettings = VectorSettings(),
    ) -> VectorResult:
        start = time.perf_counter()
        settings.validate()
        try:
            box = _as_box(exemplar_box_px)
        except Exception as exc:  # BoundingBox raises DetectionError("invalid_box")
            if isinstance(exc, VectorMatchError):
                raise
            raise VectorMatchError("invalid_exemplar_box", str(exc)) from None
        deadline = start + settings.max_runtime_seconds

        def check_deadline() -> None:
            if time.perf_counter() > deadline:
                raise VectorMatchError(
                    "timeout",
                    f"Vector matching exceeded max_runtime_seconds={settings.max_runtime_seconds}. "
                    "Use the raster matcher or raise the limit.",
                )

        warnings: List[str] = []
        with open_pdf(pdf_path) as pdf:
            if pdf.is_encrypted:
                warnings.append(
                    "The PDF is encrypted with permissions only (no open password); "
                    "content was read normally."
                )
            content = read_page_content(pdf, page_index)
        warnings.extend(content.warnings)
        kind = kind_from_content(content)
        if kind.kind == "raster":
            raise RasterPageError(
                f"Page {page_index} is a raster scan (images cover "
                f"{kind.stats['image_coverage']:.0%} of it, {kind.stats['path_segments']} path "
                "segments). Use the raster matcher."
            )
        if kind.kind == "mixed":
            if not settings.allow_mixed:
                raise RasterPageError(
                    f"Page {page_index} mixes images and vector line work; allow_mixed is "
                    "off. Use the raster matcher."
                )
            warnings.append(
                "Page mixes raster images and vector line work; symbols inside the images "
                "are not found by the vector matcher. Run the raster matcher too."
            )
        frame = content.frame
        W, H = frame.width_px, frame.height_px
        if box.x >= W or box.y >= H or box.x2 <= 0 or box.y2 <= 0:
            raise VectorMatchError(
                "invalid_exemplar_box",
                f"Exemplar box {box.to_dict()} lies outside the {W}x{H} px canonical page.",
            )
        ex_box = (float(box.x), float(box.y), float(box.x2), float(box.y2))
        check_deadline()

        prim_boxes = primitive_boxes_px(content)
        raw: List[RawDetection] = []
        strategy = ""
        stats: dict = {"primitives": content.n_segments, "form_placements": len(content.placements)}

        if settings.strategy in ("auto", "xobject"):
            xm = match_xobjects(
                content, ex_box,
                min_iou=settings.xobject_min_iou,
                rotations=settings.rotations,
                allow_mirrored=settings.allow_mirrored,
                angle_tolerance_deg=settings.angle_tolerance_deg,
                prim_boxes=prim_boxes,
            )
            use = xm is not None and (settings.strategy == "xobject" or len(xm.detections) >= 2)
            if use:
                strategy = "xobject"
                raw = xm.detections
                warnings.extend(xm.warnings)
                stats["exemplar_iou"] = round(xm.exemplar_iou, 4)
                stats["xobject"] = xm.exemplar.placement.name
            elif settings.strategy == "xobject":
                strategy = "xobject"
                warnings.append(
                    "No Form XObject placement matches the exemplar box "
                    f"(IoU >= {settings.xobject_min_iou}); nothing found. Try strategy='paths'."
                )
        check_deadline()

        ex_short = min(box.width, box.height)
        if not strategy:
            strategy = "paths"
            tol_px = settings.tolerance_pt * PT_TO_PX
            ctrl_px = apply(frame.user_to_px, content.ctrl) if content.n_segments else content.ctrl
            cell = max(16.0, 1.5 * max(box.width, box.height))
            index = PageIndex(ctrl_px, content.kind, prim_boxes, tol_px, cell, W, H)
            check_deadline()
            params = PathParams(
                threshold=settings.threshold,
                tol_px=tol_px,
                rotations=tuple(settings.rotations),
                allow_mirrored=settings.allow_mirrored,
                scale_tolerance=settings.scale_tolerance,
                max_anchors=settings.max_anchors,
                max_hypotheses=settings.max_hypotheses,
                extra_match_fraction=settings.extra_match_fraction,
                descriptor_quantum=settings.descriptor_quantum,
                check_deadline=check_deadline,
            )
            raw, pstats, ex = match_paths(index, ex_box, params, settings.max_exemplar_primitives)
            stats.update(asdict(pstats))
            ex_short = min(ex.bbox[2] - ex.bbox[0], ex.bbox[3] - ex.bbox[1])

        kept = suppress(raw, settings.duplicate_center_ratio * max(ex_short, 1.0))
        kept.sort(key=lambda d: (-round(d.score, 6), d.box[1], d.box[0]))
        truncated = len(kept) > settings.max_detections
        if truncated:
            warnings.append(
                f"More than max_detections={settings.max_detections} matches; the rest were dropped."
            )
            kept = kept[: settings.max_detections]
        detections = []
        for i, d in enumerate(kept, start=1):
            x, y, w, h = box_to_int(d.box)
            detections.append(
                VectorDetection(
                    id=f"det-{i}", box=BoundingBox(x, y, w, h), score=float(d.score),
                    rotation=d.rotation, mirrored=d.mirrored, source=d.source, angle=d.angle,
                )
            )
        return VectorResult(
            frame=frame,
            detections=tuple(detections),
            strategy=strategy,
            exemplar_box=box,
            settings=settings,
            page_kind=kind,
            elapsed_seconds=time.perf_counter() - start,
            truncated=truncated,
            warnings=tuple(warnings),
            stats=stats,
        )
