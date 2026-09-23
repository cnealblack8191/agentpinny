"""Render-service errors. All subclass pinny.errors.PinnyError."""

from pinny.errors import ConflictError, NotFoundError, PayloadTooLargeError, PinnyError, UnprocessableError


class RenderError(PinnyError):
    """Base for render/upload errors."""


class PdfUnreadableError(RenderError, UnprocessableError):
    pass


class PdfEncryptedError(RenderError, UnprocessableError):
    pass


class PdfTooManyPagesError(RenderError, UnprocessableError):
    pass


class PageTooLargeError(RenderError, UnprocessableError):
    pass


class UploadTooLargeError(RenderError, PayloadTooLargeError):
    pass


class DocumentVersionNotFoundError(RenderError, NotFoundError):
    pass


class PageNotFoundError(RenderError, NotFoundError):
    pass


class VersionOwnershipConflictError(RenderError, ConflictError):
    pass


class InvalidCropError(RenderError, UnprocessableError):
    pass
