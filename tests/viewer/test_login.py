"""Pinny's own sign-in (docs/training-site.md section 1): set-password
links, password sign-in, sessions, lockout, sign-out, password changes and
resets, and the members CLI."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from pinny.viewer import ViewerService
from pinny.viewer import passwords
from pinny.viewer.auth import IP_FAILURES, PAIR_FAILURES, Authenticator
from pinny.viewer.members import main as members_main
from pinny.viewer.server import Site, SiteServer
from pinny.viewer.settings import Settings
from pinny.viewer.sitedb import SESSION_IDLE_S, SETUP_LINK_S, SiteDB

ORIGIN = "https://pinny.test"
ADMIN = "boss@example.com"
REVIEWER = "assistant@example.com"
GOOD = "correct horse battery"
COOKIE = "__Host-pinny_session"


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def settings(tmp_path):
    return Settings(env="production", data_dir=Path(tmp_path), origin=ORIGIN, admin_emails=(ADMIN,),
                    version="test-1.0")


@pytest.fixture
def site(tmp_path):
    svc = ViewerService(tmp_path)
    st = settings(tmp_path)
    db = SiteDB(st.data_dir)
    clock = Clock()
    s = Site(svc, st, Authenticator(st, db, clock=clock))
    httpd = SiteServer(s, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", s, clock
    httpd.shutdown()
    httpd.server_close()
    svc.close()


def call(base, method, path, body=None, cookie=None, origin=ORIGIN):
    h = {}
    if origin:
        h["Origin"] = origin
    if cookie:
        h["Cookie"] = f"{COOKIE}={cookie}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, json.loads(e.read() or b"null")


def session_from(headers):
    raw = headers.get("Set-Cookie", "")
    assert raw.startswith(COOKIE + "="), raw
    low = raw.lower()
    for attr in ("httponly", "secure", "samesite=lax", "path=/"):
        assert attr in low, raw
    assert "domain=" not in low
    return raw.split(";", 1)[0].split("=", 1)[1]


def token_of(url):
    assert "#token=" in url
    return url.split("#token=", 1)[1]


def first_password(base, s, clock, email=ADMIN, password=GOOD):
    tok, _ = s.sitedb.issue_setup_link(email, actor="test", now=clock())
    code, h, out = call(base, "POST", "/api/setup", {"token": tok, "password": password})
    assert code == 200, out
    return session_from(h)


# ---------------------------------------------------------------- passwords
def test_password_hashes():
    h = passwords.hash_password(GOOD)
    assert h.startswith("scrypt$") and GOOD not in h
    assert passwords.verify_password(GOOD, h)
    assert not passwords.verify_password(GOOD + "x", h)
    assert passwords.hash_password(GOOD) != h  # salted
    assert not passwords.verify_password(GOOD, "garbage")
    for bad in ("short", "a" * 20, "x" * 300, None, "boss@example.com"):
        with pytest.raises(passwords.WeakPassword):
            passwords.check_policy(bad, "boss@example.com")
    passwords.check_policy(GOOD, ADMIN)


# ------------------------------------------------------------------- flows
def test_setup_link_sign_in_and_sign_out(site):
    base, s, clock = site
    assert call(base, "GET", "/api/me")[0] == 401
    # First admin: a set-password link from the server, used once.
    tok, _ = s.sitedb.issue_setup_link(ADMIN, actor="cli", now=clock())
    code, _, out = call(base, "POST", "/api/setup/check", {"token": tok})
    assert code == 200 and out == {"email": ADMIN}
    code, _, out = call(base, "POST", "/api/setup", {"token": tok, "password": "short"})
    assert code == 400 and out["error"]["code"] == "weak_password"
    code, h, out = call(base, "POST", "/api/setup", {"token": tok, "password": GOOD})
    assert code == 200 and out == {"email": ADMIN, "role": "admin"}
    cookie = session_from(h)
    code, _, me = call(base, "GET", "/api/me", cookie=cookie)
    assert code == 200 and me["email"] == ADMIN and me["sign_in"] is True
    # The link is spent.
    assert call(base, "POST", "/api/setup", {"token": tok, "password": GOOD})[2]["error"]["code"] == "bad_setup_link"
    # Sign out ends the session on the server, not just in the browser.
    code, h, _ = call(base, "POST", "/api/logout", {}, cookie=cookie)
    assert code == 200 and "max-age=0" in h.get("Set-Cookie", "").lower()
    assert call(base, "GET", "/api/me", cookie=cookie)[0] == 401
    # Sign in again with the password.
    code, h, out = call(base, "POST", "/api/login", {"email": " Boss@Example.com ", "password": GOOD})
    assert code == 200 and out["email"] == ADMIN
    assert call(base, "GET", "/api/me", cookie=session_from(h))[0] == 200
    # Only a hash of the session token is stored.
    raw = (Path(s.settings.data_dir) / "site.sqlite3").read_bytes()
    assert session_from(h).encode() not in raw and tok.encode() not in raw


def test_wrong_passwords_look_the_same_and_lock_the_account(site):
    base, s, clock = site
    first_password(base, s, clock)
    s.sitedb.put_member(REVIEWER, "reviewer", actor=ADMIN)  # member without a password yet
    answers = [call(base, "POST", "/api/login", {"email": e, "password": p})
               for e, p in ((ADMIN, "wrong password!"), ("nobody@example.com", GOOD), (REVIEWER, GOOD))]
    assert {(c, o["error"]["code"], o["error"]["message"]) for c, _, o in answers} == {
        (401, "bad_login", answers[0][2]["error"]["message"])}
    for _ in range(PAIR_FAILURES - 1):
        call(base, "POST", "/api/login", {"email": ADMIN, "password": "wrong password!"})
    code, _, out = call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD})
    assert code == 429 and out["error"]["code"] == "too_many_attempts"
    # An email with no account locks the same way, so a lock reveals nothing.
    for _ in range(PAIR_FAILURES):
        call(base, "POST", "/api/login", {"email": "nobody@example.com", "password": "wrong password!"})
    code2, _, out2 = call(base, "POST", "/api/login", {"email": "nobody@example.com", "password": GOOD})
    assert (code2, out2["error"]) == (code, dict(out["error"], request_id=out2["error"]["request_id"]))
    clock.t += 16 * 60
    assert call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD})[0] == 200
    assert any(e["action"] == "account_locked" for e in s.sitedb.audit_log())


def test_an_attacker_elsewhere_cannot_lock_a_member_out(tmp_path):
    """Found by review: failures from one address locked the account for
    everyone, so an attacker could keep all members locked out."""
    import time as _t
    st = settings(tmp_path)
    db = SiteDB(st.data_dir)
    db.ensure_admins((ADMIN,))
    auth = Authenticator(st, db)
    tok, _ = db.issue_setup_link(ADMIN, actor="t", now=_t.time())
    auth.complete_setup(tok, GOOD)
    for _ in range(PAIR_FAILURES * 3):
        with pytest.raises(Exception):
            auth.login(ADMIN, "wrong password!", "203.0.113.9")
    assert auth.login(ADMIN, GOOD, "198.51.100.7").email == ADMIN  # the member, from their own network


def test_too_many_failures_from_one_address(site):
    base, s, clock = site
    first_password(base, s, clock)
    s.auth._ip_failures.limit = 5  # keep the test fast; production uses IP_FAILURES
    assert IP_FAILURES >= PAIR_FAILURES
    for i in range(5):
        call(base, "POST", "/api/login", {"email": f"guess{i}@example.com", "password": "x" * 12})
    code, _, out = call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD})
    assert code == 429 and out["error"]["code"] == "too_many_attempts"


def test_login_needs_the_sites_origin(site):
    base, s, clock = site
    first_password(base, s, clock)
    code, _, out = call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD}, origin="https://evil.test")
    assert code == 403 and out["error"]["code"] == "bad_origin"


def test_sessions_expire_when_idle(site):
    base, s, clock = site
    cookie = first_password(base, s, clock)
    clock.t += SESSION_IDLE_S - 3600
    assert call(base, "GET", "/api/me", cookie=cookie)[0] == 200  # use keeps it alive
    clock.t += SESSION_IDLE_S - 3600
    assert call(base, "GET", "/api/me", cookie=cookie)[0] == 200
    clock.t += SESSION_IDLE_S + 1
    assert call(base, "GET", "/api/me", cookie=cookie)[0] == 401


def test_setup_links_expire(site):
    base, s, clock = site
    tok, _ = s.sitedb.issue_setup_link(ADMIN, actor="cli", now=clock())
    clock.t += SETUP_LINK_S + 1
    assert call(base, "POST", "/api/setup/check", {"token": tok})[0] == 400


def test_change_password_signs_out_other_devices(site):
    base, s, clock = site
    laptop = first_password(base, s, clock)
    _, h, _ = call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD})
    phone = session_from(h)
    code, _, out = call(base, "POST", "/api/me/password",
                        {"current_password": "not it at all", "new_password": "a new long password"}, cookie=laptop)
    assert code == 400
    code, _, out = call(base, "POST", "/api/me/password",
                        {"current_password": GOOD, "new_password": "a new long password"}, cookie=laptop)
    assert code == 200, out
    assert call(base, "GET", "/api/me", cookie=laptop)[0] == 200
    assert call(base, "GET", "/api/me", cookie=phone)[0] == 401
    assert call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD})[0] == 401
    assert call(base, "POST", "/api/login", {"email": ADMIN, "password": "a new long password"})[0] == 200


def test_admin_reset_and_member_removal(site):
    base, s, clock = site
    admin = first_password(base, s, clock)
    code, _, out = call(base, "POST", "/api/members", {"email": REVIEWER, "role": "reviewer"}, cookie=admin)
    assert code == 200 and out["has_password"] is False
    rev = first_password(base, s, clock, email=REVIEWER, password="reviewer password")
    # Reviewers cannot hand out links.
    code, _, out = call(base, "POST", "/api/members/setup-link", {"email": ADMIN, "reset": True}, cookie=rev)
    assert code == 403
    # Admin resets the reviewer: old password and sessions stop working.
    code, _, out = call(base, "POST", "/api/members/setup-link", {"email": REVIEWER, "reset": True}, cookie=admin)
    assert code == 200 and out["setup_url"].startswith(ORIGIN + "/setup.html#token=")
    assert call(base, "GET", "/api/me", cookie=rev)[0] == 401
    assert call(base, "POST", "/api/login", {"email": REVIEWER, "password": "reviewer password"})[0] == 401
    code, h, _ = call(base, "POST", "/api/setup", {"token": token_of(out["setup_url"]),
                                                   "password": "second reviewer password"})
    rev = session_from(h)
    # Removing a member ends their sessions at once.
    assert call(base, "POST", "/api/members/remove", {"email": REVIEWER}, cookie=admin)[0] == 200
    assert call(base, "GET", "/api/me", cookie=rev)[0] == 401
    code, _, out = call(base, "POST", "/api/members/setup-link", {"email": REVIEWER}, cookie=admin)
    assert code == 400 and out["error"]["code"] == "invalid_member"
    # An admin cannot reset themself out of the site.
    code, _, out = call(base, "POST", "/api/members/setup-link", {"email": ADMIN, "reset": True}, cookie=admin)
    assert code == 400


def test_development_has_no_sign_in_routes(tmp_path):
    svc = ViewerService(tmp_path)
    s = Site(svc, Settings.from_env({}, data_dir=tmp_path))
    httpd = SiteServer(s, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, _, out = call(base, "POST", "/api/login", {"email": ADMIN, "password": GOOD}, origin=base)
        assert code == 404 and out["error"]["code"] == "no_sign_in"
    finally:
        httpd.shutdown()
        httpd.server_close()
        svc.close()


def test_members_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PINNY_ORIGIN", ORIGIN)
    monkeypatch.setenv("PINNY_ADMIN_EMAILS", ADMIN)
    d = str(tmp_path)
    assert members_main(["--data-dir", d, "setup-link", ADMIN]) == 0
    url = capsys.readouterr().out.strip().splitlines()[-1]
    db = SiteDB(tmp_path)
    assert db.setup_link_email(token_of(url), time.time()) == ADMIN
    assert url.startswith(ORIGIN + "/setup.html#token=")
    assert members_main(["--data-dir", d, "add", REVIEWER]) == 0
    assert members_main(["--data-dir", d, "setup-link", "nobody@example.com"]) == 2
    db.set_password(ADMIN, passwords.hash_password(GOOD), actor="t")
    db.create_session(ADMIN, time.time())
    assert members_main(["--data-dir", d, "reset", ADMIN]) == 0
    assert not db.has_password(ADMIN)
    assert members_main(["--data-dir", d, "sign-out-all"]) == 0
    capsys.readouterr()
    assert members_main(["--data-dir", d, "list"]) == 0
    assert "no password yet" in capsys.readouterr().out
