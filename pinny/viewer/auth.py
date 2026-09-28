"""Who is making a request (docs/training-site.md section 1).

Production: an AWS Application Load Balancer with Amazon Cognito signs the
user in and adds a signed ``x-amzn-oidc-data`` token (ES256) to every
request. We verify it on every request (signature against the region's ALB
public key, ``signer`` pinned to our load balancer, expiry, and the Cognito
issuer when configured), then look the email up in the members table.

Development: every request is the local user (an admin), and the server
refuses to listen anywhere but loopback.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .errors import ViewerError
from .settings import Settings
from .sitedb import ADMIN, REVIEWER, SiteDB

ALB_HEADER = "x-amzn-oidc-data"
_KID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
LEEWAY_S = 60

# Minimum role per route: public < reviewer < admin.
PUBLIC = "public"
ROLE_RANK = {PUBLIC: 0, REVIEWER: 1, ADMIN: 2}


@dataclass(frozen=True)
class Identity:
    email: str
    role: str

    def allows(self, needed: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[needed]


class AlbOidcVerifier:
    """Verifies the ``x-amzn-oidc-data`` JWT an Application Load Balancer
    adds after Cognito sign-in. ``key_for(kid)`` returns the PEM public key;
    by default it is fetched once per key id from the region's ALB key
    endpoint and cached. Tests pass their own.

    The ``signer`` check is what stops a token from any other load balancer
    (or anyone who can reach the instance) from being accepted; the
    instance's security group must also admit traffic only from the ALB.
    """

    header = ALB_HEADER
    sign_out_path = "/logout"

    def __init__(self, alb_arn: str, region: str, issuer: Optional[str] = None,
                 key_for: Optional[Callable[[str], object]] = None) -> None:
        self.alb_arn = alb_arn
        self.issuer = issuer
        self._keys: Dict[str, object] = {}
        self._lock = threading.Lock()
        self._key_for = key_for or (lambda kid: self._fetch(region, kid))

    def _fetch(self, region: str, kid: str) -> bytes:
        with self._lock:
            if kid in self._keys:
                return self._keys[kid]
        url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - fixed AWS host
            pem = r.read(16384)
        with self._lock:
            self._keys[kid] = pem
        return pem

    def email(self, token: str) -> str:
        # Verified by hand: the ALB keeps "=" padding in its base64url
        # segments, which PyJWT rejects, and the signature covers the
        # segments exactly as sent.
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("not a JWT")
            head = json.loads(_b64decode(parts[0]))
            kid = head.get("kid")
            if head.get("alg") != "ES256" or not isinstance(kid, str) or not _KID_RE.match(kid):
                raise ValueError("unexpected header")
            if head.get("signer") != self.alb_arn:
                raise ValueError("signed by another load balancer")
            sig = _b64decode(parts[2])
            if len(sig) != 64:
                raise ValueError("bad signature length")
            key = self._public_key(kid)
            key.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                       f"{parts[0]}.{parts[1]}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
            claims = json.loads(_b64decode(parts[1]))
            if not isinstance(claims, dict):
                raise ValueError("bad claims")
            exp = claims.get("exp", head.get("exp"))
            if isinstance(exp, bool) or not isinstance(exp, (int, float)) or time.time() > exp + LEEWAY_S:
                raise ValueError("expired")
            if self.issuer and claims.get("iss", head.get("iss")) != self.issuer:
                raise ValueError("wrong issuer")
        except (InvalidSignature, KeyError, ValueError, TypeError, UnicodeError, OSError) as exc:
            raise ViewerError("unauthenticated", "Your sign-in could not be verified. Sign in again.",
                              401) from exc
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise ViewerError("unauthenticated", "Your sign-in has no email address.", 401)
        return email.strip().lower()

    def _public_key(self, kid: str):
        key = serialization.load_pem_public_key(self._key_for(kid))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("unexpected key type")
        return key


def _b64decode(segment: str) -> bytes:
    """base64url with or without padding."""
    if not isinstance(segment, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", segment):
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class Authenticator:
    def __init__(self, settings: Settings, sitedb: SiteDB,
                 verifier=None) -> None:
        self.settings = settings
        self.sitedb = sitedb
        if settings.production:
            self.verifier = verifier or AlbOidcVerifier(settings.alb_arn, settings.alb_region,
                                                        settings.oidc_issuer)
        else:
            self.verifier = verifier  # tests may exercise real verification in development

    def identify(self, headers: Mapping[str, str]) -> Identity:
        """The signed-in member, or a 401/403 ``ViewerError``."""
        if self.verifier is None:
            return Identity(self.settings.dev_email, ADMIN)
        token = headers.get(self.verifier.header)
        if not token:
            raise ViewerError("unauthenticated", "Sign in to use Pinny.", 401)
        email = self.verifier.email(token)
        member = self.sitedb.member(email)
        if member is None:
            raise ViewerError("not_a_member",
                              f"{email} is signed in but is not a Pinny member. Ask an admin to add you.",
                              403)
        return Identity(member.email, member.role)

    @property
    def sign_out_url(self) -> Optional[str]:
        return self.verifier.sign_out_path if self.verifier is not None else None
