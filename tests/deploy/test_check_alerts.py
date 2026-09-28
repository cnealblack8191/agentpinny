"""deploy/bin/pinny-check-alerts: what it detects and how it rate-limits."""

from __future__ import annotations

import json
import time
from pathlib import Path

from pinny.jobs.queue import JobQueue
from tests.deploy.scripts import load

ca = load("pinny-check-alerts")


def caddy_line(status: int, uri: str = "/api/scans") -> str:
    return json.dumps({"level": "info", "status": status, "request": {"method": "POST", "uri": uri}}) + "\n"


def test_5xx_counts_only_new_lines_and_survives_log_rotation(tmp_path):
    log = tmp_path / "pinny-production.log"
    log.write_text(caddy_line(500) * 3)
    state: dict = {}
    assert ca.check_5xx("production", log, state) == []  # first run starts at the end
    with log.open("a") as f:
        f.write(caddy_line(200) + caddy_line(502, "/api/documents?filename=a.pdf") + caddy_line(404))
    alerts = ca.check_5xx("production", log, state)
    assert len(alerts) == 1 and alerts[0].event
    assert "1 server error" in alerts[0].message and "502 POST /api/documents" in alerts[0].message
    assert "filename" not in alerts[0].message  # no query strings in emails
    assert ca.check_5xx("production", log, state) == []
    log.unlink()  # Caddy rolled the file: a new inode starts at 0
    log.write_text(caddy_line(503))
    assert len(ca.check_5xx("production", log, state)) == 1


def test_failed_jobs_server_side_only(tmp_path):
    q = JobQueue(tmp_path)
    since = time.time() - 1
    for code, status in (("job_crashed", 500), ("timeout", 504), ("invalid_template_box", 400)):
        job_id = q.submit("selftest", {"action": "ok"})
        job = q.claim("interactive", "w1")
        assert job.job_id == job_id
        q.fail(job_id, "w1", code, "x", status)
    alerts = ca.check_jobs("production", tmp_path / "jobs.sqlite3", since)
    assert len(alerts) == 1
    assert "2 job(s) failed" in alerts[0].message and "invalid_template_box" not in alerts[0].message
    assert ca.check_jobs("production", tmp_path / "jobs.sqlite3", time.time() + 5) == []


def test_disk_threshold(tmp_path):
    assert ca.check_disk([str(tmp_path)], limit=100.0) == []
    alerts = ca.check_disk([str(tmp_path), str(tmp_path)], limit=-1)
    assert len(alerts) == 1  # one filesystem, reported once


def test_health_local_then_public():
    calls = []

    def fake_get(url, host=None):
        calls.append((url, host))
        return (200, {"ok": True}) if url.startswith("http://127.0.0.1") else (0, None)

    env = {"PINNY_PORT": "8001", "PINNY_ORIGIN": "https://pinny.ecinc.us"}
    alerts = ca.check_health("production", env, fake_get)
    assert calls[0] == ("http://127.0.0.1:8001/healthz", "pinny.ecinc.us")
    assert [a.key for a in alerts] == ["health:production:public"]
    down = ca.check_health("production", env, lambda url, host=None: (0, None))
    assert [a.key for a in down] == ["health:production:local"]  # no second alert about the public URL


def test_units_enabled_but_not_running():
    def fake(*args):
        verb, _, unit = args
        if verb == "is-enabled":
            return 0 if "train" not in unit else 1
        return 0 if "web" in unit else 3

    keys = [a.key for a in ca.check_units("staging", fake)]
    assert keys == ["unit:pinny-worker-interactive@staging.service", "unit:pinny-worker-scan@staging.service"]


def test_backup_age(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "uptime_s", lambda: 1e6)
    monkeypatch.setattr(ca, "unit_state", lambda unit: "inactive")
    status = tmp_path / "status.json"
    now = time.time()
    enabled = lambda *a: 0 if a[0] == "is-enabled" else 3  # noqa: E731
    status.write_text(json.dumps({"last_success": "2020-01-01T00:00:00+00:00"}))
    assert [a.key for a in ca.check_backup("production", status, {}, now, enabled)] == ["backup:production"]
    import datetime as dt
    recent = dt.datetime.fromtimestamp(now - 70 * 3600, dt.timezone.utc).isoformat()  # Friday -> Monday
    status.write_text(json.dumps({"last_success": recent}))
    assert ca.check_backup("production", status, {}, now, enabled) == []
    monkeypatch.setattr(ca, "uptime_s", lambda: 60)  # just booted: a missed run may be catching up
    status.write_text(json.dumps({"last_success": "2020-01-01T00:00:00+00:00"}))
    assert ca.check_backup("production", status, {}, now, enabled) == []


def test_rate_limit_and_resolve():
    state: dict = {}
    t0 = 1_000_000.0
    disk = ca.Alert("disk:/", "Disk / is 91% full.")
    errors = ca.Alert("5xx:production", "production: 3 server errors", event=True)
    send, resolved = ca.decide([disk, errors], state, t0)
    assert len(send) == 2 and not resolved
    send, resolved = ca.decide([disk, errors], state, t0 + 300)  # 5 minutes later: quiet
    assert send == [] and resolved == []
    send, _ = ca.decide([disk, errors], state, t0 + ca.EVENT_REPEAT_S + 1)
    assert len(send) == 1 and "and 1 similar" in send[0]
    send, _ = ca.decide([disk], state, t0 + ca.REPEAT_S + 1)
    assert send == ["STILL: Disk / is 91% full."]
    send, resolved = ca.decide([], state, t0 + ca.REPEAT_S + 400)
    assert send == [] and resolved == ["Resolved: disk:/"]
    subject, body = ca.compose(["Disk / is 91% full."], [], "ip-10-0-0-5")
    assert len(subject) < 100 and "docs/deployment.md" in body
    assert ca.compose([], [], "h") is None


def test_run_checks_skips_profiles_not_deployed(tmp_path):
    state: dict = {}
    alerts = ca.run_checks(state, time.time(), profiles=["production"], etc=tmp_path, opt=tmp_path,
                           srv=tmp_path, caddy_logs=tmp_path, backup_state=tmp_path,
                           get=lambda *a, **k: (0, None), unit_run=lambda *a: 1)
    assert [a for a in alerts if not a.key.startswith("disk:")] == []
