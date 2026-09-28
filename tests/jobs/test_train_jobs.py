"""The ``train`` pool under a real worker: each job in a sandboxed child
(docs/training-site.md section 4). The torch tests skip without torch."""

from __future__ import annotations

import importlib.util
import threading
import time

import pytest

from pinny.jobs import JobQueue, limits
from pinny.jobs.worker import POOL_NAMES, Worker, WorkerPool, child_env, main as worker_main

HAS_TORCH = importlib.util.find_spec("torch") is not None


def test_train_pool_and_limits():
    assert "train" in POOL_NAMES
    for kind in limits.TRAIN_KINDS:
        assert limits.POOLS[kind] == "train"
        lim = limits.LIMITS[kind]
        assert lim.memory_mb <= 5120 and lim.wall_s >= 3600 and lim.max_attempts == 1
    assert limits.LIMITS["train_verifier"].wall_s == 4 * 3600
    assert child_env()["OMP_NUM_THREADS"] == "1"
    assert child_env(limits.LIMITS["train_verifier"].threads)["OMP_NUM_THREADS"] == "2"
    with pytest.raises(SystemExit):  # argparse accepts the pool, then wants a data dir
        worker_main(["--pool", "nope"])
    assert worker_main(["--pool", "train", "--data-dir", ""]) == 2


def test_train_job_runs_in_a_child_and_reports_its_log(tmp_path):
    q = JobQueue(tmp_path)
    job_id = q.submit("build_dataset", {})
    assert Worker(q, "train", tmp_path).run_once()
    j = q.get(job_id)
    assert j.status == "failed" and j.error_code == "nothing_to_build" and j.error_status == 422
    assert "exporting reviews" in j.progress_log
    bad = q.submit("train_verifier", {"dataset_id": "../../etc"})
    Worker(q, "train", tmp_path).run_once()
    assert q.get(bad).error_code == "invalid_job"


@pytest.mark.skipif(not HAS_TORCH, reason="torch is not installed")
def test_training_in_a_child_reports_progress_and_can_be_cancelled(tmp_path):
    from pinny.training.synthetic import synthesize_dataset
    res = synthesize_dataset(tmp_path / "datasets", documents=6, pages_per_document=2, seed=0)
    q = JobQueue(tmp_path)
    job_id = q.submit("train_verifier", {"dataset_id": res.dataset_id, "epochs": 200})
    w = Worker(q, "train", tmp_path)
    t = threading.Thread(target=w.run_once, daemon=True)
    t.start()
    deadline = time.time() + 120
    while time.time() < deadline and "epoch 1:" not in (q.get(job_id).progress_log or ""):
        time.sleep(0.2)
    j = q.get(job_id)
    assert j.status == "running" and j.progress > 0.01, j
    q.cancel(job_id)
    t.join(30)
    j = q.get(job_id)
    assert j.status == "cancelled"
    assert not (tmp_path / "models").exists() or not any((tmp_path / "models").iterdir())


# ------------------------------------------------ shutdown and restart
def test_a_stopping_worker_hands_its_job_back(tmp_path):
    """Nightly shutdown: a worker that stops mid-job requeues a job with
    attempts left (here an interactive one) instead of blaming the job."""
    q = JobQueue(tmp_path)
    pool = WorkerPool(q, tmp_path, sizes={"interactive": 1}).start()
    job_id = q.submit("selftest", {"action": "sleep", "seconds": 60})
    deadline = time.time() + 20
    while time.time() < deadline and q.get(job_id).status != "running":
        time.sleep(0.05)
    time.sleep(0.5)  # the child is inside its task
    pool.stop()
    j = q.get(job_id)
    assert (j.status, j.error_code, j.worker_id) == ("queued", None, None)


def test_interrupted_and_lost_training_jobs_fail_with_a_clear_reason(tmp_path):
    q = JobQueue(tmp_path)
    a = q.submit("train_verifier", {"dataset_id": "a" * 64})
    q.claim("train", "w")
    assert q.interrupt(a, "w") == "failed"  # one attempt: never silently rerun a long job
    j = q.get(a)
    assert j.error_code == "interrupted" and "server stopped" in j.error_message and j.finished
    assert q.interrupt(a, "w") == "failed"  # no longer running: unchanged
    b = q.submit("benchmark", {})
    q.claim("train", "w")
    q.cancel(b)
    assert q.interrupt(b, "w") == "cancelled"
    # A worker that died without stopping (power off, kill -9): the next
    # worker's recovery fails the job instead of leaving it running.
    c = q.submit("train_detector", {"dataset_id": "a" * 64})
    q.claim("train", "gone")
    assert q.recover(stale_after=0) == {"requeued": 0, "failed": 1}
    j = q.get(c)
    assert j.status == "failed" and j.error_code == "worker_lost" and "restarted" in j.error_message


@pytest.mark.skipif(not HAS_TORCH, reason="torch is not installed")
def test_stopping_the_train_worker_fails_its_run_as_interrupted(tmp_path):
    from pinny.training.synthetic import synthesize_dataset
    res = synthesize_dataset(tmp_path / "datasets", documents=6, pages_per_document=2, seed=0)
    q = JobQueue(tmp_path)
    pool = WorkerPool(q, tmp_path, sizes={"train": 1}).start()
    job_id = q.submit("train_verifier", {"dataset_id": res.dataset_id, "epochs": 200})
    deadline = time.time() + 120
    while time.time() < deadline and "epoch 1:" not in (q.get(job_id).progress_log or ""):
        time.sleep(0.2)
    pool.stop()
    j = q.get(job_id)
    assert (j.status, j.error_code) == ("failed", "interrupted"), j
