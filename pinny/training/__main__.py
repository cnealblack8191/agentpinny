"""``python -m pinny.training <command> [args]`` (owned by the Dataset chat).

Each command maps to a module exposing ``main(argv) -> int``; modules are
imported only when their command runs, so torch loads lazily.
"""

from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "train-verifier": "pinny.training.verifier_train",
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        print("usage: python -m pinny.training <command> [args]\ncommands: " + ", ".join(sorted(COMMANDS)),
              file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    return importlib.import_module(COMMANDS[argv[0]]).main(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
