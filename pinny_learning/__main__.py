"""Local maintenance: ``python -m pinny_learning {export,status} [--data-dir DIR]``.

Crop regeneration needs a renderer, so it is driven by the integrating app
via ``LearningStore(crop_renderer=...).process_pending_crops()``.
"""

import argparse
import datetime as dt
import json
import sys

from .store import LearningStore, default_data_dir


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pinny_learning")
    p.add_argument("--data-dir", default=None, help="default: $PINNY_DATA_DIR or ~/.local/share/pinny")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write versioned metadata export (JSON)")
    e.add_argument("--document-version-id")
    e.add_argument("--labeled-only", action="store_true")
    e.add_argument("--out", help="default: <data-dir>/exports/export-<utc>.json")
    sub.add_parser("status", help="print crop status counts")
    a = p.parse_args(argv)

    with LearningStore(a.data_dir or default_data_dir()) as store:
        if a.cmd == "export":
            out = a.out or store.exports_dir / f"export-{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}.json"
            doc = store.export(document_version_id=a.document_version_id,
                               include_unlabeled=not a.labeled_only, out_path=out)
            print(f"{out}: {len(doc['examples'])} examples, {len(doc['events'])} events")
        else:
            json.dump(store.crop_status_counts(), sys.stdout)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
