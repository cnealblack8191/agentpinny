"""Render service (contracts v1 sections 1, 2, 6, 9). Owned by the foundation session.

Module-level functions use a default RenderService rooted at $PINNY_DATA_DIR
(default ./pinny-data). Construct RenderService(data_dir) to inject your own.
"""

from .errors import (
    DocumentVersionNotFoundError,
    InvalidCropError,
    PageNotFoundError,
    PageTooLargeError,
    PdfEncryptedError,
    PdfTooManyPagesError,
    PdfUnreadableError,
    RenderError,
    UploadTooLargeError,
    VersionOwnershipConflictError,
)
from .frame import (
    CANONICAL_DPI,
    canonical_page_id,
    canonical_size,
    document_version_for_bytes,
    frame_descriptor,
    parse_canonical_page_id,
)
from .service import (
    DocumentVersion,
    PageInfo,
    RenderService,
    crop_renderer,
    decode_png_rgb,
    default_service,
    page_frame,
    render_page,
)

__all__ = [
    "CANONICAL_DPI",
    "DocumentVersion",
    "DocumentVersionNotFoundError",
    "InvalidCropError",
    "PageInfo",
    "PageNotFoundError",
    "PageTooLargeError",
    "PdfEncryptedError",
    "PdfTooManyPagesError",
    "PdfUnreadableError",
    "RenderError",
    "RenderService",
    "UploadTooLargeError",
    "VersionOwnershipConflictError",
    "canonical_page_id",
    "canonical_size",
    "crop_renderer",
    "decode_png_rgb",
    "default_service",
    "document_version_for_bytes",
    "frame_descriptor",
    "page_frame",
    "parse_canonical_page_id",
    "render_page",
]
