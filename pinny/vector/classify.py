"""Page classification: vector, raster (scanned) or mixed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict

from .content import PageContent, open_pdf, read_page_content

#: Images covering at least this fraction of the page make it raster-like.
RASTER_IMAGE_COVERAGE = 0.5
#: Below this many path segments an image-covered page is a plain scan.
RASTER_MAX_SEGMENTS = 200
#: Images covering at least this fraction of a page with real line work make it mixed.
MIXED_IMAGE_COVERAGE = 0.15


@dataclass(frozen=True)
class PageKind:
    """``kind`` is ``"vector"``, ``"raster"`` or ``"mixed"``."""

    kind: str
    stats: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "stats": dict(self.stats)}


def kind_from_content(content: PageContent) -> PageKind:
    page_area = max(content.frame.crop_width_pt * content.frame.crop_height_pt, 1e-9)
    # Overlapping images are summed; cap so the ratio stays a fraction.
    coverage = min(content.image_area_pt2 / page_area, 1.0)
    n_seg = content.n_segments
    if coverage >= RASTER_IMAGE_COVERAGE and n_seg < RASTER_MAX_SEGMENTS:
        kind = "raster"
    elif coverage >= MIXED_IMAGE_COVERAGE:
        kind = "mixed"
    else:
        kind = "vector"
    stats = {
        "image_coverage": round(coverage, 4),
        "image_xobjects": content.n_image_xobjects,
        "inline_images": content.n_inline_images,
        "path_segments": n_seg,
        "path_paints": content.n_path_paints,
        "text_shows": content.n_text_shows,
        "form_placements": len(content.placements),
    }
    return PageKind(kind=kind, stats=stats)


def classify_page(pdf_path: "str | os.PathLike[str]", page_index: int) -> PageKind:
    """Classify one page by what its content stream draws.

    * ``raster``: images cover >= 50% of the page and it has < 200 path
      segments (a scan, perhaps with a few markups). Use the raster matcher.
    * ``mixed``: images cover >= 15% of the page but there is real line work
      (e.g. a scan with vector overlays, or a sheet with a large photo).
      The vector matcher can run but may miss symbols that are in the image.
    * ``vector``: everything else.
    """
    with open_pdf(pdf_path) as pdf:
        return kind_from_content(read_page_content(pdf, page_index))
