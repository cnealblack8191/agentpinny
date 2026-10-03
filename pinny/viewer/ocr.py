"""Optional OCR in the viewer: the site-wide switch and sheet info (docs/ocr.md).

OCR is off until an admin turns it on. The choice is saved in
``$PINNY_DATA_DIR/settings.json``; ``$PINNY_OCR_ENGINE`` on the server
overrides it. Sheet info reads the page raster the render service already
cached, so the web process never opens a PDF. Tesseract runs as a child of
the web process, which shares one CPU and 1.5 GB (deploy/systemd), so:

* only the title-block corner (or a region at most ``MAX_OCR_PIXELS``) is read,
* one read runs at a time; a second gets ``ocr_busy`` (429) at once,
* each result is saved per page and shown again without re-reading.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from pinny.ocr import (OcrBox, OcrError, OcrSettings, engine_options, get_engine, read_sheet_info,
                       selected_engine_name, set_engine)

from .errors import ViewerError

#: Largest area read in one request, before upscaling. The default title-block
#: corner of an Arch D sheet at 200 DPI is about 5.4 Mpx.
MAX_OCR_PIXELS = 8_000_000
#: A title block read at 300 DPI in four rotations takes a few seconds.
OCR_TIMEOUT_S = 90.0
SHEET_INFO_DIR = "ocr"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class OcrSiteService:
    def __init__(self, viewer, *, engine_factory: Optional[Callable] = None) -> None:
        self.viewer = viewer
        self.data_dir = Path(viewer.data_dir)
        self._engine_factory = engine_factory or (lambda: get_engine(data_dir=self.data_dir))
        self._busy = threading.Lock()

    # ------------------------------------------------------------- switch
    def state(self) -> dict:
        options = engine_options(self.data_dir)
        selected = selected_engine_name(data_dir=self.data_dir)
        chosen = next(o for o in options if o["name"] == selected)
        return {"enabled": selected != "none", "engine": selected, "available": chosen["available"],
                "overridden": chosen["overridden"], "options": options}

    def enabled(self) -> bool:
        """Cheap: reads the setting only, without probing the engine."""
        return selected_engine_name(data_dir=self.data_dir) != "none"

    def set_engine(self, name) -> dict:
        if not isinstance(name, str):
            raise ViewerError("bad_request", 'Send {"engine": "none"} or {"engine": "tesseract"}.')
        option = next((o for o in engine_options(self.data_dir) if o["name"] == name.strip().lower()), None)
        if option is not None and not option["available"]:
            raise OcrError("ocr_unavailable",
                           f"{option['label']} is not installed on the server. Install it "
                           "(sudo apt install tesseract-ocr) and try again.")
        set_engine(name, self.data_dir)
        return self.state()

    # --------------------------------------------------------- sheet info
    def _path(self, document_version: str, page_index: int) -> Path:
        return self.data_dir / SHEET_INFO_DIR / document_version.split(":", 1)[-1] / f"p{page_index}.json"

    def saved(self, document_version: str, page_index: int) -> Optional[dict]:
        self.viewer.render.page_frame(document_version, page_index)  # 404s for an unknown page
        try:
            return json.loads(self._path(document_version, page_index).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def read(self, document_version: str, page_index: int, region=None, *, requested_by: str) -> dict:
        frame = self.viewer.render.page_frame(document_version, page_index)  # 404 first
        engine = self._engine_factory()
        if engine is None:
            raise OcrError("ocr_disabled", "OCR is turned off. An admin can turn it on under OCR (admin).")
        box = self._region(region, frame)
        if not self._busy.acquire(blocking=False):
            raise OcrError("ocr_busy", "Another sheet is being read. Try again in a few seconds.")
        try:
            page = self.viewer.render.render_page(document_version, page_index)
            settings = OcrSettings(max_page_pixels=MAX_OCR_PIXELS, max_runtime_seconds=OCR_TIMEOUT_S)
            info = read_sheet_info(page, engine, settings, region=box)
        finally:
            self._busy.release()
        out = info.to_dict()
        out.update(document_version=document_version, page_index=page_index,
                   read_at=_now(), read_by=requested_by)
        self._save(self._path(document_version, page_index), out)
        return out

    @staticmethod
    def _region(region, frame: dict) -> Optional[OcrBox]:
        """None (the default title-block corner) or a checked box in canonical px."""
        if region is None:
            return None
        try:
            box = OcrBox(*(int(region[k]) for k in ("x", "y", "width", "height")))
        except (KeyError, TypeError, ValueError, OcrError):
            raise ViewerError("bad_request", "region must be {x, y, width, height} in page pixels.") from None
        if box.x < 0 or box.y < 0 or box.x2 > frame["width"] or box.y2 > frame["height"]:
            raise ViewerError("bad_request", "region must lie inside the page.")
        if box.width * box.height > MAX_OCR_PIXELS:
            raise ViewerError("bad_request", "That region is too large to read. Draw a box around the title block.")
        return box

    @staticmethod
    def _save(path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sheet-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def purge(self, document_version: str) -> None:
        shutil.rmtree(self._path(document_version, 0).parent, ignore_errors=True)
