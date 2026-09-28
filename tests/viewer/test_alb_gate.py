"""AWS Application Load Balancer + Cognito gate (docs/training-site.md section 1).

Tokens are built the way the ALB builds ``x-amzn-oidc-data``: ES256, with
``kid`` and ``signer`` in the header and base64url segments that may keep
their ``=`` padding. They are signed with a throwaway P-256 key; only the
public-key lookup is replaced.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from pinny.viewer import ViewerService
from pinny.viewer.auth import ALB_HEADER, AlbOidcVerifier, Authenticator
from pinny.viewer.server import Site, SiteServer
from pinny.viewer.settings import ConfigError, Settings
from pinny.viewer.sitedb import SiteDB

ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/pinny-alb/50dc6c495c0c9188"
OTHER_ARN = ARN.replace("pinny-alb", "evil-alb")
ISS = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEfGhI"
KID = "7b0f6d4e-1a2b-4c3d-9e8f-001122334455"
KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def _b64(data: bytes, pad: bool) -> str:
    s = base64.urlsafe_b64encode(data).decode()
    return s if pad else s.rstrip("=")


def alb_token(email="Charles@EcInc.us", *, signer=ARN, kid=KID, alg="ES256", exp_in=120, iss=ISS,
              key=KEY, pad=True) -> str:
    now = int(time.time())
    header = {"alg": alg, "kid": kid, "signer": signer, "iss": iss, "client": "client-id", "exp": now + exp_in}
    payload = {"sub": "abc", "exp": now + exp_in, "iss": iss, "username": "abc"}
    if email is not None:
        payload["email"] = email
    signing_input = f"{_b64(json.dumps(header).encode(), pad)}.{_b64(json.dumps(payload).encode(), pad)}"
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'), pad)}"


def verifier(**kw):
    calls = []

    def key_for(kid):
        calls.append(kid)
        return PEM
    v = AlbOidcVerifier(ARN, "us-east-1", ISS, key_for=key_for, **kw)
    v.calls = calls
    return v


def test_alb_tokens_are_verified():
    v = verifier()
    assert v.email(alb_token(pad=True)) == "charles@ecinc.us"
    assert v.email(alb_token(pad=False)) == "charles@ecinc.us"
    other = ec.generate_private_key(ec.SECP256R1())
    for bad in (alb_token(signer=OTHER_ARN), alb_token(exp_in=-300), alb_token(iss=ISS + "x"),
                alb_token(key=other), alb_token(email=None), alb_token(alg="HS256"), "junk"):
        with pytest.raises(Exception) as e:
            v.email(bad)
        assert getattr(e.value, "code", None) == "unauthenticated" and e.value.status == 401


def test_unsafe_key_ids_are_never_fetched():
    v = verifier()
    for kid in ("../../etc/passwd", "a/b", "x" * 200, "", "k?x=1"):
        with pytest.raises(Exception):
            v.email(alb_token(kid=kid))
    assert v.calls == []
    with pytest.raises(Exception):
        v.email(alb_token(signer=OTHER_ARN))  # the signer is checked before any key lookup
    assert v.calls == []


def test_public_keys_come_from_the_regions_alb_endpoint(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return PEM

    def fake_urlopen(url, timeout):
        seen.append((url, timeout))
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    v = AlbOidcVerifier(ARN, "us-east-1", ISS)
    assert v.email(alb_token()) == "charles@ecinc.us"
    assert v.email(alb_token()) == "charles@ecinc.us"  # cached
    assert seen == [(f"https://public-keys.auth.elb.us-east-1.amazonaws.com/{KID}", 5)]


def test_alb_settings(tmp_path):
    base = {"PINNY_ENV": "production", "PINNY_DATA_DIR": str(tmp_path), "PINNY_ORIGIN": "https://pinny.ecinc.us",
            "PINNY_ADMIN_EMAILS": "charles@ecinc.us", "PINNY_VERSION": "v1"}
    with pytest.raises(ConfigError, match="PINNY_ALB_ARN"):
        Settings.from_env(base)
    with pytest.raises(ConfigError, match="load balancer ARN"):
        Settings.from_env(dict(base, PINNY_ALB_ARN="arn:aws:s3:::bucket"))
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env(dict(base, PINNY_ALB_ARN=ARN, PINNY_SIGN_OUT_URL="http://x"))
    s = Settings.from_env(dict(base, PINNY_ALB_ARN=ARN, PINNY_OIDC_ISSUER=ISS + "/"))
    assert (s.alb_region, s.oidc_issuer) == ("us-east-1", ISS)
    a = Authenticator(s, SiteDB(tmp_path))
    assert isinstance(a.verifier, AlbOidcVerifier) and a.sign_out_url == "/logout"


@pytest.fixture
def site(tmp_path):
    svc = ViewerService(tmp_path)
    settings = Settings(env="production", data_dir=Path(tmp_path), origin="https://pinny.ecinc.us",
                        alb_arn=ARN, oidc_issuer=ISS, admin_emails=("charles@ecinc.us",),
                        sign_out_url="https://pinny.auth.us-east-1.amazoncognito.com/logout?client_id=c"
                                     "&logout_uri=https://pinny.ecinc.us/", version="t")
    db = SiteDB(tmp_path)
    db.ensure_admins(settings.admin_emails)
    s = Site(svc, settings, Authenticator(settings, db, verifier()))
    httpd = SiteServer(s, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", settings
    httpd.shutdown()
    httpd.server_close()
    svc.close()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_alb_gate_end_to_end(site):
    base, settings = site
    code, _, raw = _get(base + "/api/me", {ALB_HEADER: alb_token()})
    assert code == 200 and json.loads(raw)["email"] == "charles@ecinc.us"
    assert json.loads(raw)["sign_out_url"] == "/logout"
    assert _get(base + "/api/me")[0] == 401
    assert _get(base + "/api/me", {"Authorization": f"Bearer {alb_token()}"})[0] == 401  # only the ALB header counts
    assert _get(base + "/api/me", {ALB_HEADER: alb_token(signer=OTHER_ARN)})[0] == 401
    assert _get(base + "/api/me", {ALB_HEADER: alb_token("t.williams@ecinc.us")})[0] == 403  # not yet a member
    code, headers, _ = _get(base + "/logout")
    assert code == 302 and headers["Location"] == settings.sign_out_url
    cookies = headers.get_all("Set-Cookie")
    assert sorted(c.split("=")[0] for c in cookies) == [f"AWSELBAuthSessionCookie-{i}" for i in range(4)]
    assert all("Max-Age=0" in c and "Secure" in c and "HttpOnly" in c for c in cookies)
