"""The legend workflow in the viewer (docs/viewer.md "Legend and whole set",
docs/training-site.md sections 3-5): find and check the legend, scan the
whole set with it as a job, review the sheets with each pin's tag, count.

Drawing sets come from ``tests/legend/pdfgen.py``: a legend page, a plan and
a plan on a ``/Rotate 90`` page drawn to display upright.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pikepdf
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pinny.jobs import JobQueue  # noqa: E402
from pinny.jobs import legend_tasks  # noqa: E402
from pinny.jobs.worker import WorkerPool  # noqa: E402
from pinny.render import RenderService  # noqa: E402
from pinny.render import pdf as pdf_module  # noqa: E402
from pinny.viewer import ViewerService  # noqa: E402
from tests.legend.pdfgen import MAIN_ROWS, Canvas, build, draw_legend, floor_plan, raster_copy  # noqa: E402
from tests.scan.test_scan import LAYOUT, LEGEND_ROWS, PX, SHEET1, SHEET2, TAG, expected, plan  # noqa: E402
from tests.viewer.test_site import ADMIN, DRAFTSMAN, REVIEWER, call, err, site, upload  # noqa: E402,F401

EXPECTED_TOTAL = {"D": 6, "G": 3, "DD": 2, "Q": 2}


def rid() -> str:
    return str(uuid.uuid4())


def legend_set(path: Path) -> bytes:
    """Legend (page 1), a plan (page 2) and a rotated plan (page 3)."""
    leg = Canvas()
    draw_legend(leg, LEGEND_ROWS, LAYOUT)
    build([(leg, 0, None), (plan(SHEET1, wire=True), 0, None),
           (plan(SHEET2, canvas=Canvas(1224, 792)), 90, None)], str(path))
    return path.read_bytes()


@pytest.fixture(scope="module")
def set_pdf(tmp_path_factory) -> bytes:
    return legend_set(tmp_path_factory.mktemp("legendset") / "set.pdf")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    s = ViewerService(tmp_path / "data")
    yield s
    s.close()


def scanned_set(svc, pdf: bytes):
    doc = svc.upload(pdf, "set.pdf")
    v = doc["document_version"]
    svc.legend.read(v)
    svc.legend.confirm(v, {})
    run = svc.legend.start_scan(v, rid())
    run = svc.legend.wait(v, run["run_id"], timeout=120)
    assert run["status"] == "done", run
    return v, run


def near(pins, sheet, tol=25):
    """Every pin sits on a synthetic symbol of its tag, and every symbol has one."""
    want = [(TAG[k], x * PX, y * PX) for k, x, y, _ in sheet if k in TAG]
    for p in pins:
        assert any(t == p["class_label"] and math.hypot(x - p["x"], y - p["y"]) < tol for t, x, y in want), p
    for t, x, y in want:
        assert any(p["class_label"] == t and math.hypot(x - p["x"], y - p["y"]) < tol for p in pins), (t, x, y)


# ---------------------------------------------------------------- service
def test_read_check_confirm_scan_and_pins_in_the_viewer_frame(svc, set_pdf):
    doc = svc.upload(set_pdf, "set.pdf")
    v = doc["document_version"]
    assert svc.legend.state(v) == {"document_version": v, "legend": None, "set_scan": None}
    lg = svc.legend.read(v, requested_by="a@b.c")["legend"]
    assert lg["page_index"] == 0 and [e["tag"] for e in lg["entries"]] == ["D", "G", "DD", "Q", "T"]
    assert [e["count"] for e in lg["entries"]] == [True, True, True, True, False]  # thermostat by mech
    assert lg["ready_to_confirm"] and not lg["confirmed"] and lg["version"] == 1
    assert (svc.data_dir / "documents" / v[7:] / "legend.json").is_file()
    # Scanning needs a confirmed legend.
    with pytest.raises(Exception) as e:
        svc.legend.start_scan(v, rid())
    assert e.value.code == "legend_not_confirmed"
    lg = svc.legend.confirm(v, {}, reviewer="a@b.c")["legend"]
    assert lg["confirmed"] and lg["confirmed_by"] == "a@b.c"
    run = svc.legend.start_scan(v, rid(), requested_by="a@b.c")
    assert run["sheets_total"] == 2 and run["status"] in ("queued", "running", "done")
    run = svc.legend.wait(v, run["run_id"], timeout=120)
    assert run["status"] == "done" and [s["page_index"] for s in run["sheets"]] == [1, 2]
    assert all(s["scan_id"] and s["error"] is None for s in run["sheets"])
    # One ordinary scan per sheet, each pin tagged, in the render service's frame.
    for s, sheet in zip(run["sheets"], (SHEET1, SHEET2)):
        st = svc.scan_state(s["scan_id"])
        assert st["mode"] == "legend" and st["detector"]["name"] == "pinny-set-scan"
        assert st["detector"]["version"] == svc.detector_version
        settings = st["detector"]["settings"]
        assert settings["legend_sha256"] == run["legend_sha256"] and "scales" in settings and "warnings" in settings
        frame = svc.frame(v, s["page_index"])
        assert (st["coordinate_frame"]["width"], st["coordinate_frame"]["height"]) == (frame["width"], frame["height"])
        near(st["pins"], sheet)
        assert all(p["state"] == "unreviewed" and p["origin"] == "machine" for p in st["pins"])
        assert svc.page_scans(v, s["page_index"])[-1]["scan_id"] == s["scan_id"]
    # The rotated sheet displays landscape: pins sit on ink in the viewer's own raster.
    assert svc.frame(v, 2)["width"] > svc.frame(v, 2)["height"]
    raster = RenderService(svc.data_dir).render_page(v, 2)
    for p in svc.scan_state(run["sheets"][1]["scan_id"])["pins"]:
        x, y = int(p["x"]), int(p["y"])
        win = raster[max(0, y - 20):y + 20, max(0, x - 20):x + 20].min(axis=2)
        ys, xs = np.nonzero(win < 128)
        assert len(xs) > 20, p  # the symbol's circle is drawn around the pin
        assert xs.min() < 20 - 5 < 20 + 5 < xs.max() and ys.min() < 20 < ys.max(), p
    c = svc.legend.counts(v)
    assert {t: n["found"] for t, n in c["totals"]["by_tag"].items()} == EXPECTED_TOTAL
    assert [t["tag"] for t in c["tags"]] == ["D", "G", "DD", "Q"]


def test_legend_edits_and_the_needs_a_look_rules(svc, tmp_path):
    """A legend with a combined row (two drawings) and a wrapped description
    can't be confirmed until someone looks; every edit is logged with who."""
    leg = Canvas()
    draw_legend(leg, MAIN_ROWS, LAYOUT)
    fp = Canvas()
    floor_plan(fp)
    build([(leg, 0, None), (fp, 0, None)], str(tmp_path / "main.pdf"))
    v = svc.upload((tmp_path / "main.pdf").read_bytes(), "main.pdf")["document_version"]
    lg = svc.legend.read(v)["legend"]
    check = [e for e in lg["entries"] if e["status"] == "check"]
    assert check and not lg["ready_to_confirm"] and "need a look" in lg["problems"][0]
    combined = next(e for e in check if len(e["symbol_boxes"]) == 2)
    assert any(n["flag"] == "combined" and n["blocking"] for n in combined["notes"])
    with pytest.raises(Exception) as e:
        svc.legend.confirm(v, {})
    assert e.value.code == "legend_needs_review"
    lg = svc.legend.edit(v, {"op": "split", "entry_id": combined["id"], "expected_version": lg["version"]},
                         reviewer="r@x.y")["legend"]
    assert len(lg["entries"]) == len(MAIN_ROWS) + 1
    # A stale version is refused, so two people can't overwrite each other.
    with pytest.raises(Exception) as e:
        svc.legend.edit(v, {"op": "rename", "entry_id": lg["entries"][0]["id"], "name": "X", "expected_version": 0})
    assert e.value.code == "stale_legend" and e.value.status == 409
    first = lg["entries"][0]["id"]
    for body in ({"op": "rename", "name": "Duplex outlet"}, {"op": "set_tag", "tag": "d1"},
                 {"op": "set_group", "group": "Power"}, {"op": "set_count", "count": False},
                 {"op": "set_count", "count": True}):
        lg = svc.legend.edit(v, dict(body, entry_id=first), reviewer="r@x.y")["legend"]
    e0 = lg["entries"][0]
    assert (e0["name"], e0["tag"], e0["group"], e0["count"]) == ("Duplex outlet", "D1", "Power", True)
    for bad, code in (({"op": "set_tag", "tag": "no good!"}, "invalid_tag"), ({"op": "nope"}, "unknown_edit"),
                      ({"op": "set_count", "count": "yes"}, "invalid_count"), ({"op": "rename"}, "invalid_name")):
        with pytest.raises(Exception) as e:
            svc.legend.edit(v, dict(bad, entry_id=first))
        assert e.value.code == code, bad
    for e in [x for x in lg["entries"] if x["status"] == "check"]:
        lg = svc.legend.edit(v, {"op": "confirm", "entry_id": e["id"]})["legend"]
    # Merge two, delete one, add one from a box drawn on the legend page.
    ids = [e["id"] for e in lg["entries"]]
    lg = svc.legend.edit(v, {"op": "merge", "entry_id": ids[1], "into": ids[2]})["legend"]
    assert len(lg["entries"]) == len(ids) - 1
    lg = svc.legend.edit(v, {"op": "delete", "entry_id": lg["entries"][-1]["id"]})["legend"]
    box = lg["entries"][0]["symbol_boxes"][0]
    b = {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}
    with pytest.raises(Exception) as e:  # that row was read already
        svc.legend.edit(v, {"op": "add", "box": b})
    assert e.value.code == "row_already_read" and "force" not in e.value.message
    n = len(lg["entries"])
    lg = svc.legend.edit(v, {"op": "add", "box": b, "force": True}, reviewer="r@x.y")["legend"]
    assert len(lg["entries"]) == n + 1 and lg["entries"][-1]["source"] == "user"
    with pytest.raises(Exception) as e:
        svc.legend.edit(v, {"op": "add", "box": {"x": -5, "y": 0, "width": 10, "height": 10}})
    assert e.value.code == "invalid_box"
    lg = svc.legend.confirm(v, {}, reviewer="r@x.y")["legend"]
    assert lg["confirmed"]
    saved = json.loads((svc.data_dir / "documents" / v[7:] / "legend.json").read_text())
    assert saved["format"] == "pinny.legend" and saved["format_version"] == 1
    ops = [e["op"] for e in saved["edits"]]
    assert ops[0] == "read" and ops[-1] == "confirm_legend" and "split" in ops and "add" in ops
    assert all(e.get("by") == "r@x.y" for e in saved["edits"] if e["op"] in ("split", "rename", "add"))
    # Any change after confirming needs a new confirmation.
    lg = svc.legend.edit(v, {"op": "set_group", "entry_id": first, "group": "Receptacles"})["legend"]
    assert not lg["confirmed"]


def test_review_with_tags_mark_reviewed_counts_and_dataset(site, set_pdf):
    base, s, svc = site
    doc = upload(base, who=REVIEWER, pdf=set_pdf, name="set.pdf")
    v = doc["document_version"]
    code, _, raw = call(base, "GET", f"/api/documents/{v}/legend", who=REVIEWER)
    assert code == 200 and json.loads(raw)["legend"] is None
    code, _, raw = call(base, "POST", f"/api/documents/{v}/legend/read", who=REVIEWER, body={})
    assert code == 200, raw
    lg = json.loads(raw)["legend"]
    code, _, raw = call(base, "POST", f"/api/documents/{v}/set-scans", who=REVIEWER, body={"request_id": rid()})
    assert code == 409 and err(raw)["code"] == "legend_not_confirmed"
    code, _, raw = call(base, "POST", f"/api/documents/{v}/legend/confirm", who=REVIEWER,
                        body={"expected_version": lg["version"]})
    assert code == 200 and json.loads(raw)["legend"]["confirmed_by"] == REVIEWER
    req = rid()
    code, _, raw = call(base, "POST", f"/api/documents/{v}/set-scans", who=REVIEWER, body={"request_id": req})
    assert code == 202, raw
    run = json.loads(raw)
    code, _, raw = call(base, "POST", f"/api/documents/{v}/set-scans", who=REVIEWER, body={"request_id": req})
    assert code == 202 and json.loads(raw)["run_id"] == run["run_id"]  # a retry is the same scan
    run = svc.legend.wait(v, run["run_id"], timeout=120)
    code, _, raw = call(base, "GET", f"/api/documents/{v}/set-scans", who=REVIEWER)
    [listed] = json.loads(raw)["set_scans"]
    assert listed["status"] == "done" and listed["sheets_done"] == 2 and listed["progress"] == 1.0
    sheet1 = run["sheets"][0]["scan_id"]
    st = json.loads(call(base, "GET", f"/api/scans/{sheet1}", who=REVIEWER)[2])
    pins = st["pins"]

    def act(body, who=REVIEWER):
        return call(base, "POST", f"/api/scans/{sheet1}/actions", who=who, body=dict(body, request_id=rid()))

    ds = [p for p in pins if p["class_label"] == "D"]
    assert act({"action": "approve", "pin_id": ds[0]["pin_id"]})[0] == 200
    code, _, raw = act({"action": "reject", "pin_id": ds[1]["pin_id"]})
    assert code == 200
    # Approving can correct the symbol: counted as G from now on.
    code, _, raw = act({"action": "approve", "pin_id": ds[2]["pin_id"], "class_label": "G"})
    assert code == 200 and json.loads(raw)["pin"]["class_label"] == "G"
    code, _, raw = act({"action": "add_manual", "x": 1000.5, "y": 700.0, "class_label": "DD"})
    assert code == 200 and json.loads(raw)["pin"]["class_label"] == "DD"
    for bad, code_ in (({"action": "add_manual", "x": 10, "y": 10, "class_label": "ZZ"}, "unknown_tag"),
                       ({"action": "add_manual", "x": 10, "y": 10, "class_label": 5}, "invalid_class_label"),
                       ({"action": "reject", "pin_id": ds[3]["pin_id"], "class_label": "D"}, "invalid_class_label")):
        code, _, raw = act(bad)
        assert code == 400 and err(raw)["code"] == code_, raw
    # Mark the sheet fully reviewed once every pin is answered.
    code, _, raw = call(base, "POST", "/api/training/pages/complete", who=REVIEWER,
                        body={"document_version": v, "page_index": 1})
    assert code == 409
    for p in json.loads(call(base, "GET", f"/api/scans/{sheet1}", who=REVIEWER)[2])["pins"]:
        if p["state"] == "unreviewed":
            assert act({"action": "approve", "pin_id": p["pin_id"]})[0] == 200
    code, _, raw = call(base, "POST", "/api/training/pages/complete", who=REVIEWER,
                        body={"document_version": v, "page_index": 1})
    assert code == 200, raw
    # Counts: per tag, per sheet and for the set.
    code, _, raw = call(base, "GET", f"/api/documents/{v}/counts", who=REVIEWER)
    c = json.loads(raw)
    s1, s2 = c["sheets"]
    assert s1["review"] == "reviewed" and s2["review"] == "to_review"
    found1 = expected(SHEET1)
    assert s1["by_tag"]["D"] == {"found": found1["D"], "approved": found1["D"] - 2, "rejected": 1, "added": 0,
                                 "confirmed": found1["D"] - 2, "unreviewed": 0}
    assert s1["by_tag"]["G"]["found"] == found1["G"] and s1["by_tag"]["G"]["approved"] == found1["G"] + 1
    assert s1["by_tag"]["DD"]["added"] == 1 and s1["by_tag"]["DD"]["confirmed"] == found1["DD"] + 1
    assert s1["all"]["confirmed"] == sum(found1.values())  # 1 rejected, 1 added
    tot = c["totals"]["by_tag"]
    assert tot["D"]["found"] == 6 and tot["D"]["unreviewed"] == expected(SHEET2)["D"]
    assert c["totals"]["all"]["unreviewed"] == sum(expected(SHEET2).values())
    code, h, raw = call(base, "GET", f"/api/documents/{v}/counts.csv", who=REVIEWER)
    assert code == 200 and h["content-type"].startswith("text/csv")
    assert 'filename="set-counts.csv"' in h["content-disposition"]
    rows = list(csv.reader(io.StringIO(raw.decode())))
    assert rows[0][:5] == ["Sheet", "Sheet status", "Tag", "Symbol", "Found by Pinny"]
    d_all = next(r for r in rows if r[0] == "All sheets" and r[2] == "D")
    assert d_all[4:] == [str(tot["D"][k]) for k in ("found", "approved", "rejected", "added", "confirmed", "unreviewed")]
    assert next(r for r in rows if r[0] == "2" and r[2] == "D")[1] == "Reviewed"
    # A dataset builds from the reviewed set scan: verifier samples from every
    # answered pin, and the fully reviewed sheet as a detector page.
    code, _, raw = call(base, "POST", "/api/training/datasets", who=ADMIN, body={})
    assert code == 202, raw
    job_id = json.loads(raw)["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        j = json.loads(call(base, "GET", f"/api/training/jobs/{job_id}")[2])
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    counts = j["result"]["counts"]
    pos = sum(x["pos"] for x in counts["verifier"].values())
    neg = sum(x["neg"] for x in counts["verifier"].values())
    assert (pos, neg) == (sum(found1.values()), 1)
    assert sum(x["pages"] for x in counts["detector"].values()) == 1
    assert sum(x["points"] for x in counts["detector"].values()) == sum(found1.values())
    # A new whole-set scan reopens the reviewed sheet (training data rules).
    call(base, "POST", f"/api/documents/{v}/legend/confirm", who=REVIEWER, body={})
    code, _, raw = call(base, "POST", f"/api/documents/{v}/set-scans", who=REVIEWER, body={"request_id": rid()})
    run2 = svc.legend.wait(v, json.loads(raw)["run_id"], timeout=120)
    assert run2["status"] == "done"
    c = json.loads(call(base, "GET", f"/api/documents/{v}/counts", who=REVIEWER)[2])
    assert c["run_id"] == run2["run_id"] and c["sheets"][0]["review"] == "to_review"
    actions = [e["action"] for e in json.loads(call(base, "GET", "/api/audit")[2])["events"]]
    assert {"legend_read", "legend_confirmed", "set_scan_started"} <= set(actions)


def test_rerecording_after_a_restart_is_idempotent(tmp_path, set_pdf, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    data = tmp_path / "data"
    svc = ViewerService(data)
    try:
        v, run = scanned_set(svc, set_pdf)
        sheet = run["sheets"][0]["scan_id"]
        pin = svc.scan_state(sheet)["pins"][0]["pin_id"]
        svc.act(sheet, {"action": "approve", "pin_id": pin, "request_id": rid()})
        scans_before = len(svc._db(svc.store.list_scans))
    finally:
        svc.close()
    # The server stopped while recording: the run is still "recording".
    rfile = data / "documents" / v[7:] / "set_scans" / run["run_id"] / "run.json"
    r = json.loads(rfile.read_text())
    r.update(status="recording", sheets=[], finished_at=None)
    rfile.write_text(json.dumps(r))
    svc = ViewerService(data, version="git:newer-release")  # a deploy changed the version meanwhile
    try:
        assert svc.legend.resume_interrupted() == [run["run_id"]]
        again = svc.legend.wait(v, run["run_id"], timeout=60)
        assert again["status"] == "done" and [s["scan_id"] for s in again["sheets"]] == \
            [s["scan_id"] for s in run["sheets"]]
        assert len(svc._db(svc.store.list_scans)) == scans_before  # nothing recorded twice
        st = svc.scan_state(sheet)
        assert next(p for p in st["pins"] if p["pin_id"] == pin)["state"] == "approved"  # reviews kept
        assert st["detector"]["version"] == run_version(svc, v, run)
    finally:
        svc.close()
    # The job finished while the web process was down: the next start records it.
    svc = ViewerService(data)
    try:
        svc.legend.confirm(v, {})
        req = rid()
        run_id = str(uuid.uuid5(uuid.UUID("5e7d0c3a-9b1f-4c2e-8d6a-2f4b7a9c1e03"), req))
        svc.legend._follow = lambda *a: None  # this "process" dies before it records anything
        started = svc.legend.start_scan(v, req)
        assert started["run_id"] == run_id
        q = svc.legend.jobs
        deadline = time.time() + 60
        while q.get(json.loads((data / "documents" / v[7:] / "set_scans" / run_id / "run.json").read_text())
                    ["job_id"]).status != "done" and time.time() < deadline:
            time.sleep(0.1)
    finally:
        svc.close()
    svc = ViewerService(data)
    try:
        assert run_id in svc.legend.resume_interrupted()
        done = svc.legend.wait(v, run_id, timeout=60)
        assert done["status"] == "done" and all(s["scan_id"] for s in done["sheets"])
    finally:
        svc.close()


def run_version(svc, v, run):
    return json.loads((svc.data_dir / "documents" / v[7:] / "set_scans" / run["run_id"] / "run.json")
                      .read_text())["detector_version"]


def test_no_legend_and_scanned_legend_point_to_the_template_workflow(svc, tmp_path, set_pdf):
    from pdfgen import make_pdf
    v = svc.upload(make_pdf(), "plain.pdf")["document_version"]
    with pytest.raises(Exception) as e:
        svc.legend.read(v)
    assert e.value.code == "legend_not_found" and e.value.status == 422
    assert "template" in e.value.message and "/" not in e.value.message.replace("/Rotate", "")
    (tmp_path / "set.pdf").write_bytes(set_pdf)
    raster_copy(str(tmp_path / "set.pdf"), 0, str(tmp_path / "scanned_legend.pdf"))
    with pikepdf.open(tmp_path / "scanned_legend.pdf") as a, pikepdf.open(tmp_path / "set.pdf") as b:
        a.pages.append(b.pages[1])
        a.save(tmp_path / "scanned_set.pdf")
    v = svc.upload((tmp_path / "scanned_set.pdf").read_bytes(), "scanned.pdf")["document_version"]
    with pytest.raises(Exception) as e:
        svc.legend.read(v, 0)
    assert e.value.code == "legend_is_scanned" and "page 1" in e.value.message and "template" in e.value.message
    with pytest.raises(Exception) as e:
        svc.legend.read(v, 7)
    assert e.value.code == "invalid_page"


def test_delete_removes_the_legend_and_set_scan_files(site, set_pdf):
    base, s, svc = site
    doc = upload(base, who=REVIEWER, pdf=set_pdf, name="set.pdf")
    v = doc["document_version"]
    v_, run = scanned_set(svc, set_pdf)
    assert v_ == v
    d = svc.data_dir / "documents" / v[7:]
    assert (d / "legend.json").is_file() and (d / "set_scans" / run["run_id"] / "result.json").is_file()
    assert call(base, "DELETE", f"/api/documents/{v}", who=REVIEWER)[0] == 200
    assert not d.exists()
    assert call(base, "GET", f"/api/documents/{v}/legend", who=REVIEWER)[0] == 404
    assert call(base, "GET", f"/api/documents/{v}/counts.csv", who=REVIEWER)[0] == 404


def test_http_validation(site, set_pdf):
    base, s, svc = site
    v = upload(base, who=REVIEWER, pdf=set_pdf, name="set.pdf")["document_version"]
    for path, body, code_, status in (
            ("legend/edit", {"op": "rename", "entry_id": "L1", "name": "x"}, "legend_not_read", 409),
            ("legend/read", {"page_index": "0"}, "invalid_page", 400),
            ("set-scans", {"request_id": "nope"}, "invalid_request_id", 400),
            ("legend/save-standard", {"engineer": ""}, "invalid_engineer", 400)):
        code, _, raw = call(base, "POST", f"/api/documents/{v}/{path}", who=REVIEWER, body=body)
        assert (code, err(raw)["code"]) == (status, code_), (path, raw)
    code, _, raw = call(base, "GET", "/api/documents/sha256:nothex/legend", who=REVIEWER)
    assert code == 404


def test_engineer_standard_legend(svc, set_pdf):
    v, _ = scanned_set(svc, set_pdf)
    with pytest.raises(Exception) as e:
        svc.legend.save_standard(v, {"engineer": "Bad<name>"})
    assert e.value.code == "invalid_engineer"
    st = svc.legend.save_standard(v, {"engineer": "Hanson & Reyes"}, reviewer="r@x.y")
    assert st["legend"]["engineer"] == "Hanson & Reyes" and st["legend"]["confirmed"]
    assert svc.legend.library()["engineers"][0]["engineer"] == "Hanson & Reyes"
    comp = st["legend"]["comparison"]
    assert comp["match"] == 5 and comp["new"] == comp["changed"] == comp["missing"] == []
    lg = svc.legend.edit(v, {"op": "rename", "entry_id": "L1", "name": "Plain duplex"})["legend"]
    assert lg["comparison"]["match"] == 5  # a new name alone is not a new symbol
    lg = svc.legend.edit(v, {"op": "set_tag", "entry_id": "L1", "tag": "DX"})["legend"]
    assert [d["tag"] for d in lg["comparison"]["changed"]] == ["DX"]


# ------------------------------------------------------ sandboxed children
@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """Jobs on: the web process must not open a PDF or read a legend itself."""
    def forbidden(*a, **k):
        raise AssertionError("the web process opened a PDF")
    for name in ("inspect", "render_rgb"):
        monkeypatch.setattr(pdf_module, name, forbidden)
    monkeypatch.setattr(legend_tasks, "read_legend", forbidden)
    monkeypatch.setattr(legend_tasks, "scan_set", forbidden)
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    jobs = JobQueue(tmp_path)
    pool = WorkerPool(jobs, tmp_path).start()
    s = ViewerService(tmp_path, jobs=jobs)
    yield s
    s.close()
    pool.stop()


def test_legend_and_whole_set_scan_in_sandboxed_children(sandboxed, set_pdf):
    svc = sandboxed
    t0 = time.time()
    v, run = scanned_set(svc, set_pdf)
    elapsed = time.time() - t0
    kinds = [j.kind for j in svc.jobs.list()]
    assert "read_legend" in kinds and "scan_set" in kinds
    job = next(j for j in svc.jobs.list() if j.kind == "scan_set")
    assert set(job.payload) == {"document_version", "run_id", "legend_sha256"}  # ids only
    assert job.progress == 1.0 and "Searched sheet" in (job.progress_log or "")
    assert job.priority == 10
    for s, sheet in zip(run["sheets"], (SHEET1, SHEET2)):
        near(svc.scan_state(s["scan_id"])["pins"], sheet)
    assert elapsed < 120


def test_scan_set_task_validates_its_payload(tmp_path):
    from pinny.jobs.progress import Progress
    from pinny.jobs.tasks import TaskError
    prog = Progress(None)
    for payload in ({"document_version": "sha256:" + "0" * 64, "run_id": "../x", "legend_sha256": "0" * 64},
                    {"document_version": "x", "run_id": rid(), "legend_sha256": "0" * 64},
                    {"document_version": "sha256:" + "0" * 64, "run_id": rid(), "legend_sha256": "zz"}):
        with pytest.raises(TaskError) as e:
            legend_tasks.scan_set(payload, tmp_path, prog)
        assert e.value.code == "invalid_job"
    with pytest.raises(TaskError) as e:
        legend_tasks.scan_set({"document_version": "sha256:" + "0" * 64, "run_id": rid(),
                               "legend_sha256": "0" * 64}, tmp_path, prog)
    assert e.value.code == "document_version_not_found"
