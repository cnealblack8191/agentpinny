"""Batch scans ("scan every page") through the viewer service and HTTP API
(contracts section 5a). Real render service, synthetic vector PDFs."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from pdfgen import RECEPTACLES_PT, receptacle_centres_px  # noqa: E402

from pinny.viewer import ViewerError, ViewerService  # noqa: E402
from pinny.viewer.server import make_server  # noqa: E402
from tests.factory import PageSpec, build_pdf  # noqa: E402
from tests.viewer.test_modes_fakes import FAKE_CLASSES, make_model, promote  # noqa: E402
from tests.viewer.test_viewer_api import _req, _template_for  # noqa: E402

CENTRES = receptacle_centres_px()


def rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    s = ViewerService(tmp_path, model_classes=FAKE_CLASSES)
    yield s
    s.close()


def drawing_set(*receptacle_counts, tiny_last=False):
    """One letter page per count, carrying that many of RECEPTACLES_PT.
    ``tiny_last`` adds a page smaller than the template, which cannot scan."""
    specs = [PageSpec(receptacles=list(RECEPTACLES_PT[:n])) for n in receptacle_counts]
    if tiny_last:
        specs.append(PageSpec(width_pt=10, height_pt=10))
    return build_pdf(specs)


@pytest.fixture
def doc(svc):
    return svc.upload(drawing_set(5, 2, 0, 3), "set.pdf")


def _start(svc, doc, **kw):
    v = doc["document_version"]
    if kw.get("mode") != "model" and "template_box" not in kw:
        kw["template_box"] = _template_for(doc, svc.frame(v, 0))
    return svc.start_batch(document_version=v, request_id=kw.pop("request_id", rid()), **kw)


def test_batch_scans_every_page_with_one_template(svc, doc):
    b = _start(svc, doc, template_page_index=0)
    assert b["status"] in ("queued", "running", "complete")
    b = svc.wait_batch(b["batch_id"], timeout=120)
    assert b["status"] == "complete"
    assert b["page_counts"] == {"pending": 0, "running": 0, "done": 4, "failed": 0,
                                "skipped": 0, "total": 4}
    assert [p["page_index"] for p in b["pages"]] == [0, 1, 2, 3]
    assert [p["counts"]["unreviewed"] for p in b["pages"]] == [5, 2, 0, 3]
    assert b["pin_counts"]["total"] == 10
    assert b["template"]["page_index"] == 0

    # Each page is an ordinary scan that knows its batch, and the template
    # was cut once from page 0.
    s1 = svc.scan_state(b["pages"][1]["scan_id"])
    assert s1["document"]["page_index"] == 1
    assert s1["batch"] == {"batch_id": b["batch_id"], "page_index": 1}
    assert s1["template"] == b["template"]
    got = sorted((round(p["x"]), round(p["y"])) for p in s1["pins"])
    assert got == sorted((round(x), round(y)) for x, y in CENTRES[:2])
    assert svc.document_batches(doc["document_version"])[0]["batch_id"] == b["batch_id"]


def test_review_queue_spans_pages_and_uses_normal_actions(svc, doc):
    b = svc.wait_batch(_start(svc, doc)["batch_id"], timeout=120)
    q = svc.batch_queue(b["batch_id"])
    assert len(q["items"]) == 10
    assert {i["page_index"] for i in q["items"]} == {0, 1, 3}
    scores = [i["score"] for i in q["items"]]
    assert scores == sorted(scores)  # template scores sit above the threshold: margin = lowest first

    first, second = q["items"][:2]
    r = svc.act(first["scan_id"], {"action": "approve", "pin_id": first["pin_id"],
                                   "request_id": rid(), "expected_version": first["version"]})
    assert r["pin"]["state"] == "approved"
    svc.act(second["scan_id"], {"action": "reject", "pin_id": second["pin_id"], "request_id": rid()})
    svc.act(b["pages"][2]["scan_id"], {"action": "add_manual", "x": 100, "y": 100,
                                       "request_id": rid()})
    q2 = svc.batch_queue(b["batch_id"], limit=3)
    assert len(q2["items"]) == 3 and first["pin_id"] not in [
        i["pin_id"] for i in q2["items"] if i["scan_id"] == first["scan_id"]]
    b = svc.batch_state(b["batch_id"])
    assert (b["pin_counts"]["approved"], b["pin_counts"]["rejected"],
            b["pin_counts"]["added"], b["pin_counts"]["unreviewed"]) == (1, 1, 1, 8)
    assert svc.batch_queue(b["batch_id"], "lowest_score")["strategy"] == "lowest_score"
    with pytest.raises(ViewerError) as e:
        svc.batch_queue(b["batch_id"], "random")
    assert e.value.code == "invalid_strategy"


def test_failed_page_does_not_stop_the_batch(svc):
    doc = svc.upload(drawing_set(5, 1, tiny_last=True), "set.pdf")
    b = svc.wait_batch(_start(svc, doc)["batch_id"], timeout=120)
    assert b["status"] == "complete"
    assert [p["status"] for p in b["pages"]] == ["done", "done", "failed"]
    err = b["pages"][2]["error"]
    assert err["code"] and err["message"]
    # Retrying still fails (the page really is too small) and counts the attempt.
    b = svc.wait_batch(svc.resume_batch(b["batch_id"], retry_failed=True)["batch_id"], timeout=120)
    assert b["pages"][2]["status"] == "failed" and b["pages"][2]["attempts"] == 2


def test_retry_failed_while_running_leaves_the_live_page_alone(svc):
    doc = svc.upload(drawing_set(5, 1, tiny_last=True), "set.pdf")
    gate, entered = threading.Event(), threading.Event()
    real = svc.detector.detect

    def slow_detect(*a, **k):
        entered.set()
        gate.wait(30)
        return real(*a, **k)

    svc.detector.detect = slow_detect
    b = _start(svc, doc)
    assert entered.wait(30)  # page 0 is running now
    assert svc.batch_state(b["batch_id"])["pages"][0]["status"] == "running"
    svc.resume_batch(b["batch_id"], retry_failed=True)  # mid-run: must not reset page 0
    assert svc.batch_state(b["batch_id"])["pages"][0]["status"] == "running"
    gate.set()
    b = svc.wait_batch(b["batch_id"], timeout=120)
    assert [p["status"] for p in b["pages"]] == ["done", "done", "failed"]
    assert [p["attempts"] for p in b["pages"]] == [1, 1, 1]
    # With the batch idle, retrying the failed page runs it again.
    b = svc.wait_batch(svc.resume_batch(b["batch_id"], retry_failed=True)["batch_id"], timeout=120)
    assert [p["attempts"] for p in b["pages"]] == [1, 1, 2]


def test_subset_of_pages_and_template_from_another_page(svc, doc):
    b = svc.wait_batch(_start(svc, doc, page_indexes=[3, 1], template_page_index=0)["batch_id"],
                       timeout=120)
    assert [p["page_index"] for p in b["pages"]] == [3, 1]
    assert [p["counts"]["total"] for p in b["pages"]] == [3, 2]


def test_retry_returns_the_same_batch_and_conflicts_are_refused(svc, doc):
    req = rid()
    b1 = svc.wait_batch(_start(svc, doc, request_id=req)["batch_id"], timeout=120)
    b2 = _start(svc, doc, request_id=req)
    assert b2["batch_id"] == b1["batch_id"] and b2["pages"] == b1["pages"]
    with pytest.raises(ViewerError) as e:
        _start(svc, doc, request_id=req, page_indexes=[0])
    assert e.value.code == "request_conflict"


def test_interrupted_batch_resumes_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    s = ViewerService(tmp_path)
    doc = s.upload(drawing_set(5, 2, 1), "set.pdf")
    v = doc["document_version"]
    # Simulate a crash mid-batch: record the batch, claim a page, never finish it.
    req = rid()
    orig = s._batch_exec.submit
    s._batch_exec.submit = lambda *a, **k: orig(lambda: None)  # don't run the job
    b = _start(s, doc, request_id=req)
    s._db(s.store.next_batch_page, b["batch_id"])
    s._batch_exec.submit = orig
    assert s.batch_state(b["batch_id"])["pages"][0]["status"] == "running"
    s.close()

    s = ViewerService(tmp_path)
    try:
        b = s.start_batch(document_version=v, request_id=req,
                          template_box=_template_for(doc, s.frame(v, 0)))
        b = s.wait_batch(b["batch_id"], timeout=120)
        assert [p["status"] for p in b["pages"]] == ["done", "done", "done"]
        assert [p["counts"]["total"] for p in b["pages"]] == [5, 2, 1]
    finally:
        s.close()


def test_cancel_skips_remaining_pages(svc, doc):
    gate = threading.Event()
    real = svc.detector.detect

    def slow_detect(*a, **k):
        gate.wait(30)
        return real(*a, **k)

    svc.detector.detect = slow_detect
    b = _start(svc, doc)
    b = svc.cancel_batch(b["batch_id"])
    gate.set()
    b = svc.wait_batch(b["batch_id"], timeout=120)
    assert b["status"] == "cancelled"
    statuses = [p["status"] for p in b["pages"]]
    assert statuses.count("skipped") >= 3 and set(statuses) <= {"done", "skipped"}
    # A cancelled batch is not resumed.
    assert svc.resume_batch(b["batch_id"])["status"] == "cancelled"


def test_validation(svc, doc):
    v = doc["document_version"]
    box = _template_for(doc, svc.frame(v, 0))
    cases = [
        (dict(page_indexes=[]), "invalid_pages"),
        (dict(page_indexes=[0, 0]), "invalid_pages"),
        (dict(page_indexes=[9]), "page_not_found"),
        (dict(page_indexes=["1"]), "invalid_pages"),
        (dict(template_page_index=7), "invalid_page"),
        (dict(template_box=None), "invalid_template_box"),
        (dict(mode="nope"), "invalid_mode"),
        (dict(threshold=2), "invalid_threshold"),
        (dict(mode="model", template_box=None, template_page_index=0), "template_not_used"),
        (dict(mode="model", template_box=None), "model_not_active"),
    ]
    for kw, code in cases:
        args = dict(document_version=v, request_id=rid(), template_box=box)
        args.update(kw)
        with pytest.raises(ViewerError) as e:
            svc.start_batch(**args)
        assert e.value.code == code, kw
    with pytest.raises(ViewerError) as e:
        svc.start_batch(document_version=v, request_id="not-a-uuid", template_box=box)
    assert e.value.code == "invalid_request_id"
    with pytest.raises(ViewerError) as e:
        svc.batch_state("nope")
    assert e.value.code == "unknown_batch" and e.value.status == 404


def test_model_mode_batch(svc, doc, tmp_path):
    mid = make_model(svc.data_dir, "detector", threshold=0.5,
                     fake={"points": [[x, y, 0.9] for x, y in CENTRES[:1]]})
    promote(svc.data_dir, mid, tmp_path)
    b = svc.wait_batch(_start(svc, doc, mode="model")["batch_id"], timeout=120)
    assert b["status"] == "complete" and b["template"] is None
    assert {p["status"] for p in b["pages"]} == {"done"}
    assert svc.scan_state(b["pages"][0]["scan_id"])["mode"] == "model"


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def server(svc):
    httpd = make_server(svc, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_batch_flow(svc, doc, server):
    v = doc["document_version"]
    body = {"document_version": v, "request_id": rid(), "template_page_index": 0,
            "template_box": _template_for(doc, svc.frame(v, 0))}
    code, _, raw = _req("POST", server + "/api/batches", body)
    assert code == 202
    batch_id = json.loads(raw)["batch_id"]
    svc.wait_batch(batch_id, timeout=120)

    code, _, raw = _req("GET", f"{server}/api/batches/{batch_id}")
    assert code == 200 and json.loads(raw)["status"] == "complete"
    code, _, raw = _req("GET", f"{server}/api/batches/{batch_id}/queue?limit=4&strategy=lowest_score")
    items = json.loads(raw)["items"]
    assert code == 200 and len(items) == 4
    code, _, raw = _req("POST", f"{server}/api/scans/{items[0]['scan_id']}/actions",
                        {"action": "approve", "pin_id": items[0]["pin_id"], "request_id": rid()})
    assert code == 200
    code, _, raw = _req("GET", f"{server}/api/documents/{urllib.request.quote(v, safe='')}/batches")
    assert code == 200 and json.loads(raw)["batches"][0]["pin_counts"]["approved"] == 1
    code, _, raw = _req("POST", f"{server}/api/batches/{batch_id}/resume", {"retry_failed": True})
    assert code == 200
    code, _, raw = _req("POST", f"{server}/api/batches/{batch_id}/cancel", {})
    assert code == 200 and json.loads(raw)["status"] == "complete"  # nothing was left to skip

    code, _, raw = _req("GET", f"{server}/api/batches/{batch_id}/queue?limit=x")
    assert code == 400 and json.loads(raw)["error"]["code"] == "invalid_limit"
    code, _, raw = _req("POST", f"{server}/api/batches/{batch_id}/resume", {"retry_failed": "yes"})
    assert code == 400
    code, _, raw = _req("GET", f"{server}/api/batches/nope")
    assert code == 404 and json.loads(raw)["error"]["code"] == "unknown_batch"
    code, _, raw = _req("POST", server + "/api/batches", {"request_id": rid()})
    assert code == 400 and json.loads(raw)["error"]["code"] == "invalid_document"
