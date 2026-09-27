"""The seam between the web tier and the workers (docs/training-site.md
section 9).

``RenderClient`` is everything ``ViewerService`` needs from the render
service. ``RenderService`` implements it in-process (development and
tests). ``JobRenderClient`` implements it without ever opening a PDF:
uploads are ingested, and pages rendered, by sandboxed jobs. It reads only
what those jobs wrote: ``version.json`` metadata and the PNG page cache.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import numpy as np

from pinny.jobs.queue import JobQueue
from pinny.jobs.tasks import staging_path
from pinny.render.errors import UploadTooLargeError
from pinny.render.service import DocumentVersion, PageInfo, RenderService, decode_png_rgb


class RenderClient(Protocol):
    def ingest_pdf(self, source, *, original_filename=None, document_id=None) -> DocumentVersion: ...
    def get_version(self, document_version: str) -> DocumentVersion: ...
    def list_versions(self, document_id: str | None = None) -> list[DocumentVersion]: ...
    def page_frame(self, document_version: str, page_index: int) -> dict: ...
    def render_page(self, document_version: str, page_index: int) -> np.ndarray: ...
    def render_page_png(self, document_version: str, page_index: int) -> bytes: ...
    def crop_renderer(self, spec: Any) -> bytes: ...
    def delete_version(self, document_version: str) -> None: ...


class JobRenderClient(RenderService):
    """``RenderService`` whose PDF work runs in sandboxed jobs."""

    render_service = "foundation+jobs"

    def __init__(self, data_dir: os.PathLike | str, jobs: JobQueue, **kw) -> None:
        super().__init__(data_dir, **kw)
        self.jobs = jobs

    def ingest_pdf(self, source: bytes | BinaryIO | os.PathLike | str, *, original_filename: str | None = None,
                   document_id: str | None = None) -> DocumentVersion:
        staging_id = uuid.uuid4().hex
        staged = staging_path(self.root, staging_id)
        try:
            size = 0
            with staged.open("wb") as out:
                for chunk in self._chunks(source):
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise UploadTooLargeError(
                            "upload_too_large",
                            f"The PDF is larger than the {self.max_upload_bytes // (1024 * 1024)} MB limit.")
                    out.write(chunk)
            meta = self.jobs.run("ingest", {"staging_id": staging_id, "filename": original_filename,
                                            "document_id": document_id})
        finally:
            staged.unlink(missing_ok=True)  # the job removes it too
        return DocumentVersion.from_dict(meta)

    def _render_uncached(self, document_version: str, page_index: int, page: PageInfo, png: Path) -> np.ndarray:
        self._render_job(document_version, page_index)
        return decode_png_rgb(png.read_bytes())

    def render_page_png(self, document_version: str, page_index: int) -> bytes:
        self._page(document_version, page_index)  # 404s for an unknown version or page
        png = self._version_dir(document_version) / "pages" / f"p{page_index}.png"
        if not png.is_file():
            self._render_job(document_version, page_index)
        return png.read_bytes()

    def _render_job(self, document_version: str, page_index: int) -> None:
        self.jobs.run("render_page", {"document_version": document_version, "page_index": page_index},
                      dedupe_key=f"render:{document_version}#p{page_index}")
