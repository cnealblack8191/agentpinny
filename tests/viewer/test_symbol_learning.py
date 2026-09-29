"""Learning each symbol type in the site (docs/set-scanning.md "Learning each
symbol type"): scan a set with its legend, review two sheets (approve the
receptacles, reject the crossed-out ones), train the tag's package as a
job, scan again: the unreviewed sheet no longer counts the crossed-out ones.
Also the HTTP routes, their roles, and switching a package off."""

from __future__ import annotations

import json
import math
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pinny.model.symbols import SymbolIndex  # noqa: E402
from pinny.viewer import ViewerService  # noqa: E402
from pinny.viewer.training import TrainingService  # noqa: E402
from tests.legend.pdfgen import Canvas, build, draw_legend  # noqa: E402
from tests.scan.test_learned import CROSSED, REAL, sheet  # noqa: E402
from tests.scan.test_scan import LAYOUT, LEGEND_ROWS, PX  # noqa: E402
from tests.viewer.test_site import ADMIN, REVIEWER, call, err, site, upload  # noqa: E402,F401

SHIFTS = (0, 20, 40)


def rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def crossed_pdf(tmp_path_factory) -> bytes:
    leg = Canvas()
    draw_legend(leg, LEGEND_ROWS, LAYOUT)
    path = tmp_path_factory.mktemp("symlearn") / "set.pdf"
    build([(leg, 0, None)] + [(sheet(REAL, CROSSED, s), 0, None) for s in SHIFTS], str(path))
    return path.read_bytes()


def crossed(p, page_index) -> bool:
    shift = SHIFTS[page_index - 1]
    return any(math.hypot(p["x"] - (x + shift) * PX, p["y"] - y * PX) < 25 for x, y in CROSSED)


def scan(svc, v):
    run = svc.legend.start_scan(v, rid())
    run = svc.legend.wait(v, run["run_id"], timeout=180)
    assert run["status"] == "done", run
    return run


def review(svc, run, pages):
    for s in run["sheets"]:
        if s["page_index"] not in pages:
            continue
        for p in svc.scan_state(s["scan_id"])["pins"]:
            if p["class_label"] != "D":
                continue
            action = "reject" if crossed(p, s["page_index"]) else "approve"
            svc.act(s["scan_id"], {"action": action, "pin_id": p["pin_id"], "request_id": rid()})
    svc.wait_crops()


def wait_job(training, job_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = training.job(job_id)
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def d_pins(svc, run, page_index):
    s = next(s for s in run["sheets"] if s["page_index"] == page_index)
    return [p for p in svc.scan_state(s["scan_id"])["pins"] if p["class_label"] == "D"]


def test_review_train_and_rescan(tmp_path, monkeypatch, crossed_pdf):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path / "data")
    training = TrainingService(svc)
    try:
        v = svc.upload(crossed_pdf, "set.pdf")["document_version"]
        svc.legend.read(v)
        svc.legend.confirm(v, {})
        run1 = scan(svc, v)
        assert run1["learned_tags"] == [] and not run1.get("learned")
        assert sum(crossed(p, 3) for p in d_pins(svc, run1, 3)) == len(CROSSED)  # counted without learning
        # The scan kept each legend symbol's drawing for learning.
        rdir = svc.data_dir / "documents" / v[7:] / "set_scans" / run1["run_id"]
        listed = json.loads((rdir / "symbols.json").read_text())["symbols"]
        assert set(listed.values()) == {"D", "G", "DD", "Q"}
        assert all((rdir / "symbols" / f"{k}.png").is_file() for k in listed)

        review(svc, run1, {1, 2})
        row = next(r for r in training.symbols()["symbols"] if r["tag"] == "D")
        assert (row["approved"], row["rejected"], row["sheets"]) == (2 * len(REAL), 2 * len(CROSSED), 2)
        assert row["model"] is None

        job = training.train_symbol({"tag": "D"}, "admin@x")
        j = wait_job(training, job["job_id"])
        assert j["status"] == "done", j
        res = j["result"]
        assert res["active"] and res["check"]["rejected_caught"] == 2 * len(CROSSED), res
        assert res["check"]["approved_lost"] == 0 and res["verifier"] and res["negatives"] >= 1
        row = next(r for r in training.symbols()["symbols"] if r["tag"] == "D")
        assert row["model"]["active"] and row["model"]["trained"] and row["model"]["trained_by"] == "admin@x"

        run2 = scan(svc, v)
        assert run2["learned_tags"] == ["D"] and run2["learned"]["D"]["used"]
        new = d_pins(svc, run2, 3)
        assert sum(crossed(p, 3) for p in new) == 0 and len(new) == len(REAL)
        sheet3 = next(s for s in run2["sheets"] if s["page_index"] == 3)
        assert sheet3["learned"] == {"rejected": {"D": len(CROSSED)}}
        # The package the scan used was a copy, checked by its sha256.
        rdir2 = svc.data_dir / "documents" / v[7:] / "set_scans" / run2["run_id"]
        assert json.loads((rdir2 / "packages.json").read_text())["packages"]["D"]["sha256"] == \
            SymbolIndex(svc.data_dir).read()["D"]["sha256"]

        # Switched off: the next scan counts as before.
        assert training.set_symbol_active({"tag": "D", "active": False}) == {"tag": "D", "active": False}
        run3 = scan(svc, v)
        assert run3["learned_tags"] == [] and sum(crossed(p, 3) for p in d_pins(svc, run3, 3)) == len(CROSSED)
    finally:
        training.close()
        svc.close()


def test_training_a_tag_with_no_reviews_says_what_to_do(tmp_path, monkeypatch, crossed_pdf):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path / "data")
    training = TrainingService(svc)
    try:
        j = wait_job(training, training.train_symbol({"tag": "Q"}, "a@x")["job_id"])
        assert j["status"] == "failed" and j["error"]["code"] == "no_legend_drawing", j
        v = svc.upload(crossed_pdf, "set.pdf")["document_version"]
        svc.legend.read(v)
        svc.legend.confirm(v, {})
        scan(svc, v)
        j = wait_job(training, training.train_symbol({"tag": "Q"}, "a@x")["job_id"])
        assert j["status"] == "done" and not j["result"]["active"] and "Approve" in j["result"]["reason"]
        with pytest.raises(Exception) as e:
            training.set_symbol_active({"tag": "Q", "active": True})
        assert e.value.code in ("unknown_symbol_model", "symbol_model_not_checked")
        with pytest.raises(Exception) as e:
            training.train_symbol({"tag": "a/b"}, "a@x")
        assert e.value.code == "invalid_tag"
    finally:
        training.close()
        svc.close()


def test_http_routes_and_roles(site, crossed_pdf):
    base, s, svc = site
    code, _, raw = call(base, "GET", "/api/training/symbols", who=REVIEWER)
    assert code == 200 and json.loads(raw)["symbols"] == []
    code, _, raw = call(base, "POST", "/api/training/symbols/train", who=REVIEWER, body={"tag": "D"})
    assert code == 403
    code, _, raw = call(base, "POST", "/api/training/symbols/train", who=ADMIN, body={"tag": ""})
    assert code == 400 and err(raw)["code"] == "invalid_tag"
    code, _, raw = call(base, "POST", "/api/training/symbols/train", who=ADMIN, body={"tag": "D"})
    assert code == 202 and json.loads(raw)["kind"] == "train_symbol"
    code, _, raw = call(base, "POST", "/api/training/symbols/active", who=ADMIN, body={"tag": "D", "active": True})
    assert code in (404, 409)
    code, _, raw = call(base, "POST", "/api/training/symbols/active", who=ADMIN, body={"tag": "D"})
    assert code == 400
    actions = [e["action"] for e in json.loads(call(base, "GET", "/api/audit")[2])["events"]]
    assert "symbol_training_started" in actions


def test_train_and_scan_in_sandboxed_children(tmp_path, monkeypatch, crossed_pdf):
    """Jobs on, as on the server: the train child trains the package, the
    scan child reads its copy from the run; the web process only copies files."""
    from pinny.jobs import JobQueue, legend_tasks
    from pinny.jobs.worker import WorkerPool
    from pinny.render import pdf as pdf_module

    def forbidden(*a, **k):
        raise AssertionError("the web process opened a PDF")
    for name in ("inspect", "render_rgb"):
        monkeypatch.setattr(pdf_module, name, forbidden)
    monkeypatch.setattr(legend_tasks, "scan_set", forbidden)
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    jobs = JobQueue(tmp_path)
    pool = WorkerPool(jobs, tmp_path, sizes={"interactive": 1, "scan": 1, "train": 1}).start()
    svc = ViewerService(tmp_path, jobs=jobs)
    training = TrainingService(svc)
    try:
        v = svc.upload(crossed_pdf, "set.pdf")["document_version"]
        svc.legend.read(v)
        svc.legend.confirm(v, {})
        run1 = scan(svc, v)
        review(svc, run1, {1, 2})
        j = wait_job(training, training.train_symbol({"tag": "D"}, "admin@x")["job_id"])
        assert j["status"] == "done" and j["result"]["active"], j
        run2 = scan(svc, v)
        assert run2["learned"]["D"]["used"]
        assert sum(crossed(p, 3) for p in d_pins(svc, run2, 3)) == 0 and len(d_pins(svc, run2, 3)) == len(REAL)
    finally:
        training.close()
        svc.close()
        pool.stop()
