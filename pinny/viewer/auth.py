"""Who is making a request (docs/training-site.md section 1).

Production: Pinny's own sign-in. A member signs in with their email and
password and gets a session cookie (``HttpOnly``, ``Secure``,
``SameSite=Lax``). Only a hash of the session token is stored. Every
request looks the session up, then the member, so removing a member or
resetting their password takes effect at once.

Development: every request is the local user (an admin), and the server
refuses to listen anywhere but loopback.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Callable, Deque, Dict, Mapping, Optional

from . import passwords
from .errors import ViewerError
from .settings import Settings
from .sitedb import ADMIN, REVIEWER, SESSION_MAX_S, SiteDB

# Minimum role per route: public < reviewer < admin.
PUBLIC = "public"
ROLE_RANK = {PUBLIC: 0, REVIEWER: 1, ADMIN: 2}

# Wrong passwords, counted per client address (across all emails) and per
# email *and* address. Both are in memory and keyed by the address, so an
# attacker elsewhere cannot lock a member out, and a blocked attempt gets the
# same answer whether or not the email has an account.
IP_FAILURES = 30
IP_WINDOW_S = 15 * 60
PAIR_FAILURES = 10  # wrong passwords for one email from one address, per window

_BAD_LOGIN = "That email and password don't match. Try again, or ask an admin for a new set-password link."


@dataclass(frozen=True)
class Identity:
    email: str
    role: str
    session: Optional[str] = None  # the session token, when signed in with a cookie

    def allows(self, needed: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[needed]


class _FailureWindow:
    """Recent failed sign-ins per client address, in memory."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> Deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and q[0] < now - self.window_s:
            q.popleft()
        return q

    def blocked(self, key: str, now: float) -> bool:
        with self._lock:
            return len(self._recent(key, now)) >= self.limit

    def hit(self, key: str, now: float) -> None:
        with self._lock:
            self._recent(key, now).append(now)
            if len(self._hits) > 10000:  # bound memory under a spray of addresses
                for k in [k for k, q in self._hits.items() if not q]:
                    del self._hits[k]


class Authenticator:
    def __init__(self, settings: Settings, sitedb: SiteDB, *, login: Optional[bool] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.sitedb = sitedb
        # Tests may turn sign-in on in development; production always has it.
        self.enabled = settings.production if login is None else (login or settings.production)
        self.clock = clock
        self._ip_failures = _FailureWindow(IP_FAILURES, IP_WINDOW_S)
        self._pair_failures = _FailureWindow(PAIR_FAILURES, IP_WINDOW_S)

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix makes browsers refuse the cookie unless it is
        # Secure, host-only and Path=/, so no subdomain can plant one.
        return "__Host-pinny_session" if (self.settings.origin or "").startswith("https://") else "pinny_session"

    def cookie_attrs(self) -> dict:
        return {"path": "/", "httponly": True, "samesite": "lax", "secure": self.cookie_name.startswith("__Host-")}

    def session_token(self, headers: Mapping[str, str]) -> Optional[str]:
        raw = headers.get("cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
        except CookieError:
            return None
        m = c.get(self.cookie_name)
        return m.value if m is not None and 0 < len(m.value) <= 128 else None

    def identify(self, headers: Mapping[str, str]) -> Identity:
        """The signed-in member, or a 401/403 ``ViewerError``."""
        if not self.enabled:
            return Identity(self.settings.dev_email, ADMIN)
        token = self.session_token(headers)
        email = self.sitedb.session_email(token, self.clock()) if token else None
        if email is None:
            raise ViewerError("unauthenticated", "Sign in to use Pinny.", 401)
        member = self.sitedb.member(email)
        if member is None:  # removal deletes sessions; this covers a race with it
            raise ViewerError("not_a_member", f"{email} is no longer a Pinny member. Ask an admin.", 403)
        return Identity(member.email, member.role, token)

    # ----------------------------------------------------------- sign-in
    def login(self, email, password, client: str) -> Identity:
        """Check a password and start a session. Every failure gives the same
        answer, and an unknown email costs as much time as a wrong password."""
        now = self.clock()
        if not isinstance(email, str) or not isinstance(password, str) or len(password) > passwords.MAX_LENGTH:
            raise ViewerError("bad_login", _BAD_LOGIN, 401)
        email = email.strip().lower()
        pair = f"{email}\n{client}"
        if self._ip_failures.blocked(client, now) or self._pair_failures.blocked(pair, now):
            raise ViewerError("too_many_attempts",
                              "Too many wrong passwords. Wait 15 minutes and try again, or ask an admin "
                              "for a new set-password link.", 429)
        rec = self.sitedb.password_record(email) if self.sitedb.member(email) else None
        ok = passwords.verify_password(password, rec["hash"]) if rec else passwords.burn_time(password)
        if not ok:
            self._ip_failures.hit(client, now)
            self._pair_failures.hit(pair, now)
            if rec is not None and self._pair_failures.blocked(pair, now):
                self.sitedb.audit(None, "account_locked", email, {"client": client, "minutes": IP_WINDOW_S // 60})
            raise ViewerError("bad_login", _BAD_LOGIN, 401)
        member = self.sitedb.member(email)
        return Identity(member.email, member.role, self.sitedb.create_session(email, now))

    def setup_email(self, token) -> str:
        """The member a set-password link belongs to."""
        email = self.sitedb.setup_link_email(token, self.clock()) if isinstance(token, str) and token else None
        if email is None:
            raise ViewerError("bad_setup_link", "This set-password link has expired or was already used. "
                              "Ask an admin for a new one.", 400)
        return email

    def complete_setup(self, token, password) -> Identity:
        email = self.setup_email(token)
        try:
            passwords.check_policy(password, email)
        except passwords.WeakPassword as exc:
            raise ViewerError("weak_password", str(exc)) from None
        self.sitedb.set_password(email, passwords.hash_password(password), actor=email)
        member = self.sitedb.member(email)
        return Identity(member.email, member.role, self.sitedb.create_session(email, self.clock()))

    def change_password(self, ident: Identity, current, new) -> None:
        rec = self.sitedb.password_record(ident.email)
        if rec is None or not isinstance(current, str) or not passwords.verify_password(current, rec["hash"]):
            raise ViewerError("bad_login", "Your current password is not right.", 400)
        try:
            passwords.check_policy(new, ident.email)
        except passwords.WeakPassword as exc:
            raise ViewerError("weak_password", str(exc)) from None
        self.sitedb.set_password(ident.email, passwords.hash_password(new), actor=ident.email,
                                 keep_session=ident.session)

    def logout(self, headers: Mapping[str, str]) -> None:
        token = self.session_token(headers)
        if token:
            self.sitedb.delete_session(token)

    @property
    def session_max_age(self) -> int:
        return SESSION_MAX_S
