"""``python -m pinny.legend``: find, read and compare symbol legends.

Exit codes: 0 ok, 2 no legend or unreadable input, 64 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import LegendError
from .library import EngineerLibrary, compare
from .model import Legend
from .reader import find_legends, read_legend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pinny.legend", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("find", help="list pages that look like they hold a legend")
    p.add_argument("pdf")
    p = sub.add_parser("read", help="read a legend and print or save it")
    p.add_argument("pdf")
    p.add_argument("--page", type=int, help="0-based page index (default: best page found)")
    p.add_argument("--engineer")
    p.add_argument("--out", help="write the legend JSON here")
    p = sub.add_parser("compare", help="compare a legend with an engineer's saved legend")
    p.add_argument("legend")
    p.add_argument("library")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0
    try:
        if args.cmd == "find":
            found = find_legends(args.pdf)
            if not found:
                print("No legend found.", file=sys.stderr)
                return 2
            for c in found:
                print(f"page {c.page_index}: {c.heading!r} ({c.rows} lines below)")
            return 0
        if args.cmd == "read":
            lg = read_legend(args.pdf, args.page, args.engineer)
            if args.out:
                lg.save(args.out)
            print(f"Legend on page {lg.page_index}: {lg.heading!r}")
            for e in lg.entries:
                flags = ",".join(e.flags)
                count = "" if e.count else f"  [not counted: {e.not_counted_reason}]"
                print(f"  {e.tag:<6} {e.name}  ({e.group}; {e.status}{'; ' + flags if flags else ''}){count}")
            print(f"{len(lg.entries)} symbols, {len(lg.counted())} counted, {len(lg.needs_review())} need a look.")
            for w in lg.warnings:
                print(f"warning: {w}")
            return 0
        lg = Legend.load(args.legend)
        diffs = compare(lg, EngineerLibrary.load(args.library))
        print(json.dumps({k: [d.to_dict() for d in v] for k, v in diffs.items()}, indent=2))
        return 0
    except LegendError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
