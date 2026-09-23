"""Pinny viewer and pin review interface (local web app).

Run with ``python -m pinny.viewer``. See ``docs/viewer.md``.
"""

from .errors import ViewerError
from .service import ViewerService

__all__ = ["ViewerError", "ViewerService"]
