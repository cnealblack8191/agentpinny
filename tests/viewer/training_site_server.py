"""Server for tests/viewer/test_training_browser.mjs: the real site in
production mode (Pinny sign-in) with small training data:

* one drawing, scanned, two pins approved;
* a synthetic dataset;
* two verifier models, each with a finished benchmark job: the gate
  recommends one and not the other;
* a training job that a fake worker keeps running (progress and log grow
  every second) until it is cancelled.

Training jobs started from the pages run in the development in-process
runner. Prints one JSON line: base URL, set-password tokens and ids.

    python tests/viewer/training_site_server.py <work dir>
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "viewer"))

from pdfgen import make_pdf  # noqa: E402

from pinny.jobs import train_tasks  # noqa: E402
from pinny.training.synthetic import synthesize_dataset  # noqa: E402
from pinny.viewer import ViewerService  # noqa: E402
from pinny.viewer.server import Site, SiteServer  # noqa: E402
from pinny.viewer.settings import Settings  # noqa: E402
from tests.viewer.test_modes_fakes import make_model, write_evidence  # noqa: E402
from tests.viewer.test_viewer_api import _template_for  # noqa: E402

ADMIN = "boss@example.com"
REVIEWER = "assistant@example.com"


def fake_benchmark(q, data_dir, model_id, promote):
    job_id = q.submit("benchmark", {"model_id": model_id, "dataset_id": "d" * 64}, requested_by=ADMIN)
    q.claim("train", "fake-worker")
    path = train_tasks.promotion_report_path(data_dir, job_id)
    path.parent.mkdir(parents=True)
    failed = [] if promote else ["meaningful_improvement", "min_test_documents"]
    write_evidence(path, model_id, promote=promote, failed_conditions=failed)
    cand = ({"true_positives": 118, "false_positives": 3, "false_negatives": 4} if promote
            else {"true_positives": 60, "false_positives": 30, "false_negatives": 62})
    base = {"true_positives": 112, "false_positives": 20, "false_negatives": 10}

    def side(c):
        tp, fp, fn = c["true_positives"], c["false_positives"], c["false_negatives"]
        return {"counts": c, "metrics": {"precision": tp / (tp + fp), "recall": tp / (tp + fn)}}
    conditions = [{"name": "recall_not_below_baseline", "passed": promote,
                   "requirement": "candidate recall >= baseline recall"},
                  {"name": "min_test_documents", "passed": promote, "requirement": ">= 3 test documents"}]
    q.finish(job_id, "fake-worker", {
        "model_id": model_id, "kind": "verifier", "candidate_mode": "template+verifier",
        "baseline_mode": "template", "promote": promote, "dataset_id": "d" * 64, "split": "test",
        "synthetic_only": False, "label": "real drawings, unverified reference", "tolerance_px": 12,
        "page_count": 12, "document_count": 4 if promote else 2, "reference_points": 122,
        "conditions": conditions, "failed_conditions": failed,
        "baseline": side(base), "candidate": side(cand)})
    return job_id


def fake_running_job(q, stop):
    """A training job a fake worker is running: progress and log grow until
    the job is cancelled, then the worker ends it as a real one would."""
    job_id = q.submit("train_verifier", {"dataset_id": "e" * 64, "epochs": 30}, requested_by=ADMIN)
    q.claim("train", "fake-trainer")

    def run():
        epoch = 0
        lines = []
        while not stop.is_set():
            if not q.heartbeat(job_id, "fake-trainer"):
                q.fail(job_id, "fake-trainer", "cancelled", "The job was cancelled.", 409)
                return
            epoch = min(epoch + 1, 29)
            lines.append(f"epoch {epoch}: train_loss={1 / epoch:.4f} val_loss={1.2 / epoch:.4f}")
            q.set_progress(job_id, "fake-trainer", epoch / 30, "\n".join(lines) + "\n")
            time.sleep(1.0)
    threading.Thread(target=run, daemon=True).start()
    return job_id


def main() -> None:
    work = Path(sys.argv[1])
    data = work / "data"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    origin = f"http://127.0.0.1:{port}"
    settings = Settings(env="production", data_dir=data, origin=origin, admin_emails=(ADMIN,), version="e2e")
    svc = ViewerService(data)
    site = Site(svc, settings)
    site.sitedb.put_member(REVIEWER, "reviewer", actor="setup")
    links = {e: site.sitedb.issue_setup_link(e, actor="setup", now=time.time())[0] for e in (ADMIN, REVIEWER)}

    doc = svc.upload(make_pdf(), "floor-plan.pdf")
    v = doc["document_version"]
    st = svc.scan(document_version=v, page_index=0, template_box=_template_for(doc, svc.frame(v, 0)),
                  request_id=str(uuid.uuid4()))
    for p in st["pins"][:2]:
        svc.act(st["scan_id"], {"action": "approve", "pin_id": p["pin_id"], "request_id": str(uuid.uuid4())},
                reviewer=REVIEWER)

    synth = synthesize_dataset(data / "datasets", documents=4, pages_per_document=1, seed=1)
    q = site.training.jobs
    good = make_model(data, "verifier", stamp="20260901T100000Z")
    weak = make_model(data, "verifier", stamp="20260902T100000Z")
    good_job = fake_benchmark(q, data, good, True)
    weak_job = fake_benchmark(q, data, weak, False)
    stop = threading.Event()
    running = fake_running_job(q, stop)

    httpd = SiteServer(site, "127.0.0.1", port)
    print(json.dumps({"base": origin, "links": links, "document_version": v, "scan_id": st["scan_id"],
                      "pins": [p["pin_id"] for p in st["pins"]], "dataset_id": synth.dataset_id,
                      "good_model": good, "weak_model": weak, "good_job": good_job, "weak_job": weak_job,
                      "running_job": running}), flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
