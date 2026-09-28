"""The viewer with sandboxed jobs on (docs/training-site.md section 4): the
web process never opens a PDF. The test makes every PDF entry point raise
in this process; the job children are separate processes and unaffected.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pdfgen import RECEPTACLES_PT, make_pdf, receptacle_centres_px  # noqa: E402

from pinny.jobs import JobQueue  # noqa: E402
from pinny.jobs.worker import WorkerPool  # noqa: E402
from pinny.render import pdf as pdf_module  # noqa: E402
from pinny.viewer import ViewerService  # noqa: E402
from pinny.viewer.server import make_server  # noqa: E402
from tests.factory import PageSpec, build_pdf  # noqa: E402
from tests.viewer.test_viewer_api import _req, _template_for  # noqa: E402


def rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def svc(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the web process opened a PDF")
    for name in ("inspect", "render_rgb"):
        monkeypatch.setattr(pdf_module, name, forbidden)
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    jobs = JobQueue(tmp_path)
    pool = WorkerPool(jobs, tmp_path).start()
    s = ViewerService(tmp_path, jobs=jobs)
    yield s
    s.close()
    pool.stop()


def test_upload_render_scan_review_delete_without_opening_a_pdf(svc, tmp_path):
    doc = svc.upload(make_pdf(), "a.pdf")
    v = doc["document_version"]
    assert doc["page_count"] == 1
    assert list((tmp_path / "tmp").glob("stage_*")) == []  # staging removed
    frame = svc.frame(v, 0)
    png = svc.raster_png(v, 0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    st = svc.scan(document_version=v, page_index=0, template_box=_template_for(doc, frame), request_id=rid())
    got = sorted((round(p["x"]), round(p["y"])) for p in st["pins"])
    assert got == sorted((round(x), round(y)) for x, y in receptacle_centres_px())
    assert st["template"]["sha256"] and st["detector"]["name"] == "opencv-template"
    kinds = {j.kind for j in svc.jobs.list()}
    assert kinds == {"ingest", "render_page", "template", "scan"}
    # Review: the training crop is cut from the PNG the job wrote.
    r = svc.act(st["scan_id"], {"action": "approve", "pin_id": st["pins"][0]["pin_id"], "request_id": rid()})
    assert r["pin"]["state"] == "approved"
    assert svc._db(svc.store.crop_status_counts) == {"written": 1}
    svc.delete_document(v)
    with pytest.raises(Exception):
        svc.frame(v, 0)


def test_job_errors_reach_the_user(svc):
    tiny = build_pdf([PageSpec(width_pt=10, height_pt=10)])
    doc = svc.upload(tiny, "tiny.pdf")
    with pytest.raises(Exception) as e:  # a 44 px template does not fit a 28 px page
        svc.scan(document_version=doc["document_version"], page_index=0,
                 template_box={"x": 0, "y": 0, "width": 44, "height": 44}, request_id=rid())
    assert getattr(e.value, "code", None) == "invalid_template_box"
    with pytest.raises(Exception) as e:
        svc.upload(b"%PDF-1.4 not really", "bad.pdf")
    assert e.value.code == "pdf_unreadable" and e.value.status == 422


def test_batch_runs_through_jobs(svc):
    specs = [PageSpec(receptacles=list(RECEPTACLES_PT[:n])) for n in (5, 2, 0)]
    specs.append(PageSpec(width_pt=10, height_pt=10))
    doc = svc.upload(build_pdf(specs), "set.pdf")
    v = doc["document_version"]
    b = svc.start_batch(document_version=v, request_id=rid(), template_box=_template_for(doc, svc.frame(v, 0)))
    b = svc.wait_batch(b["batch_id"], timeout=300)
    assert [p["status"] for p in b["pages"]] == ["done", "done", "done", "failed"]
    assert [p["counts"].get("total") for p in b["pages"][:3]] == [5, 2, 0]
    assert b["pages"][3]["error"]["code"] and "internal" not in b["pages"][3]["error"]["message"]
    scans = [j for j in svc.jobs.list() if j.kind == "scan"]
    assert len(scans) == 4 and all(j.priority == 10 for j in scans)  # batch pages yield to single scans


def test_http_upload_and_raster_through_jobs(svc):
    httpd = make_server(svc, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, _, raw = _req("POST", base + "/api/documents?filename=a.pdf", make_pdf(),
                            {"Content-Type": "application/pdf"})
        assert code == 201, raw
        v = json.loads(raw)["document_version"]
        code, headers, png = _req("GET", f"{base}/api/documents/{urllib_quote(v)}/pages/0/raster.png")
        assert code == 200 and png[:4] == b"\x89PNG"
    finally:
        httpd.shutdown()
        httpd.server_close()


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def test_settings_require_sandboxed_jobs_in_production(tmp_path):
    from pinny.viewer.settings import ConfigError, Settings
    env = {"PINNY_ENV": "production", "PINNY_DATA_DIR": str(tmp_path), "PINNY_ORIGIN": "https://p.test",
           "PINNY_ADMIN_EMAILS": "a@b.c", "PINNY_VERSION": "v"}
    assert Settings.from_env(env).jobs == "sandbox"
    assert Settings.from_env(dict(env, PINNY_JOBS="external")).jobs == "external"
    with pytest.raises(ConfigError, match="sandboxed"):
        Settings.from_env(dict(env, PINNY_JOBS="inprocess"))
    assert Settings.from_env({}, data_dir=tmp_path).jobs == "inprocess"
    with pytest.raises(ConfigError):
        Settings.from_env({"PINNY_JOBS": "threads"}, data_dir=tmp_path)
