"""Shared error base (contracts v1 section 7). Owned by the foundation session.

Every module's errors subclass PinnyError(code, message). `code` is a stable
snake_case id; `message` tells the user what to do. HTTP layers map a
PinnyError to a 4xx (`http_status`) with {"error": {"code", "message"}};
anything else is a 500.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PinnyError(Exception):
    http_status: int = 400

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        if not _CODE_RE.fullmatch(code):
            raise ValueError(f"PinnyError code must be snake_case, got {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


class NotFoundError(PinnyError):
    http_status = 404


class ConflictError(PinnyError):
    http_status = 409


class PayloadTooLargeError(PinnyError):
    http_status = 413


class UnprocessableError(PinnyError):
    http_status = 422
