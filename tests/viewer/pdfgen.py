"""Real vector PDFs for viewer tests, built with the foundation's factory.

Each unrotated page carries duplex-receptacle glyphs at RECEPTACLES_PT (PDF
user space: points, origin bottom-left). ``receptacle_centres_px`` gives
where they land on the canonical 200 DPI raster (contracts §2).
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from pinny.render import CANONICAL_DPI
from tests.factory import PageSpec, build_pdf

RECEPTACLES_PT: Tuple[Tuple[float, float], ...] = (
    (108.0, 684.0), (306.0, 684.0), (504.0, 684.0), (108.0, 396.0), (504.0, 108.0),
)
_S = CANONICAL_DPI / 72.0


def make_pdf(pages: Iterable[Tuple[float, float, int]] = ((612, 792, 0),), tag: str = "") -> bytes:
    specs = []
    for w, h, rot in pages:
        recs = [p for p in RECEPTACLES_PT if p[0] < w and p[1] < h] if rot == 0 else []
        specs.append(PageSpec(width_pt=w, height_pt=h, rotate=rot, receptacles=recs,
                              content=f"% {tag}\n" if tag else ""))
    return build_pdf(specs)


def receptacle_centres_px(height_pt: float = 792.0) -> List[Tuple[float, float]]:
    """Raster centres of RECEPTACLES_PT on an unrotated page of that height."""
    return [(x * _S, (height_pt - y) * _S) for x, y in RECEPTACLES_PT]
