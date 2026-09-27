"""Training-site web tier (docs/training-site.md): settings, Cloudflare
Access verification, per-route roles, CSRF, headers, errors, streamed
uploads, reviewer identity, deletion, members and audit.

Tokens are signed with a throwaway RSA key and verified by the real
``CloudflareAccessVerifier``; only the key lookup is replaced.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).parent))
from pdfgen import make_pdf  # noqa: E402

from pinny.viewer import ViewerService  # noqa: E402
from pinny.viewer.auth import PUBLIC, ROLE_RANK, Authenticator, CloudflareAccessVerifier  # noqa: E402
from pinny.viewer.server import ROUTES, Site, SiteServer  # noqa: E402
from pinny.viewer.settings import ConfigError, Settings  # noqa: E402
from tests.viewer.test_viewer_api import _template_for  # noqa: E402

TEAM = "acme.cloudflareaccess.com"
AUD = "aud-tag-123"
ORIGIN = "https://pinny.test"
ADMIN = "boss@example.com"
REVIEWER = "assistant@example.com"
DRAFTSMAN = "draftsman@example.com"
OUTSIDER = "stranger@example.com"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(email=ADMIN, *, aud=AUD, iss=f"https://{TEAM}", exp_in=300, key=KEY, kid="k1", **extra):
    now = int(time.time())
    claims = {"aud": [aud], "iss": iss, "iat": now, "nbf": now, "exp": now + exp_in, "sub": "x", **extra}
    if email is not None:
        claims["email"] = email
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, key, algorithm="RS256", headers=headers)


def verifier():
    keys = {"k1": KEY.public_key()}
    return CloudflareAccessVerifier(TEAM, AUD, key_for=lambda kid: keys[kid])


def prod_settings(data_dir):
    return Settings(env="production", data_dir=Path(data_dir), origin=ORIGIN, cf_team_domain=TEAM,
                    cf_aud=AUD, admin_emails=(ADMIN,), version="test-1.0")


# --------------------------------------------------------------- settings
def test_production_refuses_to_start_without_its_settings(tmp_path):
    with pytest.raises(ConfigError) as e:
        Settings.from_env({"PINNY_ENV": "production"})
    for name in ("PINNY_DATA_DIR", "PINNY_ORIGIN", "PINNY_CF_TEAM_DOMAIN", "PINNY_CF_AUD",
                 "PINNY_ADMIN_EMAILS", "PINNY_VERSION"):
        assert name in str(e.value)
    env = {"PINNY_ENV": "production", "PINNY_DATA_DIR": str(tmp_path), "PINNY_ORIGIN": "http://x",
           "PINNY_CF_TEAM_DOMAIN": TEAM, "PINNY_CF_AUD": AUD, "PINNY_ADMIN_EMAILS": ADMIN,
           "PINNY_VERSION": "v1"}
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env(env)
    s = Settings.from_env(dict(env, PINNY_ORIGIN="https://pinny.test/",
                               PINNY_ADMIN_EMAILS=" Boss@Example.com , x@y.z"))
    assert s.production and s.origin == ORIGIN and s.admin_emails == (ADMIN, "x@y.z")
    s.check_bind("0.0.0.0")  # production may listen anywhere (cloudflared is local anyway)
    with pytest.raises(ConfigError):
        Settings.from_env({"PINNY_ENV": "staging"})


def test_development_only_listens_on_loopback(tmp_path):
    s = Settings.from_env({}, data_dir=tmp_path)
    assert not s.production
    s.check_bind("127.0.0.1")
    s.check_bind("::1")
    with pytest.raises(ConfigError, match="127.0.0.1"):
        s.check_bind("0.0.0.0")


# ------------------------------------------------------------------ tokens
def test_cloudflare_tokens_are_verified():
    v = verifier()
    assert v.email(token("Boss@Example.com")) == ADMIN
    for bad in (token(aud="other"), token(iss="https://evil.cloudflareaccess.com"), token(exp_in=-120),
                token(key=OTHER_KEY), token(email=None), token(kid=None), "not-a-jwt",
                jwt.encode({"email": ADMIN, "aud": AUD}, "s" * 32, algorithm="HS256",
                           headers={"kid": "k1"})):
        with pytest.raises(Exception) as e:
            v.email(bad)
        assert getattr(e.value, "code", None) == "unauthenticated" and e.value.status == 401


def test_every_route_declares_a_role_and_body():
    names = set()
    for method, path, name, role, body in ROUTES:
        assert role in ROLE_RANK, name
        assert method in ("GET", "POST", "DELETE"), name
        if method == "POST":
            assert body in ("json", "pdf"), name
        else:
            assert body is None, name
        if role == PUBLIC:
            assert name in ("healthz", "static"), f"{name} must not be public"
        names.add(name)
    assert len(names) == len(ROUTES)


# ------------------------------------------------------------ live server
@pytest.fixture
def site(tmp_path):
    svc = ViewerService(tmp_path)
    settings = prod_settings(tmp_path)
    s = Site(svc, settings, Authenticator(settings, _sitedb_for(settings), verifier()))
    s.sitedb.put_member(REVIEWER, "reviewer", actor=ADMIN)
    s.sitedb.put_member(DRAFTSMAN, "reviewer", actor=ADMIN)
    httpd = SiteServer(s, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, s, svc
    httpd.shutdown()
    httpd.server_close()
    svc.close()


def _sitedb_for(settings):
    from pinny.viewer.sitedb import SiteDB
    db = SiteDB(settings.data_dir)
    db.ensure_admins(settings.admin_emails)
    return db


def call(base, method, path, *, who=ADMIN, body=None, ctype=None, origin=ORIGIN, headers=None):
    h = dict(headers or {})
    if who is not None:
        h["Cf-Access-Jwt-Assertion"] = token(who)
    if origin is not None:
        h["Origin"] = origin
    data = None
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", ctype or "application/json")
    elif body is not None:
        data = body
        if ctype:
            h["Content-Type"] = ctype
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def err(raw):
    return json.loads(raw)["error"]


def upload(base, who=ADMIN, pdf=None, name="plan.pdf"):
    code, _, raw = call(base, "POST", f"/api/documents?filename={name}", who=who,
                        body=pdf or make_pdf(), ctype="application/pdf")
    assert code == 201, raw
    return json.loads(raw)


def _fill(path):
    return (path.replace("{v}", "sha256:" + "0" * 64).replace("{i:int}", "0")
            .replace("{s}", str(uuid.uuid4())).replace("{b}", str(uuid.uuid4())))


def test_authorization_matrix(site):
    base, _, _ = site
    for method, path, name, role, body in ROUTES:
        if name == "static":
            continue
        p = _fill(path)
        kw = {}
        if body == "json":
            kw = {"body": {}}
        elif body == "pdf":
            kw = {"body": b"%PDF-1.4\n%%EOF", "ctype": "application/pdf"}
        results = {who: call(base, method, p, who=who, **kw) for who in (None, OUTSIDER, REVIEWER, ADMIN)}
        if role == PUBLIC:
            assert all(r[0] == 200 for r in results.values()), name
            continue
        assert results[None][0] == 401 and err(results[None][2])["code"] == "unauthenticated", name
        assert results[OUTSIDER][0] == 403 and err(results[OUTSIDER][2])["code"] == "not_a_member", name
        if role == "admin":
            assert results[REVIEWER][0] == 403 and err(results[REVIEWER][2])["code"] == "forbidden", name
        else:
            assert results[REVIEWER][0] not in (401, 403), (name, results[REVIEWER])
        assert results[ADMIN][0] not in (401, 403), (name, results[ADMIN])


def test_csrf_origin_and_content_type(site):
    base, _, _ = site
    code, _, raw = call(base, "POST", "/api/scans", body={}, origin=None)
    assert code == 403 and err(raw)["code"] == "bad_origin"
    code, _, raw = call(base, "POST", "/api/scans", body={}, origin="https://evil.example")
    assert code == 403 and err(raw)["code"] == "bad_origin"
    code, _, raw = call(base, "POST", "/api/scans", body=b"{}", ctype="text/plain")
    assert code == 415 and err(raw)["code"] == "unsupported_media_type"
    code, _, raw = call(base, "POST", "/api/documents?filename=a.pdf", body=make_pdf(),
                        ctype="application/octet-stream")
    assert code == 415
    code, _, raw = call(base, "DELETE", "/api/documents/sha256:" + "0" * 64, origin=None)
    assert code == 403 and err(raw)["code"] == "bad_origin"


def test_security_headers_and_request_ids(site):
    base, _, _ = site
    for path, who in (("/api/me", ADMIN), ("/", None), ("/app.js", None), ("/api/nope", ADMIN),
                      ("/api/me", None)):
        code, h, _ = call(base, "GET", path, who=who)
        low = {k.lower(): v for k, v in h.items()}
        assert "frame-ancestors 'none'" in low["content-security-policy"], path
        assert low["x-frame-options"] == "DENY" and low["x-content-type-options"] == "nosniff"
        assert low["referrer-policy"] == "no-referrer"
        assert low["strict-transport-security"].startswith("max-age=")
        assert len(low["x-request-id"]) == 16
        assert "server" not in low
        if path.startswith("/api/"):
            assert low["cache-control"] == "no-store"
    code, _, raw = call(base, "GET", "/healthz", who=None)
    assert code == 200 and json.loads(raw) == {"ok": True, "version": "test-1.0"}


def test_internal_errors_do_not_leak(site):
    base, _, svc = site

    def boom():
        raise RuntimeError("secret detail at /srv/pinny/data")
    svc.documents = boom
    code, h, raw = call(base, "GET", "/api/documents")
    e = err(raw)
    assert code == 500 and e["code"] == "internal_error" and "secret" not in e["message"]
    assert e["request_id"] == {k.lower(): v for k, v in h.items()}["x-request-id"]
    assert e["request_id"] in e["message"]


def test_uploads_stream_with_a_limit(site, tmp_path):
    base, _, svc = site
    svc.render.max_upload_bytes = 2000
    code, _, raw = call(base, "POST", "/api/documents?filename=big.pdf", body=b"%PDF-" + b"x" * 5000,
                        ctype="application/pdf")
    assert code == 413 and err(raw)["code"] == "upload_too_large"
    assert list((tmp_path / "tmp").iterdir()) == []  # staging cleaned up
    svc.render.max_upload_bytes = 10 * 1024 * 1024
    assert upload(base)["page_count"] == 1
    assert list((tmp_path / "tmp").iterdir()) == []


def test_me_and_reviewer_identity(site):
    base, s, svc = site
    code, _, raw = call(base, "GET", "/api/me", who=REVIEWER)
    assert json.loads(raw) == {"email": REVIEWER, "role": "reviewer", "env": "production",
                               "sign_out_url": "/cdn-cgi/access/logout", "version": "test-1.0"}
    doc = upload(base, who=REVIEWER)
    assert doc["uploaded_by"] == REVIEWER
    v = doc["document_version"]
    body = {"document_version": v, "page_index": 0, "request_id": str(uuid.uuid4()),
            "template_box": _template_for(doc, svc.frame(v, 0))}
    code, _, raw = call(base, "POST", "/api/scans", who=DRAFTSMAN, body=body)
    assert code == 201, raw
    st = json.loads(raw)
    pin = st["pins"][0]["pin_id"]
    code, _, raw = call(base, "POST", f"/api/scans/{st['scan_id']}/actions", who=REVIEWER,
                        body={"action": "approve", "pin_id": pin, "request_id": str(uuid.uuid4())})
    assert code == 200, raw
    state = svc._db(svc.store.load_scan, st["scan_id"])
    assert state.events[0].reviewer == REVIEWER
    assert state.scan.metadata["requested_by"] == DRAFTSMAN
    code, _, raw = call(base, "POST", "/api/batches", who=DRAFTSMAN,
                        body=dict(body, request_id=str(uuid.uuid4()), template_page_index=0))
    assert code == 202, raw
    b = svc.wait_batch(json.loads(raw)["batch_id"], timeout=60)
    assert svc._db(svc.store.get_batch, b["batch_id"]).metadata["requested_by"] == DRAFTSMAN
    code, _, raw = call(base, "GET", "/api/audit")
    actions = [(e["actor"], e["action"]) for e in json.loads(raw)["events"]]
    assert (REVIEWER, "document_uploaded") in actions and (DRAFTSMAN, "batch_started") in actions


def test_delete_document(site, tmp_path):
    base, s, svc = site
    doc = upload(base, who=REVIEWER)
    v = doc["document_version"]
    body = {"document_version": v, "page_index": 0, "request_id": str(uuid.uuid4()),
            "template_box": _template_for(doc, svc.frame(v, 0))}
    st = json.loads(call(base, "POST", "/api/scans", who=REVIEWER, body=body)[2])
    call(base, "POST", f"/api/scans/{st['scan_id']}/actions", who=REVIEWER,
         body={"action": "approve", "pin_id": st["pins"][0]["pin_id"], "request_id": str(uuid.uuid4())})
    assert svc._db(svc.store.crop_status_counts) == {"written": 1}
    crop_files = list((tmp_path / "crops").rglob("*.png"))
    assert len(crop_files) == 1

    code, _, raw = call(base, "DELETE", f"/api/documents/{v}", who=DRAFTSMAN)
    assert code == 403 and err(raw)["code"] == "forbidden"  # not the uploader, not an admin
    code, _, raw = call(base, "DELETE", f"/api/documents/{v}", who=REVIEWER)
    assert code == 200 and json.loads(raw)["crops_deleted"] == 1
    assert not (tmp_path / "documents" / v[7:]).exists()
    assert list((tmp_path / "crops").rglob("*.png")) == []
    assert call(base, "GET", f"/api/documents/{v}/pages/0/frame")[0] == 404
    assert call(base, "GET", f"/api/documents/{v}/pages/0/raster.png")[0] == 404
    assert json.loads(call(base, "GET", "/api/documents")[2])["documents"] == []
    assert call(base, "DELETE", f"/api/documents/{v}")[0] == 404
    # Labels stay; crops are never cut again.
    assert svc._db(svc.store.load_scan, st["scan_id"]).events[0].action == "approve"
    assert svc._db(svc.store.process_pending_crops)["written"] == 0
    assert list((tmp_path / "crops").rglob("*.png")) == []
    # Admin can delete anyone's upload; the same bytes can come back.
    doc2 = upload(base, who=DRAFTSMAN)
    assert doc2["document_version"] == v and doc2["uploaded_by"] == DRAFTSMAN
    assert call(base, "DELETE", f"/api/documents/{v}", who=ADMIN)[0] == 200
    actions = [e["action"] for e in json.loads(call(base, "GET", "/api/audit")[2])["events"]]
    assert actions.count("document_deleted") == 2


def test_members(site):
    base, _, _ = site
    code, _, raw = call(base, "GET", "/api/members")
    assert {m["email"]: m["role"] for m in json.loads(raw)["members"]} == {
        ADMIN: "admin", REVIEWER: "reviewer", DRAFTSMAN: "reviewer"}
    assert call(base, "GET", "/api/me", who=OUTSIDER)[0] == 403
    code, _, raw = call(base, "POST", "/api/members", body={"email": "Stranger@Example.com", "role": "reviewer"})
    assert code == 200 and json.loads(raw)["email"] == OUTSIDER
    assert call(base, "GET", "/api/me", who=OUTSIDER)[0] == 200
    code, _, raw = call(base, "POST", "/api/members", body={"email": "nope", "role": "reviewer"})
    assert code == 400 and err(raw)["code"] == "invalid_member"
    code, _, raw = call(base, "POST", "/api/members", body={"email": OUTSIDER, "role": "owner"})
    assert code == 400
    code, _, raw = call(base, "POST", "/api/members/remove", body={"email": ADMIN})
    assert code == 400 and "yourself" in err(raw)["message"]
    code, _, raw = call(base, "POST", "/api/members/remove", body={"email": OUTSIDER})
    assert code == 200
    assert call(base, "GET", "/api/me", who=OUTSIDER)[0] == 403
    assert call(base, "POST", "/api/members/remove", body={"email": OUTSIDER})[0] == 404
    # The last admin cannot be demoted.
    code, _, raw = call(base, "POST", "/api/members", body={"email": ADMIN, "role": "reviewer"})
    assert code == 400 and "last admin" in err(raw)["message"]
    events = [e["action"] for e in json.loads(call(base, "GET", "/api/audit?limit=5")[2])["events"]]
    assert events[:2] == ["member_removed", "member_added"]


def test_development_mode_is_the_local_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_REVIEWER", "tester")
    svc = ViewerService(tmp_path)
    settings = Settings.from_env({"PINNY_REVIEWER": "tester"}, data_dir=tmp_path)
    s = Site(svc, settings)
    httpd = SiteServer(s, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, h, raw = call(base, "GET", "/api/me", who=None, origin=None)
        assert code == 200 and json.loads(raw)["role"] == "admin" and json.loads(raw)["sign_out_url"] is None
        assert "strict-transport-security" not in {k.lower() for k in h}
        # CSRF still applies locally: the page's own origin only.
        assert call(base, "POST", "/api/scans", who=None, body={}, origin=None)[0] == 403
        assert call(base, "POST", "/api/scans", who=None, body={}, origin=base)[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
        svc.close()
