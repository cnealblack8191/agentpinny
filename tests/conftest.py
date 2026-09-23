"""Shared pytest fixtures (foundation-owned)."""

from __future__ import annotations

import pytest

from pinny.render import RenderService
from tests.factory import PageSpec, build_pdf


@pytest.fixture
def render_service(tmp_path) -> RenderService:
    """A RenderService rooted in a temporary data dir."""
    return RenderService(tmp_path / "pinny-data")


@pytest.fixture
def make_pdf():
    """make_pdf([PageSpec(...), ...], password=None) -> bytes. Synthetic only."""
    return build_pdf


__all__ = ["PageSpec"]
