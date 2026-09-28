"""Site configuration from the environment (docs/training-site.md section 10).

Production refuses to start unless every setting it needs is present, so a
missing variable fails at boot, not on the first request.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple

from pinny.learning.store import default_data_dir as _store_default_data_dir

DEVELOPMENT = "development"
PRODUCTION = "production"

# Where PDF work runs (docs/training-site.md section 4).
JOBS_INPROCESS = "inprocess"  # development and tests only: no sandbox
JOBS_SANDBOX = "sandbox"  # worker threads in the web process, each job in a sandboxed child
JOBS_EXTERNAL = "external"  # separate `python -m pinny.jobs.worker` services
JOBS_MODES = (JOBS_INPROCESS, JOBS_SANDBOX, JOBS_EXTERNAL)



class ConfigError(ValueError):
    """The environment does not describe a site that can start safely."""


def _git_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        sha = ""
    return f"git:{sha}" if sha else "git:unknown"


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    env: str = DEVELOPMENT
    data_dir: Path = field(default_factory=_store_default_data_dir)
    origin: Optional[str] = None  # https://pinny.example.com; None in development
    admin_emails: Tuple[str, ...] = ()
    version: str = "git:unknown"
    dev_email: str = "local@localhost"
    jobs: str = JOBS_INPROCESS

    @property
    def production(self) -> bool:
        return self.env == PRODUCTION

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None,
                 data_dir: Optional[os.PathLike] = None) -> "Settings":
        e = os.environ if environ is None else environ
        env = (e.get("PINNY_ENV") or DEVELOPMENT).strip().lower()
        if env not in (DEVELOPMENT, PRODUCTION):
            raise ConfigError(f"PINNY_ENV must be {DEVELOPMENT} or {PRODUCTION}, not {env!r}.")
        admins = tuple(sorted({a.strip().lower() for a in (e.get("PINNY_ADMIN_EMAILS") or "").split(",")
                               if a.strip()}))
        raw_dir = data_dir if data_dir is not None else e.get("PINNY_DATA_DIR")
        origin = (e.get("PINNY_ORIGIN") or "").strip().rstrip("/") or None
        jobs = (e.get("PINNY_JOBS") or (JOBS_SANDBOX if env == PRODUCTION else JOBS_INPROCESS)).strip().lower()
        if jobs not in JOBS_MODES:
            raise ConfigError(f"PINNY_JOBS must be one of {', '.join(JOBS_MODES)}, not {jobs!r}.")
        s = cls(env=env,
                data_dir=Path(raw_dir).resolve() if raw_dir else _store_default_data_dir(),
                origin=origin, admin_emails=admins,
                version=(e.get("PINNY_VERSION") or "").strip() or _git_version(),
                dev_email=((e.get("PINNY_REVIEWER") or "").strip() or _os_user()) if env == DEVELOPMENT
                else "",
                jobs=jobs)
        if s.production:
            missing = [name for name, val in (
                ("PINNY_DATA_DIR", raw_dir), ("PINNY_ORIGIN", origin),
                ("PINNY_ADMIN_EMAILS", admins), ("PINNY_VERSION", e.get("PINNY_VERSION"))) if not val]
            if missing:
                raise ConfigError("Production needs these settings: " + ", ".join(missing) + ".")
            if not origin.startswith("https://"):
                raise ConfigError("PINNY_ORIGIN must start with https:// in production.")
            if jobs == JOBS_INPROCESS:
                raise ConfigError("Production opens PDFs only in sandboxed jobs: set PINNY_JOBS to "
                                  "sandbox or external.")
        return s

    def check_bind(self, host: str) -> None:
        """Development has no sign-in, so it may only listen on loopback."""
        if not self.production and not is_loopback(host):
            raise ConfigError(f"Development mode has no sign-in and may only listen on 127.0.0.1, "
                              f"not {host!r}. Set PINNY_ENV=production to turn on sign-in.")


def _os_user() -> str:
    try:
        import getpass
        return getpass.getuser() or "local@localhost"
    except Exception:  # noqa: BLE001
        return "local@localhost"
