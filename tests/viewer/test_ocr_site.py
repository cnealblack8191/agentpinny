"""Optional OCR in the viewer (docs/ocr.md): off by default, an admin-only
site-wide switch, and sheet info read from the cached page raster."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pinny.ocr import OcrBox, OcrResult, OcrSettings, OcrWord, TesseractEngine  # noqa: E402
from tests.viewer.test_site import ADMIN, REVIEWER, call, err, site, upload  # noqa: E402,F401

HAVE_TESSERACT = TesseractEngine().is_available()


@pytest.fixture(autouse=True)
def no_env_override(monkeypatch):
    monkeypatch.delenv("PINNY_OCR_ENGINE", raising=False)
    monkeypatch.delenv("PINNY_TESSERACT_CMD", raising=False)


class FakeEngine:
    """Returns a small title block in the region it is asked to read."""
    name, license = "fake", "MIT"

    def __init__(self, gate=None):
        self.gate = gate
        self.calls = []

    def is_available(self):
        return True

    def recognize(self, page, settings=OcrSettings(), region=None):
        self.calls.append((page.shape, region))
        if self.gate is not None:
            self.gate.wait(10)
        x, y = (region.x, region.y) if region else (0, 0)
        words = [("SHEET", 0, 0, 20, 1), ("NO", 70, 0, 20, 1), ("E-101", 0, 30, 60, 2)]
        return OcrResult("fake", "1", settings, tuple(
            OcrWord(t, OcrBox(x + 10 + dx, y + 10 + dy, 20 * len(t), h), 95.0, (1, 1, ln))
            for t, dx, dy, h, ln in words), region)


def path(v, i=0):
    return f"/api/documents/{v}/pages/{i}/sheet-info"


def test_off_by_default(site):
    base, s, svc = site
    v = upload(base)["document_version"]
    code, _, raw = call(base, "GET", "/api/ocr", who=REVIEWER)
    state = json.loads(raw)
    assert code == 200 and state["enabled"] is False and state["engine"] == "none"
    assert [o["name"] for o in state["options"]][0] == "none"
    code, _, raw = call(base, "GET", path(v), who=REVIEWER)
    assert code == 200 and json.loads(raw) == {"sheet_info": None, "ocr": {"enabled": False}}
    code, _, raw = call(base, "POST", path(v), who=REVIEWER, body={})
    assert code == 409 and err(raw)["code"] == "ocr_disabled"


def test_only_an_admin_turns_it_on(site):
    base, s, svc = site
    code, _, raw = call(base, "POST", "/api/ocr", who=REVIEWER, body={"engine": "tesseract"})
    assert code == 403
    code, _, raw = call(base, "POST", "/api/ocr", who=ADMIN, body={"engine": "abbyy"})
    assert code == 400 and err(raw)["code"] == "unknown_engine"
    code, _, raw = call(base, "POST", "/api/ocr", who=ADMIN, body={})
    assert code == 400


def test_cannot_turn_on_an_engine_that_is_not_installed(site, monkeypatch, tmp_path):
    base, s, svc = site
    monkeypatch.setenv("PINNY_TESSERACT_CMD", str(tmp_path / "no-tesseract"))
    code, _, raw = call(base, "POST", "/api/ocr", who=ADMIN, body={"engine": "tesseract"})
    assert code == 409 and err(raw)["code"] == "ocr_unavailable"
    assert "apt install tesseract-ocr" in err(raw)["message"]
    assert json.loads(call(base, "GET", "/api/ocr")[2])["enabled"] is False


def test_env_override_is_reported(site, monkeypatch):
    base, s, svc = site
    monkeypatch.setenv("PINNY_OCR_ENGINE", "none")
    state = json.loads(call(base, "GET", "/api/ocr")[2])
    assert state["overridden"] is True and state["enabled"] is False


def test_read_save_and_purge(site):
    base, s, svc = site
    fake = FakeEngine()
    svc.ocr._engine_factory = lambda: fake
    v = upload(base)["document_version"]
    code, _, raw = call(base, "POST", path(v), who=REVIEWER, body={})
    assert code == 200, raw
    info = json.loads(raw)["sheet_info"]
    assert info["sheet_number"]["value"] == "E-101" and info["sheet_number"]["method"] == "label"
    assert info["read_by"] == REVIEWER and info["page_index"] == 0
    shape, region = fake.calls[0]
    frame = svc.frame(v, 0)
    assert shape[:2] == (frame["height"], frame["width"])
    assert region is not None and region.x2 == frame["width"] and region.y2 == frame["height"]
    assert "words" not in json.dumps(info)  # the raw OCR output is not stored
    # Shown again without re-reading.
    saved = json.loads(call(base, "GET", path(v), who=REVIEWER)[2])["sheet_info"]
    assert saved["sheet_number"]["value"] == "E-101" and len(fake.calls) == 1
    assert any(a["action"] == "sheet_info_read" for a in s.sitedb.audit_log())
    folder = svc.data_dir / "ocr" / v.split(":")[1]
    assert folder.is_dir()
    code, _, _ = call(base, "DELETE", f"/api/documents/{v}")
    assert code == 200 and not folder.exists()


def test_region_is_checked(site):
    base, s, svc = site
    svc.ocr._engine_factory = FakeEngine
    v = upload(base)["document_version"]
    frame = svc.frame(v, 0)
    for bad in ({"x": 0}, {"x": -1, "y": 0, "width": 10, "height": 10},
                {"x": 0, "y": 0, "width": frame["width"] + 1, "height": 10}, "corner"):
        code, _, raw = call(base, "POST", path(v), body={"region": bad})
        assert code == 400, (bad, raw)
    code, _, raw = call(base, "POST", path(v), body={"region": {"x": 0, "y": 0, "width": 50, "height": 40}})
    assert code == 200, raw


def test_unknown_page_is_404(site):
    base, s, svc = site
    svc.ocr._engine_factory = FakeEngine
    v = upload(base)["document_version"]
    assert call(base, "POST", path(v, 99), body={})[0] == 404
    assert call(base, "GET", path(v, 99))[0] == 404


def test_one_read_at_a_time(site):
    base, s, svc = site
    gate = threading.Event()
    fake = FakeEngine(gate)
    svc.ocr._engine_factory = lambda: fake
    v = upload(base)["document_version"]
    first = {}
    t = threading.Thread(target=lambda: first.update(r=call(base, "POST", path(v), body={})))
    t.start()
    for _ in range(200):
        if fake.calls:
            break
        threading.Event().wait(0.05)
    code, _, raw = call(base, "POST", path(v), body={})
    assert code == 429 and err(raw)["code"] == "ocr_busy"
    gate.set()
    t.join(20)
    assert first["r"][0] == 200


@pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract executable not installed")
def test_real_engine_end_to_end(site):
    base, s, svc = site
    code, _, raw = call(base, "POST", "/api/ocr", who=ADMIN, body={"engine": "tesseract"})
    assert code == 200 and json.loads(raw)["enabled"] is True, raw
    assert any(a["action"] == "ocr_engine_set" for a in s.sitedb.audit_log())
    v = upload(base)["document_version"]
    code, _, raw = call(base, "POST", path(v), who=REVIEWER, body={})
    assert code == 200, raw
    info = json.loads(raw)["sheet_info"]
    assert set(info) >= {"sheet_number", "sheet_title", "revision", "warnings", "engine"}
    assert info["engine"]["name"] == "tesseract"
