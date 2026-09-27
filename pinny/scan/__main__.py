"""``python -m pinny.scan``: count a drawing set's legend symbols.

    python -m pinny.scan set.pdf [--legend legend.json] [--pages 2,3,4] [--out result.json]

Without ``--legend`` the legend is read from the set; if any symbol needs a
look first, nothing is scanned. Exit codes: 0 ok, 2 can't scan (message
says why), 64 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys

from pinny.detection.types import DetectionError

from pinny.legend import Legend, LegendError, read_legend

from .scanner import ScanError, SetScanSettings, scan_set


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pinny.scan", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf")
    parser.add_argument("--legend", help="reviewed legend JSON (default: read it from the set)")
    parser.add_argument("--pages", help="comma-separated 0-based pages to scan (default: all but the legend)")
    parser.add_argument("--out", help="write the full result JSON here")
    try:
        args = parser.parse_args(argv)
        pages = tuple(int(p) for p in args.pages.split(",")) if args.pages else None
    except SystemExit as exc:
        return 64 if exc.code else 0
    except ValueError:
        print("--pages must be comma-separated page numbers", file=sys.stderr)
        return 64
    try:
        legend = Legend.load(args.legend) if args.legend else read_legend(args.pdf)
        result = scan_set(args.pdf, legend, SetScanSettings(pages=pages),
                          progress=lambda p, done, total: print(f"sheet {p}: done ({done}/{total})", file=sys.stderr))
    except (ScanError, LegendError, DetectionError) as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2)
            fh.write("\n")
    names = {e["tag"]: e["name"] for e in result.entries}
    print(f"{'TAG':<8}{'COUNT':>7}  SYMBOL")
    for tag, n in result.counts().items():
        print(f"{tag:<8}{n:>7}  {names.get(tag, '')}")
    for w in result.warnings:
        print(f"note: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
