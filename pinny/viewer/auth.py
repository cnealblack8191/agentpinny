"""Who is making a request (docs/training-site.md section 1).

Production: Cloudflare Access signs in the user and adds a JWT in the
``Cf-Access-Jwt-Assertion`` header. We verify its signature, audience,
issuer and expiry on every request, then look the email up in the members
table. Development: every request is the local user (an admin), and the
server refuses to listen anywhere but loopback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

import jwt

from .errors import ViewerError
from .settings import Settings
from .sitedb import ADMIN, REVIEWER, SiteDB

CF_HEADER = "cf-access-jwt-assertion"
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


class CloudflareAccessVerifier:
    """Verifies Cloudflare Access JWTs. ``key_for(kid)`` returns the public
    key for a key id; by default the team's JWKS, fetched and cached (an
    unknown ``kid`` refetches once). Tests pass their own."""

    def __init__(self, team_domain: str, audience: str,
                 key_for: Optional[Callable[[str], object]] = None) -> None:
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        if key_for is None:
            client = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True,
                                     lifespan=3600, timeout=10)
            key_for = lambda kid: client.get_signing_key(kid).key  # noqa: E731
        self._key_for = key_for

    def email(self, token: str) -> str:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            if not kid:
                raise jwt.InvalidTokenError("no kid")
            claims = jwt.decode(token, self._key_for(kid), algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, leeway=LEEWAY_S,
                                options={"require": ["exp", "iat", "iss", "aud"]})
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise ViewerError("unauthenticated", "Your sign-in could not be verified. Sign in again.",
                              401) from exc
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise ViewerError("unauthenticated", "Your sign-in has no email address.", 401)
        return email.strip().lower()


class Authenticator:
    def __init__(self, settings: Settings, sitedb: SiteDB,
                 verifier: Optional[CloudflareAccessVerifier] = None) -> None:
        self.settings = settings
        self.sitedb = sitedb
        if settings.production:
            self.verifier = verifier or CloudflareAccessVerifier(settings.cf_team_domain, settings.cf_aud)
        else:
            self.verifier = verifier  # tests may exercise real verification in development

    def identify(self, headers: Mapping[str, str]) -> Identity:
        """The signed-in member, or a 401/403 ``ViewerError``."""
        if self.verifier is None:
            return Identity(self.settings.dev_email, ADMIN)
        token = headers.get(CF_HEADER)
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
        return "/cdn-cgi/access/logout" if self.verifier is not None else None
