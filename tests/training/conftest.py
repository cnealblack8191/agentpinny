"""Fixtures for dataset tests: a real RenderService and LearningStore in tmp_path.

Pages are tiny synthetic PDFs (tests/factory.py). Scans are recorded by hand
so the tests control exactly which pins exist and how they are reviewed.
"""

from __future__ import annotations

import itertools

import pytest

from pinny.learning import Box, Detection, LearningStore, Scan
from pinny.render import RenderService
from tests.factory import PageSpec, build_pdf

PAGE_PT = 216.0  # 3 in square -> 600 x 600 px at 200 DPI
PAGE_PX = 600


def pt_to_px(cx: float, cy: float) -> tuple[float, float]:
    return cx * 200 / 72, (PAGE_PT - cy) * 200 / 72


class World:
    """A data dir with the render service and learning store sharing it, like the viewer."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.render = RenderService(data_dir)
        self.store = LearningStore(data_dir, crop_renderer=self.render.crop_renderer, default_reviewer="test")
        self._req = itertools.count()
        self._scan = itertools.count()

    def rid(self) -> str:
        return f"req-{next(self._req)}"

    def add_document(self, receptacles_pt, *, pages: int = 1, document_id: str | None = None):
        """Ingest a PDF whose every page has receptacles at ``receptacles_pt``; returns the version."""
        pdf = build_pdf([PageSpec(width_pt=PAGE_PT, height_pt=PAGE_PT, receptacles=list(receptacles_pt))
                         for _ in range(pages)])
        return self.render.ingest_pdf(pdf, document_id=document_id)

    def add_scan(self, version, points_px, *, page_index: int = 0) -> str:
        scan_id = f"scan-{next(self._scan)}"
        dets = [Detection(f"det-{i}", Box(int(x) - 20, int(y) - 20, 40, 40), score=0.9, x=x, y=y)
                for i, (x, y) in enumerate(points_px)]
        info = version.pages[page_index]
        self.store.record_scan(Scan(scan_id=scan_id, document_id=version.document_id,
                                    document_version=version.document_version, page_index=page_index,
                                    frame_width=info.width_px, frame_height=info.height_px,
                                    detector_name="opencv-template", detector_version="git:test",
                                    detector_settings={}, created_at="2026-09-24T00:00:00.000Z"), dets)
        return scan_id

    def approve(self, scan_id, pin_id):
        return self.store.approve(scan_id, pin_id, request_id=self.rid(), source="test")

    def reject(self, scan_id, pin_id):
        return self.store.reject(scan_id, pin_id, request_id=self.rid(), source="test")

    def add_manual(self, scan_id, x, y):
        return self.store.add_manual(scan_id, x, y, request_id=self.rid(), source="test").pin.pin_id

    def remove_manual(self, scan_id, pin_id):
        return self.store.remove_manual(scan_id, pin_id, request_id=self.rid(), source="test")

    def export(self, **kw):
        return self.store.export(**kw)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "data")
    yield w
    w.store.close()
