"""Viewer service and HTTP API tests (stub render service).

Run: python -m pytest tests/viewer
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pdfgen import make_pdf  # noqa: E402

from pinny.viewer import ViewerError, ViewerService  # noqa: E402
from pinny.viewer.render_stub import (SYMBOL_SIZE, stub_alignment_points,  # noqa: E402
                                      stub_symbol_centres)
from pinny.viewer.server import make_server  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    s = ViewerService(tmp_path)
    yield s
    s.close()


def _template_for(doc, frame, page=0):
    x, y, _ = next(t for t in stub_symbol_centres(doc["document_version"], page, frame["width"],
                                                  frame["height"]) if t[2] == 0)
    h = SYMBOL_SIZE // 2 + 2
    return {"x": x - h, "y": y - h, "width": 2 * h, "height": 2 * h}


def _scan(svc, doc, page=0, **kw):
    frame = svc.frame(doc["document_version"], page)
    return svc.scan(document_version=doc["document_version"], page_index=page,
                    template_box=_template_for(doc, frame, page), request_id=kw.pop("request_id", rid()),
                    **kw)


# ---------------------------------------------------------------- upload
def test_upload_versions_and_frames(svc):
    pdf = make_pdf([(612, 792, 0), (612, 792, 90), (1728, 1152, 0)])
    doc = svc.upload(pdf, "plan.pdf")
    assert doc["document_version"] == "sha256:" + hashlib.sha256(pdf).hexdigest()
    assert doc["page_count"] == 3 and doc["render_service"] == "stub"
    again = svc.upload(pdf, "plan.pdf")
    assert again["document_id"] == doc["document_id"]  # same bytes, same identity
    f0, f1, f2 = (svc.frame(doc["document_version"], i) for i in range(3))
    assert (f0["width"], f0["height"]) == (1700, 2200)
    assert (f1["width"], f1["height"]) == (2200, 1700)  # /Rotate 90 applied: shown upright
    assert (f2["width"], f2["height"]) == (4800, 3200)  # 24x16 in at 200 DPI
    assert f0["dpi"] == 200 and f0["origin"] == "top-left" and f0["y_axis"] == "down"
    raster = svc.render.render_page(doc["document_version"], 1)
    assert raster.shape == (1700, 2200, 3) and raster.dtype.name == "uint8"


@pytest.mark.parametrize("data,code", [(b"", "empty_upload"), (b"hello", "not_a_pdf"),
                                       (b"%PDF-1.4\n%%EOF", "no_pages")])
def test_upload_errors(svc, data, code):
    with pytest.raises(ViewerError) as e:
        svc.upload(data, "x.pdf")
    assert e.value.code == code


def test_unknown_page(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    with pytest.raises(ViewerError) as e:
        svc.frame(doc["document_version"], 5)
    assert e.value.code == "unknown_page" and e.value.status == 404


# ------------------------------------------------------------------ scan
def test_scan_finds_stub_symbols_at_exact_centres(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    st = _scan(svc, doc)
    want = {(x, y) for x, y, _ in stub_symbol_centres(doc["document_version"], 0, 1700, 2200)}
    got = {(p["x"], p["y"]) for p in st["pins"]}
    assert got == want
    assert all(p["state"] == "unreviewed" and p["origin"] == "machine" for p in st["pins"])
    assert all(0.8 <= p["score"] <= 1.0 for p in st["pins"])
    assert st["coordinate_frame"]["width"] == 1700
    assert st["document"]["page_index"] == 0


def test_scan_retry_with_same_request_id_returns_same_scan(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    r = rid()
    a = _scan(svc, doc, request_id=r)
    b = _scan(svc, doc, request_id=r)
    assert a["scan_id"] == b["scan_id"]
    assert len(svc.page_scans(doc["document_version"], 0)) == 1


def test_scan_empty_result_and_bad_template(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    v = doc["document_version"]
    # Blank area of grid: flat template is rejected by the detector with a message.
    with pytest.raises(ViewerError) as e:
        svc.scan(document_version=v, page_index=0, request_id=rid(),
                 template_box={"x": 1510, "y": 1210, "width": 30, "height": 30})
    assert e.value.code
    with pytest.raises(ViewerError) as e:
        svc.scan(document_version=v, page_index=0, request_id=rid(),
                 template_box={"x": 1690, "y": 10, "width": 30, "height": 30})
    assert e.value.code == "invalid_template_box"
    # A template of the banner text matches nothing else at a high threshold.
    st = svc.scan(document_version=v, page_index=0, request_id=rid(), threshold=0.99,
                  template_box={"x": 60, "y": 90, "width": 300, "height": 30})
    assert all(p["box"]["x"] == 60 for p in st["pins"])  # at most itself


# ---------------------------------------------------------------- review
def test_review_actions_persist_and_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path)
    doc = svc.upload(make_pdf(), "a.pdf")
    st = _scan(svc, doc)
    sid = st["scan_id"]
    p1, p2 = st["pins"][0], st["pins"][1]
    r = svc.act(sid, {"action": "approve", "pin_id": p1["pin_id"], "request_id": rid(),
                      "expected_version": p1["version"]})
    assert r["pin"]["state"] == "approved" and r["pin"]["version"] == 2
    r = svc.act(sid, {"action": "delete_pin", "pin_id": p2["pin_id"], "request_id": rid()})
    assert r["pin"]["state"] == "rejected" and r["action"] == "reject"
    add_id = rid()
    r = svc.act(sid, {"action": "add_manual", "x": 12.25, "y": 2199.5, "request_id": add_id})
    manual = r["pin"]
    assert manual["origin"] == "manual" and manual["state"] == "added"
    assert (manual["x"], manual["y"]) == (12.25, 2199.5)
    # Replaying the add creates nothing new.
    again = svc.act(sid, {"action": "add_manual", "x": 12.25, "y": 2199.5, "request_id": add_id})
    assert again["replayed"] and again["pin"]["pin_id"] == manual["pin_id"]
    r = svc.act(sid, {"action": "delete_pin", "pin_id": manual["pin_id"], "request_id": rid()})
    assert r["pin"]["state"] == "removed"
    before = svc.scan_state(sid)
    report_before = svc.report(sid)
    svc.close()

    svc2 = ViewerService(tmp_path)
    try:
        after = svc2.scan_state(sid)
        assert after["pins"] == before["pins"]  # same ids, coordinates, states, versions
        report_after = svc2.report(sid)
        for k in ("original_detections", "corrected_pins", "review_events"):
            assert report_after[k] == report_before[k]
    finally:
        svc2.close()


def test_stale_version_and_invalid_transitions(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    st = _scan(svc, doc)
    sid, p = st["scan_id"], st["pins"][0]
    svc.act(sid, {"action": "approve", "pin_id": p["pin_id"], "request_id": rid()})
    with pytest.raises(ViewerError) as e:
        svc.act(sid, {"action": "reject", "pin_id": p["pin_id"], "request_id": rid(),
                      "expected_version": 1})
    assert e.value.code == "stale_version" and e.value.status == 409
    m = svc.act(sid, {"action": "add_manual", "x": 5, "y": 5, "request_id": rid()})["pin"]
    with pytest.raises(ViewerError) as e:
        svc.act(sid, {"action": "approve", "pin_id": m["pin_id"], "request_id": rid()})
    assert e.value.code == "invalid_transition"
    with pytest.raises(ViewerError) as e:
        svc.act(sid, {"action": "approve", "pin_id": "nope", "request_id": rid()})
    assert e.value.status == 404
    with pytest.raises(ViewerError) as e:
        svc.act(sid, {"action": "approve", "pin_id": p["pin_id"], "request_id": "not-a-uuid"})
    assert e.value.code == "invalid_request_id"


@pytest.mark.parametrize("x,y", [(-0.01, 5), (5, -1), (1700.01, 5), (5, 2200.5), ("1", 2),
                                 (float("nan"), 1)])
def test_manual_pin_must_be_on_page(svc, x, y):
    doc = svc.upload(make_pdf(), "a.pdf")
    sid = _scan(svc, doc)["scan_id"]
    with pytest.raises(ViewerError) as e:
        svc.act(sid, {"action": "add_manual", "x": x, "y": y, "request_id": rid()})
    assert e.value.code in ("point_outside_page", "invalid_point")


def test_page_edges_are_allowed(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    sid = _scan(svc, doc)["scan_id"]
    for x, y in [(0, 0), (1700, 2200), (1700, 0), (0, 2200)]:
        pin = svc.act(sid, {"action": "add_manual", "x": x, "y": y, "request_id": rid()})["pin"]
        assert (pin["x"], pin["y"]) == (x, y)


def test_rescan_keeps_previous_scan_and_its_reviews(svc):
    doc = svc.upload(make_pdf(), "a.pdf")
    first = _scan(svc, doc)
    p = first["pins"][0]
    svc.act(first["scan_id"], {"action": "approve", "pin_id": p["pin_id"], "request_id": rid()})
    svc.act(first["scan_id"], {"action": "add_manual", "x": 50, "y": 60, "request_id": rid()})
    second = _scan(svc, doc)
    assert second["scan_id"] != first["scan_id"]
    assert all(q["state"] == "unreviewed" for q in second["pins"])
    kept = svc.scan_state(first["scan_id"])
    assert kept["counts"]["approved"] == 1 and kept["counts"]["added"] == 1
    listed = svc.page_scans(doc["document_version"], 0)
    assert [s["scan_id"] for s in listed] == [first["scan_id"], second["scan_id"]]


# ---------------------------------------------------------------- report
def test_report_keeps_original_and_corrected_apart_and_original_unchanged(svc, tmp_path):
    doc = svc.upload(make_pdf(), "a.pdf")
    st = _scan(svc, doc)
    sid = st["scan_id"]
    original_before = json.dumps(svc.report(sid)["original_detections"], sort_keys=True)
    svc.act(sid, {"action": "delete_pin", "pin_id": st["pins"][0]["pin_id"], "request_id": rid()})
    svc.act(sid, {"action": "approve", "pin_id": st["pins"][1]["pin_id"], "request_id": rid()})
    svc.act(sid, {"action": "add_manual", "x": 100.5, "y": 200.25, "request_id": rid()})
    rep = svc.report(sid)
    assert json.dumps(rep["original_detections"], sort_keys=True) == original_before
    orig, corr = rep["original_detections"], rep["corrected_pins"]
    assert orig["provenance"] == "original_detector_output"
    assert corr["provenance"] == "reviewed_corrections"
    assert orig["document"] == corr["document"] == rep["document"]
    assert orig["coordinate_frame"] == corr["coordinate_frame"]
    assert len(orig["detections"]) == len(st["pins"])
    assert all(d["source"] == "detector" and d["confidence"] == d["score"] for d in orig["detections"])
    final = {(p["x"], p["y"], p["state"]) for p in corr["final_pins"]}
    assert (100.5, 200.25, "added") in final
    assert len(corr["final_pins"]) == 2
    assert [e["action"] for e in rep["review_events"]] == ["reject", "approve", "add"]

    # The original part is accepted by the evaluator's `pending` command.
    det_path = tmp_path / "detections.json"
    det_path.write_text(json.dumps(orig))
    d = orig["document"]
    fr = orig["coordinate_frame"]
    run = subprocess.run(
        [sys.executable, "-m", "pinny_eval", "pending", "--detections", str(det_path),
         "--document-id", d["document_id"], "--document-version", d["document_version"],
         "--page", str(d["page_index"]), "--width", str(fr["width"]), "--height", str(fr["height"]),
         "--json-out", str(tmp_path / "r.json")],
        cwd=REPO / "evaluation", capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    # The corrected set is refused as detections.
    corr_path = tmp_path / "corrected.json"
    corr_path.write_text(json.dumps(dict(orig, provenance="reviewed_corrections")))
    run = subprocess.run(
        [sys.executable, "-m", "pinny_eval", "pending", "--detections", str(corr_path),
         "--document-id", d["document_id"], "--document-version", d["document_version"],
         "--page", str(d["page_index"]), "--width", str(fr["width"]), "--height", str(fr["height"])],
        cwd=REPO / "evaluation", capture_output=True, text=True)
    assert run.returncode == 2


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def server(svc):
    httpd = make_server(svc, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _req(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    h = {"Content-Type": "application/json"} if isinstance(body, dict) else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, r.headers, raw
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_http_flow(server):
    pdf = make_pdf([(612, 792, 0)])
    code, _, raw = _req("POST", server + "/api/documents?filename=a.pdf", pdf,
                        {"Content-Type": "application/pdf"})
    assert code == 201
    doc = json.loads(raw)
    v = urllib.request.quote(doc["document_version"], safe="")
    code, _, raw = _req("GET", f"{server}/api/documents/{v}/pages/0/frame")
    frame = json.loads(raw)
    assert frame["width"] == 1700 and frame["render_service"] == "stub"
    code, headers, png = _req("GET", f"{server}/api/documents/{v}/pages/0/raster.png")
    assert code == 200 and headers["Content-Type"] == "image/png"
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (2200, 1700)
    for x, y in stub_alignment_points(1700, 2200):  # red marker pixels around the point
        assert tuple(img[y, x][::-1]) == (255, 0, 0)
    code, _, raw = _req("POST", server + "/api/scans", {
        "document_version": doc["document_version"], "page_index": 0,
        "template_box": _template_for(doc, frame), "request_id": rid()})
    assert code == 201
    st = json.loads(raw)
    sid = st["scan_id"]
    code, _, raw = _req("POST", f"{server}/api/scans/{sid}/actions",
                        {"action": "approve", "pin_id": st["pins"][0]["pin_id"], "request_id": rid()})
    assert code == 200 and json.loads(raw)["pin"]["state"] == "approved"
    code, _, raw = _req("POST", f"{server}/api/scans/{sid}/actions",
                        {"action": "add_manual", "x": -5, "y": 1, "request_id": rid()})
    assert code == 400 and json.loads(raw)["error"]["code"] == "point_outside_page"
    code, headers, raw = _req("GET", f"{server}/api/scans/{sid}/report")
    assert code == 200 and "attachment" in headers["Content-Disposition"]
    assert json.loads(raw)["corrected_pins"]["counts"]["approved"] == 1
    code, _, raw = _req("GET", f"{server}/api/documents/{v}/pages/0/scans")
    assert [s["scan_id"] for s in json.loads(raw)["scans"]] == [sid]


def test_http_errors_and_static(server):
    code, _, raw = _req("POST", server + "/api/documents?filename=x.txt", b"not a pdf")
    assert code == 400 and json.loads(raw)["error"]["code"] == "not_a_pdf"
    code, _, raw = _req("GET", server + "/api/scans/nope")
    assert code == 404 and json.loads(raw)["error"]["message"]
    code, _, raw = _req("POST", server + "/api/scans", b"{bad", {"Content-Type": "application/json"})
    assert code == 400 and json.loads(raw)["error"]["code"] == "bad_json"
    code, headers, raw = _req("GET", server + "/")
    assert code == 200 and b"Pinny" in raw
    code, headers, _ = _req("GET", server + "/app.js")
    assert code == 200 and headers["Content-Type"].startswith("text/javascript")
    code, _, _ = _req("GET", server + "/../pinny/viewer/service.py")
    assert code == 404
    code, _, _ = _req("GET", server + "/%2e%2e/pinny/viewer/service.py")
    assert code == 404
