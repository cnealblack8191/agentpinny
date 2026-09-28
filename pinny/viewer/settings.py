"""Site configuration from the environment (docs/training-site.md section 10).

Production refuses to start unless every setting it needs is present, so a
missing variable fails at boot, not on the first request.
"""

from __future__ import annotations

import ipaddress
import os
import re
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

# Which sign-in gate sits in front of the site (docs/training-site.md section 1).
GATE_ALB = "alb"  # AWS Application Load Balancer with Amazon Cognito
GATE_CLOUDFLARE = "cloudflare"  # Cloudflare Tunnel with Cloudflare Access
GATES = (GATE_ALB, GATE_CLOUDFLARE)

_ALB_ARN_RE = re.compile(r"^arn:aws[a-z-]*:elasticloadbalancing:([a-z0-9-]+):\d{12}:loadbalancer/app/[\w-]+/[0-9a-f]+$")


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
    cf_team_domain: Optional[str] = None  # acme.cloudflareaccess.com
    cf_aud: Optional[str] = None
    gate: str = GATE_ALB
    alb_arn: Optional[str] = None  # the load balancer that signs x-amzn-oidc-data
    oidc_issuer: Optional[str] = None  # the Cognito user pool, checked when set
    sign_out_url: Optional[str] = None  # Cognito logout URL the ALB gate redirects to
    admin_emails: Tuple[str, ...] = ()
    version: str = "git:unknown"
    dev_email: str = "local@localhost"
    jobs: str = JOBS_INPROCESS

    @property
    def production(self) -> bool:
        return self.env == PRODUCTION

    @property
    def alb_region(self) -> Optional[str]:
        m = _ALB_ARN_RE.match(self.alb_arn or "")
        return m.group(1) if m else None

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
        team = (e.get("PINNY_CF_TEAM_DOMAIN") or "").strip().lower() or None
        if team:
            team = team.removeprefix("https://").rstrip("/")
        gate = (e.get("PINNY_GATE") or GATE_ALB).strip().lower()
        if gate not in GATES:
            raise ConfigError(f"PINNY_GATE must be one of {', '.join(GATES)}, not {gate!r}.")
        jobs = (e.get("PINNY_JOBS") or (JOBS_SANDBOX if env == PRODUCTION else JOBS_INPROCESS)).strip().lower()
        if jobs not in JOBS_MODES:
            raise ConfigError(f"PINNY_JOBS must be one of {', '.join(JOBS_MODES)}, not {jobs!r}.")
        s = cls(env=env,
                data_dir=Path(raw_dir).resolve() if raw_dir else _store_default_data_dir(),
                origin=origin, cf_team_domain=team,
                cf_aud=(e.get("PINNY_CF_AUD") or "").strip() or None,
                gate=gate, alb_arn=(e.get("PINNY_ALB_ARN") or "").strip() or None,
                oidc_issuer=(e.get("PINNY_OIDC_ISSUER") or "").strip().rstrip("/") or None,
                sign_out_url=(e.get("PINNY_SIGN_OUT_URL") or "").strip() or None,
                admin_emails=admins,
                version=(e.get("PINNY_VERSION") or "").strip() or _git_version(),
                dev_email=((e.get("PINNY_REVIEWER") or "").strip() or _os_user()) if env == DEVELOPMENT
                else "",
                jobs=jobs)
        if s.production:
            gate_settings = ((("PINNY_ALB_ARN", s.alb_arn),) if gate == GATE_ALB else
                             (("PINNY_CF_TEAM_DOMAIN", team), ("PINNY_CF_AUD", s.cf_aud)))
            missing = [name for name, val in (
                ("PINNY_DATA_DIR", raw_dir), ("PINNY_ORIGIN", origin), *gate_settings,
                ("PINNY_ADMIN_EMAILS", admins), ("PINNY_VERSION", e.get("PINNY_VERSION"))) if not val]
            if missing:
                raise ConfigError("Production needs these settings: " + ", ".join(missing) + ".")
            if gate == GATE_ALB and s.alb_region is None:
                raise ConfigError("PINNY_ALB_ARN must be an application load balancer ARN "
                                  "(arn:aws:elasticloadbalancing:<region>:<account>:loadbalancer/app/<name>/<id>).")
            if s.sign_out_url and not s.sign_out_url.startswith("https://"):
                raise ConfigError("PINNY_SIGN_OUT_URL must start with https://.")
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
                              f"not {host!r}. Set PINNY_ENV=production behind Cloudflare Access.")


def _os_user() -> str:
    try:
        import getpass
        return getpass.getuser() or "local@localhost"
    except Exception:  # noqa: BLE001
        return "local@localhost"
