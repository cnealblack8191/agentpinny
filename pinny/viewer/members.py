"""Manage site members from the server (docs/training-site.md section 1).

    python -m pinny.viewer.members list
    python -m pinny.viewer.members add EMAIL [--role reviewer|admin]
    python -m pinny.viewer.members remove EMAIL
    python -m pinny.viewer.members setup-link EMAIL   # one-time link to choose a password
    python -m pinny.viewer.members reset EMAIL        # forgot password: new link, old one stops working
    python -m pinny.viewer.members sign-out-all       # end every session

Uses ``$PINNY_DATA_DIR`` (or ``--data-dir``) and ``$PINNY_ORIGIN`` for the
links. Send a link to its owner privately; it works once, for 3 days.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import sys
import time

from .settings import Settings
from .sitedb import ROLES, SiteDB


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pinny.viewer.members")
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("email")
    a.add_argument("--role", choices=ROLES, default="reviewer")
    for name in ("remove", "setup-link", "reset"):
        sub.add_parser(name).add_argument("email")
    sub.add_parser("sign-out-all")
    args = ap.parse_args(argv)
    settings = Settings.from_env(data_dir=args.data_dir)
    db = SiteDB(settings.data_dir)
    db.ensure_admins(settings.admin_emails)
    actor = f"cli:{getpass.getuser()}"
    try:
        if args.cmd == "list":
            for m in db.members():
                pw = "password set" if db.has_password(m.email) else "no password yet"
                print(f"{m.email}\t{m.role}\t{pw}\tadded {m.added_at} by {m.added_by or '-'}")
        elif args.cmd == "add":
            m = db.put_member(args.email, args.role, actor=actor)
            print(f"{m.email}: {m.role}")
        elif args.cmd in ("setup-link", "reset"):
            token, expires = db.issue_setup_link(args.email, actor=actor, now=time.time(),
                                                 reset=args.cmd == "reset")
            origin = settings.origin or "http://127.0.0.1:8765"
            when = dt.datetime.fromtimestamp(expires, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if args.cmd == "reset":
                print(f"{args.email}'s password was removed and they were signed out everywhere.")
            print(f"Send this link to {args.email} privately. It works once, until {when}:")
            print(f"{origin}/setup.html#token={token}")
        elif args.cmd == "sign-out-all":
            print(f"Ended {db.sign_out_everyone(actor=actor)} sessions.")
        elif not db.remove_member(args.email, actor=actor):
            print(f"{args.email} is not a member.", file=sys.stderr)
            return 1
        else:
            print(f"Removed {args.email}.")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
