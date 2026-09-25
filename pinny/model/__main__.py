"""``python -m pinny.model``: inspect, verify or compare model packages.

Exit codes: 0 ok, 2 invalid/corrupt package or failed check, 64 usage error.
The signing key is read from an environment variable, never the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .format import ModelPackageError
from .package import ModelPackage
from .promote import promotion_check


def _key(env: str | None) -> bytes | None:
    if not env:
        return None
    value = os.environ.get(env)
    if not value:
        raise ModelPackageError("invalid_key", f"Environment variable {env} is not set.")
    return value.encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pinny.model", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("inspect", "print the manifest summary"), ("verify", "check integrity and signature")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("package")
        p.add_argument("--key-env", help="environment variable holding the signing key")
    p = sub.add_parser("promote-check", help="may CANDIDATE replace CURRENT?")
    p.add_argument("candidate")
    p.add_argument("current", nargs="?")
    p.add_argument("--max-drop", type=float, default=0.01)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0

    try:
        if args.cmd in ("inspect", "verify"):
            pkg = ModelPackage.load(args.package, verify_key=_key(args.key_env))
            if args.cmd == "verify":
                print(f"OK {pkg.package_sha256} signed={pkg.signed}")
                return 0
            m = pkg.manifest()
            summary = {
                **pkg.identity(),
                "package_sha256": pkg.package_sha256,
                "signed": pkg.signed,
                "created_at": pkg.created_at,
                "templates": len(pkg.templates),
                "negatives": len(pkg.negatives),
                "verifier": (m["verifier"] or {}).get("type"),
                "calibration": (m["calibration"] or {}).get("type"),
                "decision": m["decision"],
                "renderer_version": pkg.renderer_version,
                "evaluation": pkg.evaluation,
                "provenance": pkg.provenance,
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        cand = ModelPackage.load(args.candidate)
        cur = ModelPackage.load(args.current) if args.current else None
        decision = promotion_check(cand, cur, max_drop=args.max_drop)
        print("PROMOTE" if decision.ok else "DO NOT PROMOTE")
        for r in decision.reasons:
            print(f"- {r}")
        return 0 if decision.ok else 2
    except ModelPackageError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
