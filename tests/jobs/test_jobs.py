"""Job queue, worker and sandbox (docs/training-site.md section 4).

The sandbox tests run real child processes with the ``selftest`` job kind,
which the web tier never submits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pinny.jobs import JobError, JobQueue, limits
from pinny.jobs.child import network_isolated
from pinny.jobs.queue import PRIORITY_BATCH
from pinny.jobs.worker import Worker, WorkerPool

REPO = Path(__file__).resolve().parents[2]


def _can_isolate() -> bool:
    code = ("import ctypes,socket,sys;"
            "r=ctypes.CDLL(None,use_errno=True).unshare(0x10000000|0x40000000);"
            "sys.exit(0 if r==0 and {n for _,n in socket.if_nameindex()}<={'lo'} else 1)")
    return subprocess.run([sys.executable, "-c", code]).returncode == 0


CAN_ISOLATE = _can_isolate()


@pytest.fixture
def q(tmp_path):
    return JobQueue(tmp_path)


def run(q, tmp_path, payload, *, kind="selftest", require_isolation=False, **kw):
    job_id = q.submit(kind, payload, **kw)
    assert Worker(q, "interactive" if kind != "scan" else "scan", tmp_path,
                  require_isolation=require_isolation).run_once()
    return q.get(job_id)


# ----------------------------------------------------------------- queue
def test_queue_lifecycle_priority_and_dedupe(q):
    a = q.submit("selftest", {"action": "ok"}, priority=PRIORITY_BATCH)
    b = q.submit("selftest", {"action": "ok"})
    assert q.claim("interactive", "w1").job_id == b  # interactive before batch
    assert q.claim("scan", "w1") is None
    job = q.claim("interactive", "w1")
    assert job.job_id == a and job.status == "running" and job.attempts == 1
    q.finish(a, "w1", {"x": 1})
    assert q.get(a).status == "done" and q.get(a).result == {"x": 1}
    q.finish(a, "w2", {"x": 2})  # not its job: ignored
    assert q.get(a).result == {"x": 1}
    d1 = q.submit("render_page", {"document_version": "v", "page_index": 0}, dedupe_key="k")
    assert q.submit("render_page", {}, dedupe_key="k") == d1
    q.cancel(d1)
    assert q.get(d1).status == "cancelled"
    assert q.submit("render_page", {}, dedupe_key="k") != d1  # finished jobs are not reused
    with pytest.raises(ValueError):
        q.submit("rm_rf", {})


def test_wait_raises_job_errors(q):
    j = q.submit("selftest", {})
    q.claim("interactive", "w")
    q.fail(j, "w", "page_too_large", "Too big.", 422)
    with pytest.raises(JobError) as e:
        q.wait(j, 1)
    assert (e.value.code, e.value.status, e.value.message) == ("page_too_large", 422, "Too big.")
    with pytest.raises(JobError) as e:
        q.wait(q.submit("selftest", {}), 0.05)
    assert e.value.code == "job_wait_timeout" and e.value.status == 503


def test_recover_requeues_then_gives_up(q):
    t = [1000.0]
    q._clock = lambda: t[0]
    j = q.submit("selftest", {})
    q.claim("interactive", "dead-worker")
    t[0] += 5
    assert q.recover(stale_after=30) == {"requeued": 0, "failed": 0}
    t[0] += 60
    assert q.recover(stale_after=30) == {"requeued": 1, "failed": 0}
    assert q.get(j).status == "queued"
    q.claim("interactive", "dead-again")
    t[0] += 60
    assert q.recover(stale_after=30) == {"requeued": 0, "failed": 1}
    job = q.get(j)
    assert (job.status, job.error_code, job.attempts) == ("failed", "worker_lost", 2)


# --------------------------------------------------------------- sandbox
def test_child_runs_a_job_with_a_clean_environment(q, tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_ALB_ARN", "secret-arn")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    job = run(q, tmp_path, {"action": "env"})
    assert job.status == "done", job.log_tail
    env = set(job.result["env"])
    assert not {e for e in env if e.startswith(("PINNY_", "AWS_"))}
    assert env <= {"PATH", "HOME", "LANG", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "OMP_NUM_THREADS",
                   "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OPENCV_FOR_THREADS_NUM", "LC_CTYPE"}


@pytest.mark.skipif(not CAN_ISOLATE, reason="this kernel does not allow unprivileged network namespaces")
def test_child_has_no_network(q, tmp_path):
    job = run(q, tmp_path, {"action": "network"}, require_isolation=True)
    assert job.status == "done" and job.result["network"] is False
    assert not network_isolated()  # while this test process still has its network


def test_memory_limit(q, tmp_path):
    job = run(q, tmp_path, {"action": "allocate", "mb": limits.LIMITS["selftest"].memory_mb + 512})
    assert (job.status, job.error_code, job.error_status) == ("failed", "out_of_memory", 422)
    assert "more memory" in job.error_message


def test_cpu_limit(q, tmp_path):
    job = run(q, tmp_path, {"action": "spin", "cpu_s": 1})
    assert (job.status, job.error_code) == ("failed", "cpu_limit"), job.log_tail


def test_wall_clock_limit_kills_a_hung_job(q, tmp_path, monkeypatch):
    monkeypatch.setitem(limits.LIMITS, "selftest", limits.Limits(memory_mb=1024, cpu_s=30, wall_s=2))
    t = time.monotonic()
    job = run(q, tmp_path, {"action": "sleep", "seconds": 60})
    assert (job.status, job.error_code, job.error_status) == ("failed", "timeout", 504)
    assert time.monotonic() - t < limits.HEARTBEAT_S * 2 + 5


def test_file_size_limit(q, tmp_path):
    job = run(q, tmp_path, {"action": "write", "mb": limits.LIMITS["selftest"].file_mb + 1})
    assert job.status == "failed", job.result  # SIGXFSZ is ignored, so the write raises and the job fails
    assert job.error_code == "job_failed" and "File too large" in job.log_tail
    assert (tmp_path / "tmp" / "selftest.bin").stat().st_size <= limits.LIMITS["selftest"].file_mb * limits.MB


def test_internal_errors_stay_in_the_log(q, tmp_path):
    job = run(q, tmp_path, {"action": "crash"})
    assert (job.error_code, job.error_status) == ("job_failed", 500)
    assert "secret" not in job.error_message and "secret internal detail" in job.log_tail
    job = run(q, tmp_path, {"action": "fail"})
    assert (job.error_code, job.error_status, job.error_message) == ("selftest_failed", 422, "Expected failure.")


def test_payloads_are_validated_in_the_child(q, tmp_path):
    for kind, payload in (("render_page", {"document_version": "../../etc/passwd", "page_index": 0}),
                          ("render_page", {"document_version": "sha256:" + "0" * 64, "page_index": -1}),
                          ("ingest", {"staging_id": "../../secret"}),
                          ("template", {"document_version": "sha256:" + "0" * 64, "page_index": 0,
                                        "box": {"x": 1.5, "y": 0, "width": 10, "height": 10}})):
        job = run(q, tmp_path, payload, kind=kind)
        assert job.status == "failed" and job.error_status == 400, (kind, job.error_code, job.log_tail)
    job = run(q, tmp_path, {"document_version": "sha256:" + "0" * 64, "page_index": 0}, kind="render_page")
    assert job.error_code == "document_version_not_found" and job.error_status == 404


def test_cancel_a_running_job(q, tmp_path):
    job_id = q.submit("selftest", {"action": "sleep", "seconds": 60})
    w = Worker(q, "interactive", tmp_path)
    t = threading.Thread(target=w.run_once)
    t.start()
    deadline = time.time() + 10
    while q.get(job_id).status != "running" and time.time() < deadline:
        time.sleep(0.05)
    q.cancel(job_id)
    t.join(limits.HEARTBEAT_S * 2 + 5)
    assert not t.is_alive()
    assert q.get(job_id).status == "cancelled"


def _children_of_marker(tmp_path):
    """pids of job children started for this data dir."""
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if (d / "stat").read_text().split(")")[-1].split()[0] in ("Z", "X"):
                continue  # dead, waiting to be reaped
            cmd = (d / "cmdline").read_bytes().split(b"\0")
            cwd = os.readlink(d / "cwd")
        except OSError:
            continue
        if b"pinny.jobs.child" in cmd and cwd == str(tmp_path.resolve()):
            pids.append(int(d.name))
    return pids


def test_killed_worker_job_is_recovered(q, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.Popen([sys.executable, "-m", "pinny.jobs.worker", "--pool", "interactive",
                             "--data-dir", str(tmp_path), "--allow-network"], env=env, cwd=REPO,
                            start_new_session=True)
    try:
        job_id = q.submit("selftest", {"action": "sleep_first", "seconds": 120})
        marker = tmp_path / "tmp" / "selftest.marker"  # the first attempt is inside its task
        deadline = time.time() + 30
        while not (marker.exists() and _children_of_marker(tmp_path)) and time.time() < deadline:
            time.sleep(0.05)
        assert q.get(job_id).status == "running"
        os.killpg(proc.pid, signal.SIGKILL)  # the worker dies mid-job
        proc.wait(10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    deadline = time.time() + 10
    while _children_of_marker(tmp_path) and time.time() < deadline:
        time.sleep(0.1)
    assert _children_of_marker(tmp_path) == []  # its child died with it (PR_SET_PDEATHSIG)
    assert q.get(job_id).status == "running"  # until recovery notices the stale heartbeat
    assert q.recover(stale_after=0) == {"requeued": 1, "failed": 0}
    assert Worker(q, "interactive", tmp_path).run_once()
    job = q.get(job_id)
    assert (job.status, job.result, job.attempts) == ("done", {"attempt": "second"}, 2)


def test_worker_pool_threads(q, tmp_path):
    pool = WorkerPool(q, tmp_path, sizes={"interactive": 2}).start()
    try:
        ids = [q.submit("selftest", {"action": "ok"}) for _ in range(4)]
        assert [q.wait(i, 30) for i in ids] == [{"ok": True}] * 4
    finally:
        pool.stop()


def test_prune_removes_old_finished_jobs(q):
    t = [1000.0]
    q._clock = lambda: t[0]
    old = q.submit("selftest", {})
    q.cancel(old)
    live = q.submit("selftest", {})
    t[0] += 8 * 86400
    assert q.prune(older_than_days=7) == 1
    assert q.get(live).status == "queued"
    with pytest.raises(KeyError):
        q.get(old)


def test_queue_files_are_group_writable(tmp_path):
    """Web and workers share the queue through a group (docs/deployment.md)."""
    import os
    import stat
    old = os.umask(0o007)
    try:
        q = JobQueue(tmp_path)
        q.submit("selftest", {"action": "ok"})
    finally:
        os.umask(old)
    for suffix in ("", "-wal", "-shm"):
        f = tmp_path / ("jobs.sqlite3" + suffix)
        if f.exists():
            assert stat.S_IMODE(f.stat().st_mode) & 0o060 == 0o060, (f, oct(f.stat().st_mode))
    # An old 0640 queue is repaired by its owner.
    os.chmod(tmp_path / "jobs.sqlite3", 0o640)
    JobQueue(tmp_path)
    assert stat.S_IMODE((tmp_path / "jobs.sqlite3").stat().st_mode) & 0o060 == 0o060
