"""Scan modes (docs/phase2-contracts.md P7) through the service and the HTTP API.

The models are the fakes in ``test_modes_fakes.py``: they satisfy the P6
interface and need no torch or trained weights.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pdfgen import make_pdf, receptacle_centres_px  # noqa: E402

from pinny.viewer import ViewerError, ViewerService  # noqa: E402
from pinny.viewer.server import make_server  # noqa: E402
from tests.viewer.test_modes_fakes import (FAKE_CLASSES, FakePointDetector,  # noqa: E402
                                           FakeVerifier, make_model, points_near, promote)
from tests.viewer.test_viewer_api import _req, _template_for  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CENTRES = receptacle_centres_px()


def rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    s = ViewerService(tmp_path, model_classes=FAKE_CLASSES)
    yield s
    s.close()


@pytest.fixture
def doc(svc):
    return svc.upload(make_pdf(), "a.pdf")


def _scan(svc, doc, mode, **kw):
    v = doc["document_version"]
    if mode != "model" and "template_box" not in kw:
        kw["template_box"] = _template_for(doc, svc.frame(v, 0))
    return svc.scan(document_version=v, page_index=0, request_id=kw.pop("request_id", rid()),
                    mode=mode, **kw)


def _verifier(svc, tmp_path, reject=((CENTRES[1]),), threshold=0.5):
    mid = make_model(svc.data_dir, "verifier", threshold=threshold,
                     fake={"reject": [list(p) for p in reject], "low": 0.2, "high": 0.9})
    promote(svc.data_dir, mid, tmp_path)
    return mid


# Two true receptacles, one false positive in blank space, one below threshold.
_POINTS = [[CENTRES[0][0] + 0.4, CENTRES[0][1] - 0.3, 0.97], [CENTRES[3][0], CENTRES[3][1], 0.81],
           [900.0, 1500.0, 0.6], [5.0, 5.0, 0.2]]


def _detector(svc, tmp_path, threshold=0.5, points=_POINTS):
    mid = make_model(svc.data_dir, "detector", threshold=threshold, fake={"points": points})
    promote(svc.data_dir, mid, tmp_path)
    return mid


# -------------------------------------------------------------- template
def test_template_mode_is_phase1_and_the_default(svc, doc):
    a = _scan(svc, doc, "template")
    b = svc.scan(document_version=doc["document_version"], page_index=0, request_id=rid(),
                 template_box=_template_for(doc, svc.frame(doc["document_version"], 0)))
    for st in (a, b):
        assert st["mode"] == "template" and st["detector"]["name"] == "opencv-template"
        assert st["detector"]["version"].startswith("git:")
        assert st["suppressed"] == [] and len(st["pins"]) == len(CENTRES)
        assert all("verifier_score" not in p and "template_score" not in p for p in st["pins"])
        assert "threshold" in st["detector"]["settings"]
    assert [p["score"] for p in a["pins"]] == [p["score"] for p in b["pins"]]
    rep = svc.report(a["scan_id"])["original_detections"]
    assert rep["mode"] == "template" and "suppressed" not in rep


def test_modes_listing(svc, tmp_path):
    m = {e["mode"]: e for e in svc.models()["modes"]}
    assert m["template"]["available"] and m["template"]["needs_template"]
    assert not m["template+verifier"]["available"] and "No verifier" in m["template+verifier"]["reason"]
    assert not m["model"]["available"] and not m["model"]["needs_template"]
    vid, did = _verifier(svc, tmp_path, threshold=0.6), _detector(svc, tmp_path)
    listing = svc.models()
    assert listing["active"] == {"verifier": vid, "detector": did}
    m = {e["mode"]: e for e in listing["modes"]}
    assert m["template+verifier"]["available"] and m["template+verifier"]["model_id"] == vid
    assert m["template+verifier"]["threshold"] == 0.6
    assert m["model"]["available"] and m["model"]["model_id"] == did


@pytest.mark.parametrize("mode", ["template+verifier", "model"])
def test_mode_without_active_model_is_refused(svc, doc, mode):
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, mode)
    assert e.value.code == "model_not_active" and e.value.status == 409
    assert ("verifier" if mode != "model" else "detector") in e.value.message
    assert svc.page_scans(doc["document_version"], 0) == []


def test_invalid_mode(svc, doc):
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "yolo")
    assert e.value.code == "invalid_mode" and "template+verifier" in e.value.message


# ----------------------------------------------------- template+verifier
def test_template_plus_verifier_rescored_and_suppressed(svc, doc, tmp_path):
    vid = _verifier(svc, tmp_path)
    base = _scan(svc, doc, "template")
    st = _scan(svc, doc, "template+verifier")
    assert st["mode"] == "template+verifier"
    assert st["detector"]["name"] == "opencv-template+verifier"
    assert st["detector"]["version"] == vid
    vs = st["detector"]["settings"]["verifier"]
    assert vs == {"model_id": vid, "threshold": 0.5, "threshold_source": "model"}
    assert st["detector"]["settings"]["threshold"] == 0.8  # template threshold, as Phase 1

    kept = [(p["x"], p["y"]) for p in st["pins"]]
    assert len(kept) == len(CENTRES) - 1 and not points_near(kept, *CENTRES[1])
    template_scores = {(round(p["x"]), round(p["y"])): p["score"] for p in base["pins"]}
    for p in st["pins"]:
        assert p["verifier_score"] == p["score"] == 0.9
        assert p["template_score"] == template_scores[(round(p["x"]), round(p["y"]))]
        assert p["state"] == "unreviewed" and p["origin"] == "machine"
    [sup] = st["suppressed"]
    assert points_near([(sup["x"], sup["y"])], *CENTRES[1])
    assert sup["verifier_score"] == 0.2 and sup["score"] == 0.2
    assert sup["template_score"] > 0.8 and sup["suppressed_by"] == "verifier"
    assert sup["id"].startswith("sup-") and sup["box"]["width"] > 0

    # Reviews work exactly as in Phase 1, and the pin keeps its scores.
    p = st["pins"][0]
    r = svc.act(st["scan_id"], {"action": "approve", "pin_id": p["pin_id"], "request_id": rid()})
    assert r["pin"]["state"] == "approved" and r["pin"]["verifier_score"] == 0.9
    assert r["pin"]["template_score"] == p["template_score"]

    rep = svc.report(st["scan_id"])
    orig = rep["original_detections"]
    assert orig["mode"] == "template+verifier" and orig["suppressed"] == st["suppressed"]
    assert all(d["confidence"] == d["verifier_score"] for d in orig["detections"])
    assert len(rep["corrected_pins"]["pins"]) == len(CENTRES) - 1
    assert [s["mode"] for s in svc.page_scans(doc["document_version"], 0)] == [
        "template", "template+verifier"]


def test_verifier_threshold_override_and_model_cache(svc, doc, tmp_path):
    _verifier(svc, tmp_path)
    FakeVerifier.loads = 0
    st = _scan(svc, doc, "template+verifier", model_threshold=0.95)
    assert st["pins"] == [] and len(st["suppressed"]) == len(CENTRES)
    assert st["detector"]["settings"]["verifier"]["threshold_source"] == "request"
    _scan(svc, doc, "template+verifier")
    assert FakeVerifier.loads == 1  # loaded once, then reused
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "template+verifier", model_threshold=1.5)
    assert e.value.code == "invalid_threshold"
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "template", model_threshold=0.5)
    assert e.value.code == "invalid_threshold"


def test_newly_promoted_verifier_is_used_on_the_next_scan(svc, doc, tmp_path):
    v1 = _verifier(svc, tmp_path)
    assert _scan(svc, doc, "template+verifier")["detector"]["version"] == v1
    v2 = make_model(svc.data_dir, "verifier", stamp="20260925T000000Z",
                    fake={"reject": [], "high": 0.7})
    promote(svc.data_dir, v2, tmp_path)
    st = _scan(svc, doc, "template+verifier")
    assert st["detector"]["version"] == v2 and st["suppressed"] == []
    assert {p["verifier_score"] for p in st["pins"]} == {0.7}


def test_verifier_needs_a_template_box(svc, doc, tmp_path):
    _verifier(svc, tmp_path)
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "template+verifier", template_box=None)
    assert e.value.code == "invalid_template_box"


def test_bad_model_output_is_refused_not_recorded(svc, doc, tmp_path, monkeypatch):
    _verifier(svc, tmp_path)
    monkeypatch.setattr(FakeVerifier, "score", lambda self, page, pts: [0.5] * (len(pts) - 1))
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "template+verifier")
    assert e.value.code == "model_output_invalid"
    monkeypatch.setattr(FakeVerifier, "score", lambda self, page, pts: [float("nan")] * len(pts))
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "template+verifier")
    assert e.value.code == "model_output_invalid"
    assert svc.page_scans(doc["document_version"], 0) == []


# ------------------------------------------------------------------ model
def test_model_mode_needs_no_template(svc, doc, tmp_path):
    did = _detector(svc, tmp_path)
    st = _scan(svc, doc, "model")
    assert st["mode"] == "model" and st["template"] is None
    assert st["detector"]["name"] == "pinny-point-detector" and st["detector"]["version"] == did
    assert st["detector"]["settings"]["threshold"] == 0.5
    assert st["suppressed"] == []
    assert [p["score"] for p in st["pins"]] == [0.97, 0.81, 0.6]  # 0.2 is below threshold
    for p, (x, y, s) in zip(st["pins"], _POINTS):
        assert (p["x"], p["y"]) == (x, y) and p["rotation"] == 0
        b = p["box"]
        assert (b["width"], b["height"]) == (40, 40)
        assert abs(b["x"] + 20 - x) <= 0.5 and abs(b["y"] + 20 - y) <= 0.5
        assert isinstance(b["x"], int) and isinstance(b["y"], int)
        assert "verifier_score" not in p and "template_score" not in p
    # Same review flow as Phase 1.
    sid = st["scan_id"]
    for p in st["pins"][:2]:
        svc.act(sid, {"action": "approve", "pin_id": p["pin_id"], "request_id": rid()})
    svc.act(sid, {"action": "delete_pin", "pin_id": st["pins"][2]["pin_id"], "request_id": rid()})
    svc.act(sid, {"action": "add_manual", "x": CENTRES[4][0], "y": CENTRES[4][1],
                  "request_id": rid()})
    rep = svc.report(sid)
    assert rep["original_detections"]["mode"] == "model"
    assert "template" not in rep["original_detections"]
    assert len(rep["corrected_pins"]["final_pins"]) == 3
    assert svc.page_scans(doc["document_version"], 0)[0]["mode"] == "model"


def test_model_mode_threshold_and_refusals(svc, doc, tmp_path):
    _detector(svc, tmp_path)
    st = _scan(svc, doc, "model", model_threshold=0.1)
    assert len(st["pins"]) == 4 and st["detector"]["settings"]["threshold_source"] == "request"
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "model", template_box={"x": 0, "y": 0, "width": 20, "height": 20})
    assert e.value.code == "template_not_used"
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "model", threshold=0.8)
    assert e.value.code == "invalid_threshold"


def test_model_mode_refuses_points_off_the_page(svc, doc, tmp_path):
    _detector(svc, tmp_path, points=[[1700.5, 10.0, 0.9]])
    with pytest.raises(ViewerError) as e:
        _scan(svc, doc, "model")
    assert e.value.code == "model_output_invalid"


def test_model_scan_retry_returns_the_same_scan(svc, doc, tmp_path):
    _detector(svc, tmp_path)
    r = rid()
    FakePointDetector.loads = 0
    a = _scan(svc, doc, "model", request_id=r)
    b = _scan(svc, doc, "model", request_id=r)
    assert a["scan_id"] == b["scan_id"] and FakePointDetector.loads == 1
    assert len(svc.page_scans(doc["document_version"], 0)) == 1


def test_modes_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path, model_classes=FAKE_CLASSES)
    doc = svc.upload(make_pdf(), "a.pdf")
    _verifier(svc, tmp_path)
    st = _scan(svc, doc, "template+verifier")
    svc.close()
    svc2 = ViewerService(tmp_path, model_classes=FAKE_CLASSES)
    try:
        assert svc2.models()["active"]["verifier"] == st["detector"]["version"]
        again = svc2.scan_state(st["scan_id"])
        assert again["pins"] == st["pins"] and again["suppressed"] == st["suppressed"]
        assert again["mode"] == "template+verifier"
    finally:
        svc2.close()


# ----------------------------------------------------------- no torch
def test_without_torch_template_works_and_model_modes_say_why(tmp_path, monkeypatch):
    pytest.importorskip("pinny.viewer")
    try:
        import torch  # noqa: F401
        pytest.skip("torch is installed; this checks the base install")
    except ImportError:
        pass
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path)  # the real (lazily imported) model classes
    try:
        doc = svc.upload(make_pdf(), "a.pdf")
        vid = make_model(tmp_path, "verifier")
        promote(tmp_path, vid, tmp_path)
        m = {e["mode"]: e for e in svc.models()["modes"]}
        assert m["template"]["available"]
        assert not m["template+verifier"]["available"] and "torch" in m["template+verifier"]["reason"]
        assert len(_scan(svc, doc, "template")["pins"]) == len(CENTRES)
        with pytest.raises(ViewerError) as e:
            _scan(svc, doc, "template+verifier")
        assert e.value.code == "models_unavailable" and e.value.status == 503
        assert "train" in e.value.message
        assert "torch" not in sys.modules
    finally:
        svc.close()


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def server(svc):
    httpd = make_server(svc, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_modes(server, svc, doc, tmp_path):
    v = doc["document_version"]
    tbox = _template_for(doc, svc.frame(v, 0))
    code, _, raw = _req("GET", server + "/api/models")
    assert code == 200
    assert {e["mode"]: e["available"] for e in json.loads(raw)["modes"]} == {
        "template": True, "template+verifier": False, "model": False}

    def post(body):
        code, _, raw = _req("POST", server + "/api/scans",
                            dict({"document_version": v, "page_index": 0, "request_id": rid()},
                                 **body))
        return code, json.loads(raw)

    code, body = post({"mode": "model"})
    assert code == 409 and body["error"]["code"] == "model_not_active"
    code, body = post({"mode": "template+verifier", "template_box": tbox})
    assert code == 409 and body["error"]["code"] == "model_not_active"
    code, body = post({"mode": 3, "template_box": tbox})
    assert code == 400 and body["error"]["code"] == "invalid_mode"
    code, body = post({"mode": "nope", "template_box": tbox})
    assert code == 400 and body["error"]["code"] == "invalid_mode"

    code, st = post({"template_box": tbox})  # no mode: Phase 1 request shape
    assert code == 201 and st["mode"] == "template" and len(st["pins"]) == len(CENTRES)
    code, st = post({"mode": "template", "template_box": tbox, "threshold": 0.99})
    assert code == 201 and all(p["score"] >= 0.99 for p in st["pins"])

    vid, did = _verifier(svc, tmp_path), _detector(svc, tmp_path)
    code, raw_models = _req("GET", server + "/api/models")[0::2]
    assert json.loads(raw_models)["active"] == {"verifier": vid, "detector": did}

    code, st = post({"mode": "template+verifier", "template_box": tbox})
    assert code == 201 and st["detector"]["version"] == vid
    assert len(st["pins"]) == len(CENTRES) - 1 and len(st["suppressed"]) == 1
    assert all(p["verifier_score"] == 0.9 and p["template_score"] > 0.8 for p in st["pins"])
    code, _, raw = _req("POST", f"{server}/api/scans/{st['scan_id']}/actions",
                        {"action": "approve", "pin_id": st["pins"][0]["pin_id"],
                         "request_id": rid()})
    assert code == 200 and json.loads(raw)["pin"]["verifier_score"] == 0.9

    code, st = post({"mode": "model"})
    assert code == 201 and st["detector"]["version"] == did and st["template"] is None
    assert all(p["box"]["width"] == 40 for p in st["pins"])
    code, body = post({"mode": "model", "template_box": tbox})
    assert code == 400 and body["error"]["code"] == "template_not_used"
    code, _, raw = _req("GET", f"{server}/api/scans/{st['scan_id']}/report")
    assert code == 200 and json.loads(raw)["original_detections"]["mode"] == "model"

    code, _, raw = _req("GET", f"{server}/api/documents/{urllib_quote(v)}/pages/0/scans")
    assert [s["mode"] for s in json.loads(raw)["scans"]] == [
        "template", "template", "template+verifier", "model"]


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def test_model_modes_report_is_accepted_by_the_evaluator(svc, doc, tmp_path):
    _verifier(svc, tmp_path)
    _detector(svc, tmp_path)
    for mode in ("template+verifier", "model"):
        orig = svc.report(_scan(svc, doc, mode)["scan_id"])["original_detections"]
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps(orig))
        d, fr = orig["document"], orig["coordinate_frame"]
        run = subprocess.run(
            [sys.executable, "-m", "pinny_eval", "pending", "--detections", str(path),
             "--document-id", d["document_id"], "--document-version", d["document_version"],
             "--page", str(d["page_index"]), "--width", str(fr["width"]),
             "--height", str(fr["height"])],
            cwd=REPO / "evaluation", capture_output=True, text=True)
        assert run.returncode == 0, (mode, run.stderr)


# ------------------------------------------------- real classes (optional)
def test_real_model_classes_satisfy_p6_and_scan(tmp_path, monkeypatch):
    """Runs only when torch and chats B/C's classes are importable. The full
    scan runs against trained artifacts named by $PINNY_TEST_VERIFIER_DIR /
    $PINNY_TEST_DETECTOR_DIR (e.g. trained on synthetic data)."""
    import inspect
    import os
    import shutil

    pytest.importorskip("torch")
    vmod = pytest.importorskip("pinny.models.verifier")
    dmod = pytest.importorskip("pinny.models.point_detector")
    Verifier, PointDetector = vmod.Verifier, dmod.PointDetector
    for cls, method in ((Verifier, "score"), (PointDetector, "detect_points")):
        assert inspect.ismethod(cls.load), f"{cls.__name__}.load must be a classmethod"
        assert callable(getattr(cls, method))
    params = inspect.signature(PointDetector.detect_points).parameters
    assert params["threshold"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["max_points"].kind is inspect.Parameter.KEYWORD_ONLY

    dirs = {k: os.environ.get(f"PINNY_TEST_{k.upper()}_DIR") for k in ("verifier", "detector")}
    if not all(dirs.values()):
        pytest.skip("set PINNY_TEST_VERIFIER_DIR and PINNY_TEST_DETECTOR_DIR to run a real scan")
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path)  # no fakes: the real classes, loaded lazily
    try:
        for kind, src in dirs.items():
            meta = json.loads((Path(src) / "model.json").read_text())
            shutil.copytree(src, tmp_path / "models" / meta["model_id"])
            promote(tmp_path, meta["model_id"], tmp_path)
        doc = svc.upload(make_pdf(), "a.pdf")
        for mode in ("template+verifier", "model"):
            st = _scan(svc, doc, mode)
            assert st["mode"] == mode
            assert all(0 <= p["score"] <= 1 for p in st["pins"])
    finally:
        svc.close()
