"""Password hashing for Pinny's own sign-in (docs/training-site.md section 1).

scrypt from the standard library, with a random 16-byte salt per password.
Stored as ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>`` so the cost can be
raised later without breaking existing hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading

N, R, P = 2 ** 15, 8, 1  # about 32 MB and ~0.1 s per hash
_DKLEN = 32
_MAXMEM = 128 * 1024 * 1024

# Each hash takes ~32 MB, so a burst of sign-in attempts could otherwise
# use a web thread's worth of memory each.
_CONCURRENT = threading.BoundedSemaphore(4)

MIN_LENGTH = 12
MAX_LENGTH = 256  # hashing cost is bounded, but refuse absurd bodies anyway


class WeakPassword(ValueError):
    """The new password does not meet the policy."""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _CONCURRENT:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_DKLEN)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_scrypt(password, salt, N, R, P))}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# Verified against when the email is unknown, so a wrong email takes as long
# as a wrong password and does not reveal who has an account.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def burn_time(password: str) -> None:
    verify_password(password, _DUMMY)


def check_policy(password, email: str = "") -> str:
    if not isinstance(password, str):
        raise WeakPassword("Enter a password.")
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"Use at most {MAX_LENGTH} characters.")
    if email and password.strip().lower() in (email.lower(), email.split("@")[0].lower()):
        raise WeakPassword("Don't use your email address as your password.")
    if len(set(password)) < 4:
        raise WeakPassword("That password is too easy to guess.")
    return password
