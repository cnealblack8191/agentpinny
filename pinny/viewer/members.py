"""Manage site members from the server (docs/training-site.md section 1).

    python -m pinny.viewer.members list
    python -m pinny.viewer.members add EMAIL [--role reviewer|admin]
    python -m pinny.viewer.members remove EMAIL

Uses ``$PINNY_DATA_DIR`` (or ``--data-dir``). People must also be on the
Cognito user pool (invited there) to sign in at the load balancer.
"""

from __future__ import annotations

import argparse
import getpass
import sys

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
    r = sub.add_parser("remove")
    r.add_argument("email")
    args = ap.parse_args(argv)
    db = SiteDB(Settings.from_env(data_dir=args.data_dir).data_dir)
    actor = f"cli:{getpass.getuser()}"
    try:
        if args.cmd == "list":
            for m in db.members():
                print(f"{m.email}\t{m.role}\tadded {m.added_at} by {m.added_by or '-'}")
        elif args.cmd == "add":
            m = db.put_member(args.email, args.role, actor=actor)
            print(f"{m.email}: {m.role}")
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
