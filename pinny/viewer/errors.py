"""Viewer error type.

Follows contracts section 7: ``code`` is a stable snake_case id and the
message says what to do. It subclasses ``pinny.errors.PinnyError`` once the
foundation has pushed it; until then it is a plain exception with the same
shape.
"""

from __future__ import annotations

try:  # pragma: no cover - depends on the foundation branch being merged
    from pinny.errors import PinnyError as _Base
except ImportError:  # pragma: no cover
    _Base = Exception


class ViewerError(_Base):  # type: ignore[misc, valid-type]
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        try:
            super().__init__(code, message)
        except TypeError:  # pragma: no cover - base with another signature
            super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return self.message
