"""Training site API (docs/training-site.md sections 3 and 4): dashboard,
review queue, page completion, datasets, training runs, benchmarks,
promotion and deactivation.

Signed-in clients come from ``tests/viewer/test_site.py`` (``site`` fixture
and ``call``), so a change of sign-in touches only that module. Training
jobs run in the development in-process runner; the torch tests skip when
torch is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from pinny.jobs import JobQueue  # noqa: E402
from pinny.jobs import train_tasks  # noqa: E402
from pinny.jobs.inprocess import InProcessRunner  # noqa: E402
from pinny.jobs.progress import Progress, job_dir, read as read_progress  # noqa: E402
from tests.viewer.test_modes_fakes import make_model, write_evidence  # noqa: E402
from tests.viewer.test_site import ADMIN, REVIEWER, call, err, site, upload  # noqa: E402,F401
from tests.viewer.test_viewer_api import _template_for  # noqa: E402

HAS_TORCH = importlib.util.find_spec("torch") is not None


def rid() -> str:
    return str(uuid.uuid4())


def get(base, path, who=ADMIN):
    code, _, raw = call(base, "GET", path, who=who)
    return code, json.loads(raw)


def post(base, path, body, who=ADMIN):
    code, _, raw = call(base, "POST", path, who=who, body=body)
    return code, json.loads(raw)


def wait_job(base, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, j = get(base, f"/api/training/jobs/{job_id}")
        assert code == 200, j
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def scanned(base, svc, who=REVIEWER):
    """Upload the test drawing and scan it: five machine pins."""
    doc = upload(base, who=who)
    v = doc["document_version"]
    code, st = post(base, "/api/scans", {"document_version": v, "page_index": 0, "request_id": rid(),
                                         "template_box": _template_for(doc, svc.frame(v, 0))}, who=who)
    assert code == 201, st
    return v, st


def act(base, scan_id, pin_id, action, who=REVIEWER):
    code, out = post(base, f"/api/scans/{scan_id}/actions",
                     {"action": action, "pin_id": pin_id, "request_id": rid()}, who=who)
    assert code == 200, out


def audit_actions(base):
    return [e["action"] for e in get(base, "/api/audit")[1]["events"]]


# ------------------------------------------------------ dashboard and queue
def test_dashboard_counts_labels_and_readiness(site):
    base, _, svc = site
    code, d = get(base, "/api/training/dashboard", who=REVIEWER)
    assert code == 200
    assert d["labels"] == {"positive": 0, "negative": 0, "unlabeled": 0, "withdrawn": 0}
    assert d["readiness"]["verifier"]["ready"] is False and d["active"] == {"verifier": None, "detector": None}
    v, st = scanned(base, svc)
    pins = st["pins"]
    act(base, st["scan_id"], pins[0]["pin_id"], "approve")
    act(base, st["scan_id"], pins[1]["pin_id"], "approve")
    act(base, st["scan_id"], pins[2]["pin_id"], "reject")
    code, d = get(base, "/api/training/dashboard", who=REVIEWER)
    assert d["labels"] == {"positive": 2, "negative": 1, "unlabeled": 2, "withdrawn": 0}
    assert d["pins_reviewed"] == 3 and d["pins_total"] == 5
    assert d["pages"] == {"scanned": 1, "fully_reviewed": 0, "detector_eligible": 0}
    assert d["documents"]["uploaded"] == 1 and d["documents"]["with_labels"] == 1
    checks = {c["name"]: c for c in d["readiness"]["verifier"]["checks"]}
    assert checks["positive labels"] == {"name": "positive labels", "have": 2, "need": 100, "ok": False}
    assert d["thresholds"]["detector"]["complete_pages"] == 30


def test_queue_orders_by_uncertainty_and_mark_page_reviewed(site):
    base, _, svc = site
    v, st = scanned(base, svc)
    code, q = get(base, "/api/training/queue", who=REVIEWER)
    assert code == 200 and q["total"] == 5 and len(q["items"]) == 5
    margins = [i["margin"] for i in q["items"]]
    assert margins == sorted(margins)
    assert {i["pin_id"] for i in q["items"]} == {p["pin_id"] for p in st["pins"]}
    first = q["items"][0]
    assert first["document_version"] == v and first["page_index"] == 0 and first["filename"] == "plan.pdf"
    [page] = q["pages"]
    assert page["status"] == "needs_review" and page["unreviewed"] == 5
    assert page["first_pin"] == {"scan_id": st["scan_id"], "pin_id": first["pin_id"]}
    assert get(base, "/api/training/queue?limit=2", who=REVIEWER)[1]["items"] == q["items"][:2]
    assert get(base, "/api/training/queue?limit=x", who=REVIEWER)[0] == 400

    body = {"document_version": v, "page_index": 0}
    code, out = post(base, "/api/training/pages/complete", body, who=REVIEWER)
    assert code == 409 and out["error"]["code"] == "page_has_unreviewed_pins"
    assert "5 unreviewed" in out["error"]["message"]
    for i, p in enumerate(st["pins"]):
        act(base, st["scan_id"], p["pin_id"], "approve" if i else "reject")
    [page] = get(base, "/api/training/queue", who=REVIEWER)[1]["pages"]
    assert page["status"] == "ready_to_mark" and page["unreviewed"] == 0
    code, out = post(base, "/api/training/pages/complete", body, who=REVIEWER)
    assert code == 200 and out["status"] == "complete" and out["reviewer"] == REVIEWER
    assert post(base, "/api/training/pages/complete", body, who=REVIEWER)[0] == 200  # idempotent
    q = get(base, "/api/training/queue", who=REVIEWER)[1]
    assert q["items"] == [] and q["pages"] == []
    d = get(base, "/api/training/dashboard")[1]
    assert d["pages"] == {"scanned": 1, "fully_reviewed": 1, "detector_eligible": 1}
    assert d["labels"]["positive"] == 4 and d["labels"]["negative"] == 1


def test_mark_page_reviewed_validates_its_ids(site):
    base, _, svc = site
    v, _ = scanned(base, svc)
    for body, code, what in (({"document_version": "../x", "page_index": 0}, 400, "invalid_document"),
                             ({"document_version": v, "page_index": -1}, 400, "invalid_page"),
                             ({"document_version": v, "page_index": True}, 400, "invalid_page"),
                             ({"document_version": "sha256:" + "1" * 64, "page_index": 0}, 404,
                              "document_version_not_found"),
                             ({"document_version": v, "page_index": 3}, 404, "page_not_scanned")):
        c, out = post(base, "/api/training/pages/complete", body, who=REVIEWER)
        assert (c, out["error"]["code"]) == (code, what), body


# ------------------------------------------------------------- jobs, datasets
def test_build_dataset_job_progress_log_and_listing(site):
    base, s, svc = site
    code, out = post(base, "/api/training/datasets", {})
    assert code == 202 and out["kind"] == "build_dataset"
    j = wait_job(base, out["job_id"])
    assert j["status"] == "failed" and j["error"]["code"] == "nothing_to_build"  # no scans yet
    v, st = scanned(base, svc)
    for i, p in enumerate(st["pins"]):
        act(base, st["scan_id"], p["pin_id"], "approve" if i else "reject")
    code, out = post(base, "/api/training/datasets", {})
    j = wait_job(base, out["job_id"])
    assert j["status"] == "done" and j["progress"] == 1.0, j
    info = j["result"]
    assert info["counts"]["verifier"][next(k for k in info["documents"] if info["documents"][k])]["pos"] == 4
    assert "exporting reviews" in j["log_tail"] and "dataset " in j["log_tail"]
    assert str(s.settings.data_dir) not in json.dumps(j)
    code, ds = get(base, "/api/training/datasets")
    assert code == 200 and [d["dataset_id"] for d in ds["datasets"]] == [info["dataset_id"]]
    assert ds["datasets"][0]["gate"]["ok"] is False and ds["gate"] == {"test_documents": 3, "test_points": 100}
    code, jobs = get(base, "/api/training/jobs")
    assert [x["kind"] for x in jobs["jobs"]] == ["build_dataset", "build_dataset"]
    assert "log_tail" not in jobs["jobs"][0]
    assert audit_actions(base).count("dataset_build_started") == 2


def test_training_requests_are_validated(site):
    base, s, _ = site
    ds = "a" * 64
    cases = [({"kind": "yolo", "dataset_id": ds}, 400, "invalid_model_kind"),
             ({"kind": "verifier", "dataset_id": "../../etc"}, 400, "invalid_dataset"),
             ({"kind": "verifier", "dataset_id": ds}, 404, "dataset_not_found")]
    for body, code, what in cases:
        c, out = post(base, "/api/training/train", body)
        assert (c, out["error"]["code"]) == (code, what), body
    d = s.settings.data_dir / "datasets" / ds
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{}")
    for body in ({"kind": "verifier", "dataset_id": ds, "epochs": 0},
                 {"kind": "verifier", "dataset_id": ds, "epochs": 1000},
                 {"kind": "verifier", "dataset_id": ds, "epochs": "3"},
                 {"kind": "verifier", "dataset_id": ds, "tiles_per_epoch": 64}):
        c, out = post(base, "/api/training/train", body)
        assert (c, out["error"]["code"]) == (400, "invalid_setting"), body
    for body, code, what in (({"model_id": "../x"}, 400, "invalid_model_id"),
                             ({"model_id": "verifier-nope"}, 404, "unknown_model")):
        c, out = post(base, "/api/training/benchmarks", body)
        assert (c, out["error"]["code"]) == (code, what), body
    m = make_model(s.settings.data_dir, "verifier")
    c, out = post(base, "/api/training/benchmarks", {"model_id": m, "dataset_id": "b" * 64})
    assert (c, out["error"]["code"]) == (404, "dataset_not_found")
    for t in (2, "0.5", True):
        c, out = post(base, "/api/training/benchmarks", {"model_id": m, "dataset_id": ds, "template_threshold": t})
        assert (c, out["error"]["code"]) == (400, "invalid_setting"), t
    for path in ("/api/training/jobs/not-a-uuid", f"/api/training/jobs/{uuid.uuid4()}",
                 f"/api/training/jobs/{str(uuid.uuid4()).upper()}"):
        c, out = get(base, path)
        assert (c, out["error"]["code"]) == (404, "unknown_job"), path
    # A job of another pool is not a training job.
    other = s.training.jobs.submit("selftest", {"action": "ok"})
    assert get(base, f"/api/training/jobs/{other}")[0] == 404
    assert call(base, "POST", f"/api/training/jobs/{other}/cancel", body={})[0] == 404


def test_start_and_cancel_a_training_job(site):
    base, s, _ = site
    ds = "c" * 64
    d = s.settings.data_dir / "datasets" / ds
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{}")
    s.training._runner = None  # keep it queued: no in-process runner picks it up
    code, j = post(base, "/api/training/train", {"kind": "detector", "dataset_id": ds, "epochs": 2,
                                                 "tiles_per_epoch": 64})
    assert code == 202 and j["kind"] == "train_detector" and j["status"] == "queued"
    assert j["payload"] == {"dataset_id": ds, "epochs": 2, "tiles_per_epoch": 64}
    assert j["requested_by"] == ADMIN
    again = post(base, "/api/training/train", {"kind": "detector", "dataset_id": ds, "epochs": 2,
                                               "tiles_per_epoch": 64})[1]
    assert again["job_id"] == j["job_id"]  # a double click reuses the queued job
    code, c = post(base, f"/api/training/jobs/{j['job_id']}/cancel", {})
    assert code == 200 and c["status"] == "cancelled"
    assert get(base, f"/api/training/jobs/{j['job_id']}")[1]["status"] == "cancelled"
    acts = audit_actions(base)
    assert "training_started" in acts and "training_cancelled" in acts


# ------------------------------------------------------- in-process runner
def test_inprocess_runner_reports_progress_and_cancels(tmp_path, monkeypatch):
    started = []

    def slow(p, data_dir, progress, job_id):
        started.append(job_id)
        for i in range(10_000):
            progress.update(i / 10_000, f"step {i} in {data_dir}")
            time.sleep(0.01)
        return {"never": True}

    monkeypatch.setitem(train_tasks.TRAIN_TASKS, "build_dataset", slow)
    q = JobQueue(tmp_path)
    runner = InProcessRunner(q, "train", tmp_path).start()
    try:
        job_id = q.submit("build_dataset", {})
        runner.wake()
        deadline = time.time() + 20
        while time.time() < deadline and not (q.get(job_id).progress or 0) > 0:
            time.sleep(0.05)
        j = q.get(job_id)
        assert j.status == "running" and j.progress > 0
        assert "step " in j.progress_log and str(tmp_path) not in j.progress_log  # paths are hidden
        assert "$PINNY_DATA_DIR" in j.progress_log
        q.cancel(job_id)
        while time.time() < deadline and not q.get(job_id).finished:
            time.sleep(0.05)
        assert q.get(job_id).status == "cancelled"
    finally:
        runner.stop()


def test_progress_files_and_log_limit(tmp_path):
    job_id = str(uuid.uuid4())
    d = job_dir(tmp_path, job_id)
    p = Progress(d, data_dir=tmp_path)
    p.update(0.5, "half")
    p.update(0.2, "never goes back")
    frac, tail = read_progress(tmp_path, job_id)
    assert frac == 0.5 and tail.splitlines()[-1].endswith("never goes back")
    assert read_progress(tmp_path, "../../etc") == (None, None)
    with pytest.raises(ValueError):
        job_dir(tmp_path, "../x")


# --------------------------------------------------------- models, promote
def _fake_benchmark(s, model_id, *, promote=True, finish=True):
    """A benchmark job as a worker would leave it, with its promotion report."""
    q = s.training.jobs
    job_id = q.submit("benchmark", {"model_id": model_id, "dataset_id": "d" * 64})
    assert q.claim("train", "test-worker").job_id == job_id
    report = train_tasks.promotion_report_path(s.settings.data_dir, job_id)
    report.parent.mkdir(parents=True)
    write_evidence(report, model_id, promote=promote, failed_conditions=[] if promote else ["min_test_documents"])
    if finish:
        result = {"model_id": model_id, "kind": model_id.split("-")[0], "promote": promote,
                  "candidate_mode": "template+verifier", "baseline_mode": "template",
                  "baseline": {"counts": {"true_positives": 90, "false_positives": 10, "false_negatives": 10},
                               "metrics": {"precision": 0.9, "recall": 0.9}},
                  "candidate": {"counts": {"true_positives": 95, "false_positives": 2, "false_negatives": 5},
                                "metrics": {"precision": 0.979, "recall": 0.95}},
                  "failed_conditions": [] if promote else ["min_test_documents"], "conditions": []}
        q.finish(job_id, "test-worker", result)
    return job_id


def test_promote_by_benchmark_job_and_deactivate(site):
    base, s, _ = site
    s.training._runner = None
    good = make_model(s.settings.data_dir, "verifier", stamp="20260101T000000Z")
    weak = make_model(s.settings.data_dir, "verifier", stamp="20260102T000000Z")
    good_job = _fake_benchmark(s, good)
    weak_job = _fake_benchmark(s, weak, promote=False)
    running = _fake_benchmark(s, weak, finish=False)

    code, m = get(base, "/api/training/models", who=REVIEWER)
    assert code == 200 and m["active"] == {"verifier": None, "detector": None}
    by_id = {x["model_id"]: x for x in m["models"]}
    assert [b["job_id"] for b in by_id[good]["benchmarks"]] == [good_job]
    assert by_id[good]["benchmarks"][0]["candidate"]["metrics"]["precision"] == 0.979

    for body, code, what in (({"benchmark_job_id": "nope"}, 400, "invalid_job"),
                             ({"benchmark_job_id": str(uuid.uuid4())}, 404, "unknown_job"),
                             ({"benchmark_job_id": running}, 409, "benchmark_not_finished"),
                             ({"benchmark_job_id": weak_job}, 409, "promotion_not_recommended"),
                             ({"model_id": good}, 400, "invalid_job")):
        c, out = post(base, "/api/training/promote", body)
        assert (c, out["error"]["code"]) == (code, what), body
    # Never a path: the report comes from the job's own directory.
    c, out = post(base, "/api/training/promote", {"benchmark_job_id": good_job, "evidence": "/etc/passwd"})
    assert c == 200 and out["model_id"] == good and out["benchmark_job_id"] == good_job
    assert s.service.registry.active("verifier") == good
    m = get(base, "/api/training/models", who=REVIEWER)[1]
    assert m["active"]["verifier"]["model_id"] == good
    assert "evidence_path" not in json.dumps(m) and str(s.settings.data_dir) not in json.dumps(m)
    assert {x["model_id"]: x["active"] for x in m["models"]} == {good: True, weak: False}
    # A tampered report is refused by the registry.
    tampered = _fake_benchmark(s, weak)
    report = train_tasks.promotion_report_path(s.settings.data_dir, tampered)
    report.write_text(json.dumps(dict(json.loads(report.read_text()), model_id=good,
                                      candidate={"mode": "template+verifier", "model_id": good})))
    c, out = post(base, "/api/training/promote", {"benchmark_job_id": tampered})
    assert c == 409 and out["error"]["code"] == "evidence_wrong_model"
    assert str(s.settings.data_dir) not in out["error"]["message"]

    assert post(base, "/api/training/promote", {"benchmark_job_id": good_job}, who=REVIEWER)[0] == 403
    assert post(base, "/api/training/deactivate", {"kind": "verifier"}, who=REVIEWER)[0] == 403
    c, out = post(base, "/api/training/deactivate", {"kind": "yolo"})
    assert (c, out["error"]["code"]) == (400, "invalid_model_kind")
    c, out = post(base, "/api/training/deactivate", {"kind": "verifier"})
    assert c == 200 and out == {"kind": "verifier", "deactivated": good}
    assert s.service.registry.active("verifier") is None
    assert post(base, "/api/training/deactivate", {"kind": "verifier"})[1]["deactivated"] is None
    events = get(base, "/api/audit")[1]["events"]
    promoted = [e for e in events if e["action"] == "model_promoted"]
    assert len(promoted) == 1 and promoted[0]["target"] == good and promoted[0]["detail"]["benchmark_job_id"] == good_job
    assert [e["action"] for e in events].count("model_deactivated") == 1


# --------------------------------------------------------------- queue schema
def test_old_jobs_database_gains_progress_columns(tmp_path):
    db = sqlite3.connect(tmp_path / "jobs.sqlite3")
    db.executescript("""CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, pool TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, dedupe_key TEXT,
        status TEXT NOT NULL, result TEXT, error_code TEXT, error_message TEXT, error_status INTEGER,
        log_tail TEXT, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, worker_id TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0, requested_by TEXT, created_at REAL NOT NULL,
        started_at REAL, heartbeat_at REAL, finished_at REAL);
        INSERT INTO jobs(job_id, kind, pool, payload, status, max_attempts, created_at)
        VALUES ('old', 'scan', 'scan', '{}', 'done', 2, 1.0);""")
    db.close()
    q = JobQueue(tmp_path)
    old = q.get("old")
    assert old.status == "done" and old.progress is None and old.progress_log is None
    JobQueue(tmp_path)  # a second open is a no-op
    job = q.submit("benchmark", {})
    q.claim("train", "w")
    q.set_progress(job, "w", 0.25, "x" * 5000)
    j = q.get(job)
    assert j.progress == 0.25 and len(j.progress_log) == 4000
    q.finish(job, "w", {})
    assert q.get(job).progress == 1.0


def test_prune_keeps_training_history(tmp_path):
    now = [1_000_000.0]
    q = JobQueue(tmp_path, clock=lambda: now[0])
    ids = {}
    for kind, pool in (("selftest", "interactive"), ("benchmark", "train")):
        ids[kind] = q.submit(kind, {})
        q.claim(pool, "w")
        q.finish(ids[kind], "w", {})
    now[0] += 30 * 86400
    assert q.prune(older_than_days=7) == 1
    assert q.get(ids["benchmark"]).status == "done"
    with pytest.raises(KeyError):
        q.get(ids["selftest"])


# ------------------------------------------------------------ with torch
@pytest.mark.skipif(not HAS_TORCH, reason="torch is not installed")
def test_train_benchmark_and_gate_on_a_synthetic_dataset(site):
    """The real trainer and benchmark through the API. Synthetic data never
    passes the promotion gate, so promotion is refused."""
    from pinny.training.synthetic import synthesize_dataset
    base, s, _ = site
    res = synthesize_dataset(s.settings.data_dir / "datasets", documents=6, pages_per_document=2, seed=0)
    code, ds = get(base, "/api/training/datasets")
    assert [d["dataset_id"] for d in ds["datasets"]] == [res.dataset_id] and ds["datasets"][0]["synthetic"]
    code, j = post(base, "/api/training/train", {"kind": "verifier", "dataset_id": res.dataset_id, "epochs": 2})
    assert code == 202
    j = wait_job(base, j["job_id"], timeout=300)
    assert j["status"] == "done", j
    model_id = j["result"]["model_id"]
    assert j["result"]["synthetic_only"] is True and "epoch 2" in j["log_tail"]
    code, b = post(base, "/api/training/benchmarks", {"model_id": model_id})  # defaults to its dataset
    assert code == 202 and b["payload"] == {"model_id": model_id, "dataset_id": res.dataset_id}
    b = wait_job(base, b["job_id"], timeout=300)
    assert b["status"] == "done", b
    assert b["result"]["promote"] is False and "not_synthetic_only" in b["result"]["failed_conditions"]
    m = get(base, "/api/training/models")[1]
    [entry] = [x for x in m["models"] if x["model_id"] == model_id]
    assert entry["benchmarks"][0]["job_id"] == b["job_id"]
    c, out = post(base, "/api/training/promote", {"benchmark_job_id": b["job_id"]})
    assert c == 409 and out["error"]["code"] == "promotion_not_recommended"
    assert s.service.registry.active("verifier") is None


def test_build_dataset_skips_deleted_drawings(site):
    """Found on the first real server: once any reviewed drawing was deleted,
    every dataset build failed with document_version_not_found."""
    from pdfgen import make_pdf

    base, s, svc = site
    versions = []
    for tag in ("keep", "gone"):
        doc = upload(base, who=REVIEWER, pdf=make_pdf(tag=tag), name=f"{tag}.pdf")
        v = doc["document_version"]
        code, st = post(base, "/api/scans", {"document_version": v, "page_index": 0, "request_id": rid(),
                                             "template_box": _template_for(doc, svc.frame(v, 0))}, who=REVIEWER)
        assert code == 201, st
        for i, p in enumerate(st["pins"]):
            act(base, st["scan_id"], p["pin_id"], "approve" if i else "reject")
        versions.append(v)
    code, _, raw = call(base, "DELETE", f"/api/documents/{versions[1]}", who=ADMIN)
    assert code == 200, raw
    code, out = post(base, "/api/training/datasets", {})
    j = wait_job(base, out["job_id"])
    assert j["status"] == "done", j
    total_pos = sum(c["pos"] for c in j["result"]["counts"]["verifier"].values())
    assert total_pos == 4  # only the kept drawing's four approved pins
    assert "deleted drawings" in j["log_tail"]


def test_a_rescan_supersedes_the_old_scan_for_marking_and_the_queue(site):
    """Found by review: pins of an earlier scan of the same page blocked
    "mark fully reviewed" and showed up in the label queue, inviting reviewers
    to reject duplicates that sit on real receptacles."""
    base, _, svc = site
    v, old = scanned(base, svc)
    doc = {"document_version": v}
    code, new = post(base, "/api/scans", {"document_version": v, "page_index": 0, "request_id": rid(),
                                          "template_box": _template_for(doc, svc.frame(v, 0))}, who=REVIEWER)
    assert code == 201 and new["scan_id"] != old["scan_id"]
    q = get(base, "/api/training/queue", who=REVIEWER)[1]
    assert {i["scan_id"] for i in q["items"]} == {new["scan_id"]}  # nothing from the old scan
    assert q["pages"][0]["unreviewed"] == 5 and q["pages"][0]["scans"] == 2
    for p in new["pins"]:
        act(base, new["scan_id"], p["pin_id"], "approve")
    code, out = post(base, "/api/training/pages/complete", {"document_version": v, "page_index": 0},
                     who=REVIEWER)
    assert code == 200 and out["status"] == "complete", out


def test_a_sandboxed_dataset_build_has_missing_pages_rendered_by_the_workers(site, monkeypatch):
    """Found by review: the train child (web user) rendered pages straight from
    the uploaded PDF when a PNG was missing (e.g. after a restore, which leaves
    page images out). In a sandboxed child it now asks for render_page jobs."""
    from pinny.jobs.progress import Progress
    from pinny.render import RenderService
    from pinny.viewer.render_client import JobRenderClient

    base, s, svc = site
    v, st = scanned(base, svc)
    for i, p in enumerate(st["pins"]):
        act(base, st["scan_id"], p["pin_id"], "approve" if i else "reject")
    data = s.settings.data_dir
    for png in (data / "documents").rglob("pages/*.png"):
        png.unlink()
    asked = []

    def fake_job(self, version, page_index):  # what the interactive worker would do
        asked.append((version, page_index))
        RenderService(data).render_page_png(version, page_index)

    monkeypatch.setattr(JobRenderClient, "_render_job", fake_job)
    monkeypatch.setattr(train_tasks, "RENDER_IN_WORKERS", True)
    job_id = "test"
    info = train_tasks.build_dataset({}, data, Progress(None, data_dir=data), job_id)
    assert asked == [(v, 0)]
    assert sum(c["pos"] for c in info["counts"]["verifier"].values()) == 4


def test_deleting_a_drawing_removes_datasets_built_from_it(site):
    """Found by review: datasets keep whole-page images, so a deleted drawing
    lived on in datasets/<id>/detector/*.png and in every backup."""
    base, s, svc = site
    v, st = scanned(base, svc)
    for i, p in enumerate(st["pins"]):
        act(base, st["scan_id"], p["pin_id"], "approve" if i else "reject")
    code, out = post(base, "/api/training/datasets", {})
    j = wait_job(base, out["job_id"])
    ds = s.settings.data_dir / "datasets" / j["result"]["dataset_id"]
    assert ds.is_dir()
    code, _, raw = call(base, "DELETE", f"/api/documents/{v}", who=ADMIN)
    assert code == 200 and json.loads(raw)["datasets_deleted"] == [j["result"]["dataset_id"]]
    assert not ds.exists()


def test_review_clicks_do_not_wait_for_training_crops(site, monkeypatch):
    """Found by review: every approve/reject decoded a page image on the one
    store thread. Crops are now cut on their own thread."""
    import threading as _th
    base, s, svc = site
    v, st = scanned(base, svc)
    gate = _th.Event()
    real = svc._crop_store.crop_renderer

    def slow(spec):
        gate.wait(10)
        return real(spec)

    svc._crop_store.crop_renderer = slow
    t0 = time.time()
    act(base, st["scan_id"], st["pins"][0]["pin_id"], "approve")
    act(base, st["scan_id"], st["pins"][1]["pin_id"], "reject")
    assert time.time() - t0 < 5  # neither click waited for the (blocked) crop
    gate.set()
    svc.wait_crops()
    svc.wait_crops()
    assert svc._db(svc.store.crop_status_counts) == {"written": 2}
