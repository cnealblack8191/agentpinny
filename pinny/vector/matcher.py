"""Public API: :class:`VectorMatcher`, :class:`VectorSettings`, :class:`VectorResult`."""

from __future__ import annotations

import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

import numpy as np

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


# --------------------------------------------------------------------------
# Exemplars from another page (e.g. the legend sheet)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorExemplar:
    """A symbol taken from one page, usable on any page of the same file.

    ``ctrl``/``kind`` are the symbol's primitives in canonical px of the
    source page (only their shape matters). When the symbol is a placed Form
    XObject, ``xobject_key`` identifies that block definition and
    ``xobject_linear`` its orientation, so other placements of the same block
    are found exactly on every page.
    """

    source_page: int
    box: BoundingBox
    ctrl: np.ndarray
    kind: np.ndarray
    xobject_key: Optional[str] = None
    xobject_linear: Optional[np.ndarray] = None
    scale: float = 1.0

    def scaled(self, factor: float) -> "VectorExemplar":
        """The same symbol drawn ``factor`` times larger (for legends drawn at
        a different size than the sheets). Block matching is dropped: it
        already reports any scale."""
        if not (isinstance(factor, (int, float)) and math.isfinite(factor) and factor > 0):
            raise VectorMatchError("invalid_settings", f"Scale factor must be > 0, got {factor!r}.")
        c = np.array([self.box.x + self.box.width / 2.0, self.box.y + self.box.height / 2.0])
        ctrl = c + (self.ctrl - c) * factor
        w, h = max(1, round(self.box.width * factor)), max(1, round(self.box.height * factor))
        box = BoundingBox(int(round(c[0] - w / 2)), int(round(c[1] - h / 2)), int(w), int(h))
        return VectorExemplar(self.source_page, box, ctrl, self.kind.copy(), None, None, self.scale * factor)


def _load_vector_page(pdf_path, page_index: int, settings: VectorSettings, warnings: List[str]):
    with open_pdf(pdf_path) as pdf:
        if pdf.is_encrypted:
            warnings.append(
                "The PDF is encrypted with permissions only (no open password); content was read normally."
            )
        content = read_page_content(pdf, page_index)
    warnings.extend(content.warnings)
    kind = kind_from_content(content)
    if kind.kind == "raster":
        raise RasterPageError(
            f"Page {page_index} is a raster scan (images cover {kind.stats['image_coverage']:.0%} of it, "
            f"{kind.stats['path_segments']} path segments). Use the raster matcher."
        )
    if kind.kind == "mixed":
        if not settings.allow_mixed:
            raise RasterPageError(
                f"Page {page_index} mixes images and vector line work; allow_mixed is off. Use the raster matcher."
            )
        warnings.append(
            "Page mixes raster images and vector line work; symbols inside the images are not found by the "
            "vector matcher. Run the raster matcher too."
        )
    return content, kind


def _path_params(settings: VectorSettings, check_deadline) -> PathParams:
    return PathParams(
        threshold=settings.threshold,
        tol_px=settings.tolerance_pt * PT_TO_PX,
        rotations=tuple(settings.rotations),
        allow_mirrored=settings.allow_mirrored,
        scale_tolerance=settings.scale_tolerance,
        max_anchors=settings.max_anchors,
        max_hypotheses=settings.max_hypotheses,
        extra_match_fraction=settings.extra_match_fraction,
        descriptor_quantum=settings.descriptor_quantum,
        check_deadline=check_deadline,
    )


def _no_deadline() -> None:
    return None


def make_exemplar(pdf_path, page_index: int, exemplar_box_px, settings: VectorSettings = VectorSettings()) -> VectorExemplar:
    """Take the symbol inside ``exemplar_box_px`` on ``page_index`` as a
    portable exemplar."""
    settings.validate()
    try:
        box = _as_box(exemplar_box_px)
    except Exception as exc:
        if isinstance(exc, VectorMatchError):
            raise
        raise VectorMatchError("invalid_exemplar_box", str(exc)) from None
    content, _kind = _load_vector_page(pdf_path, page_index, settings, [])
    frame = content.frame
    W, H = frame.width_px, frame.height_px
    if box.x >= W or box.y >= H or box.x2 <= 0 or box.y2 <= 0:
        raise VectorMatchError("invalid_exemplar_box", f"Exemplar box {box.to_dict()} lies outside the {W}x{H} px page.")
    ex_box = (float(box.x), float(box.y), float(box.x2), float(box.y2))
    prim_boxes = primitive_boxes_px(content)
    key, linear = None, None
    if content.placements and settings.strategy in ("auto", "xobject"):
        from .geometry import iou
        from .xobject_matcher import place_forms

        best, best_iou = None, -1.0
        for pf in place_forms(content, prim_boxes):
            v = iou(pf.box, ex_box)
            if v > best_iou:
                best, best_iou = pf, v
        if best is not None and best_iou >= settings.xobject_min_iou:
            key, linear = best.placement.group_key, best.linear.copy()
    tol_px = settings.tolerance_pt * PT_TO_PX
    ctrl_px = apply(frame.user_to_px, content.ctrl) if content.n_segments else content.ctrl
    index = PageIndex(ctrl_px, content.kind, prim_boxes, tol_px, max(16.0, 1.5 * max(box.width, box.height)), W, H)
    from .path_matcher import build_exemplar

    ex = build_exemplar(index, ex_box, _path_params(settings, _no_deadline), settings.max_exemplar_primitives)
    return VectorExemplar(page_index, box, ex.ctrl.copy(), ex.kind.copy(), key, linear)


@dataclass
class PreparedPage:
    """One page's vector content, loaded and indexed once, for matching
    many exemplars (every legend symbol) against it."""

    page_index: int
    content: object
    kind: PageKind
    prim_boxes: np.ndarray
    warnings: List[str]
    _index: Optional[PageIndex] = None
    _index_key: Optional[Tuple[float, float]] = None

    @property
    def frame(self) -> PageFrame:
        return self.content.frame

    def index(self, tol_px: float, cell: float) -> Optional[PageIndex]:
        if not self.content.n_segments:
            return None
        if self._index is None or self._index_key[0] != tol_px:
            ctrl_px = apply(self.frame.user_to_px, self.content.ctrl)
            self._index = PageIndex(ctrl_px, self.content.kind, self.prim_boxes, tol_px, cell,
                                    self.frame.width_px, self.frame.height_px)
            self._index_key = (tol_px, cell)
        return self._index


def prepare_page(pdf_path, page_index: int, settings: VectorSettings = VectorSettings()) -> PreparedPage:
    settings.validate()
    warnings: List[str] = []
    content, kind = _load_vector_page(pdf_path, page_index, settings, warnings)
    return PreparedPage(page_index, content, kind, primitive_boxes_px(content), warnings)


def detect_exemplar(pdf_path, page_index: int, exemplar: VectorExemplar,
                    settings: VectorSettings = VectorSettings(),
                    prepared: Optional[PreparedPage] = None) -> VectorResult:
    """Find ``exemplar`` (taken from any page of the same file) on
    ``page_index``. Pass ``prepared`` to reuse a page loaded by
    :func:`prepare_page`."""
    start = time.perf_counter()
    settings.validate()
    if prepared is None:
        prepared = prepare_page(pdf_path, page_index, settings)
    return match_prepared(prepared, exemplar, settings, start)


def match_prepared(prepared: PreparedPage, exemplar: VectorExemplar,
                   settings: VectorSettings = VectorSettings(), start: Optional[float] = None) -> VectorResult:
    start = time.perf_counter() if start is None else start
    deadline = start + settings.max_runtime_seconds

    def check_deadline() -> None:
        if time.perf_counter() > deadline:
            raise VectorMatchError(
                "timeout",
                f"Vector matching exceeded max_runtime_seconds={settings.max_runtime_seconds}. "
                "Use the raster matcher or raise the limit.",
            )

    warnings: List[str] = list(prepared.warnings)
    content, kind, frame = prepared.content, prepared.kind, prepared.frame
    prim_boxes = prepared.prim_boxes
    stats: dict = {"primitives": content.n_segments, "form_placements": len(content.placements),
                   "exemplar_page": exemplar.source_page, "exemplar_scale": exemplar.scale}
    raw: List[RawDetection] = []
    strategy = ""
    if exemplar.xobject_key and settings.strategy in ("auto", "xobject"):
        from .xobject_matcher import match_xobject_key

        found = match_xobject_key(
            content, exemplar.xobject_key, exemplar.xobject_linear, rotations=settings.rotations,
            allow_mirrored=settings.allow_mirrored, angle_tolerance_deg=settings.angle_tolerance_deg,
            prim_boxes=prim_boxes,
        )
        if found and (found[0] or settings.strategy == "xobject"):
            raw, xw = found
            warnings.extend(xw)
            strategy = "xobject"
    check_deadline()
    ex_short = min(exemplar.box.width, exemplar.box.height)
    if not strategy and settings.strategy != "xobject":
        strategy = "paths"
        params = _path_params(settings, check_deadline)
        from .path_matcher import exemplar_from_ctrl

        ex = exemplar_from_ctrl(exemplar.ctrl, exemplar.kind, params)
        cell = max(16.0, 1.5 * max(ex.bbox[2] - ex.bbox[0], ex.bbox[3] - ex.bbox[1]))
        index = prepared.index(params.tol_px, cell)
        if index is not None:
            check_deadline()
            raw, pstats, _ = match_paths(index, None, params, settings.max_exemplar_primitives, exemplar=ex)
            stats.update(asdict(pstats))
        ex_short = min(ex.bbox[2] - ex.bbox[0], ex.bbox[3] - ex.bbox[1])
    kept = suppress(raw, settings.duplicate_center_ratio * max(ex_short, 1.0))
    kept.sort(key=lambda d: (-round(d.score, 6), d.box[1], d.box[0]))
    truncated = len(kept) > settings.max_detections
    if truncated:
        warnings.append(f"More than max_detections={settings.max_detections} matches; the rest were dropped.")
        kept = kept[: settings.max_detections]
    detections = []
    for i, d in enumerate(kept, start=1):
        x, y, w, h = box_to_int(d.box)
        detections.append(VectorDetection(id=f"det-{i}", box=BoundingBox(x, y, max(w, 1), max(h, 1)),
                                          score=float(d.score), rotation=d.rotation, mirrored=d.mirrored,
                                          source=d.source, angle=d.angle))
    return VectorResult(frame=frame, detections=tuple(detections), strategy=strategy or "xobject",
                        exemplar_box=exemplar.box, settings=settings, page_kind=kind,
                        elapsed_seconds=time.perf_counter() - start, truncated=truncated,
                        warnings=tuple(warnings), stats=stats)
