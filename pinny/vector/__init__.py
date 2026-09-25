"""Vector-first symbol matching on PDF drawing primitives.

Most construction drawings are CAD exports whose symbols are vector paths,
often a single block definition (Form XObject) placed many times. Matching
those primitives is faster and more exact than raster template matching.
Use :func:`classify_page` to decide, then :class:`VectorMatcher` on vector
pages and the raster matcher (``pinny.detection``) otherwise.

All public coordinates are canonical raster px (contracts section 2).
"""

from .classify import PageKind, classify_page
from .errors import RasterPageError, VectorMatchError
from .frame import CANONICAL_DPI, PT_TO_PX, PageFrame
from .matcher import VectorDetection, VectorMatcher, VectorResult, VectorSettings

__all__ = [
    "CANONICAL_DPI",
    "PT_TO_PX",
    "PageFrame",
    "PageKind",
    "RasterPageError",
    "VectorDetection",
    "VectorMatchError",
    "VectorMatcher",
    "VectorResult",
    "VectorSettings",
    "classify_page",
]
