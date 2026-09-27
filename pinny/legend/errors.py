from __future__ import annotations


class LegendError(ValueError):
    """A legend can't be read or edited. ``code`` is a stable id; the message
    says what to do. Same shape as the other Pinny module errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"LegendError({self.code!r}, {str(self)!r})"
