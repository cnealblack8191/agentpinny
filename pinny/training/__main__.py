"""``python -m pinny.training <command> ...`` (docs/phase2-ownership.md). Owned by chat A.

Minimal dispatcher created by the detector chat before A landed; the
coordinator keeps A's version and carries over the command table entries.
Each entry maps a command to "module:function"; the function takes argv
(the arguments after the command) and returns an exit code. Modules are
imported only when their command runs, so torch stays lazy.
"""

from __future__ import annotations

import importlib
import sys

COMMANDS: dict[str, str] = {
    "train-detector": "pinny.training.detector_train:main",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print("usage: python -m pinny.training <command> [args]\ncommands: " + ", ".join(sorted(COMMANDS)),
              file=sys.stderr if argv and argv[0] not in ("-h", "--help") else sys.stdout)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    module, func = COMMANDS[argv[0]].split(":")
    return int(getattr(importlib.import_module(module), func)(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
