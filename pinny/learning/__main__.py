"""Local maintenance: ``python -m pinny.learning [--data-dir DIR] <command>``.

Commands: ``status``, ``migrate``, ``export``, ``export-dataset``,
``suggest-threshold``, ``split``, ``page-complete``.

Crop regeneration needs a renderer, so it is driven by the integrating app
via ``LearningStore(crop_renderer=...).process_pending_crops()``.
"""

import argparse
import dataclasses
import datetime as dt
import json
import sys

from .loop import suggest_threshold
from .store import LearningStore, default_data_dir


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pinny.learning")
    p.add_argument("--data-dir", default=None, help="default: $PINNY_DATA_DIR or ~/.local/share/pinny")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print crop status counts")
    sub.add_parser("migrate", help="upgrade the store schema in place and print its version")
    e = sub.add_parser("export", help="write versioned metadata export (JSON)")
    e.add_argument("--document-version")
    e.add_argument("--labeled-only", action="store_true")
    e.add_argument("--include-heldout", action="store_true", help="include test/eval documents")
    e.add_argument("--out", help="default: <data-dir>/exports/export-<utc>.json")
    d = sub.add_parser("export-dataset", help="write a COCO training set for one document split")
    d.add_argument("out_dir")
    d.add_argument("--split", default="train", choices=["train", "val", "test", "eval"])
    d.add_argument("--allow-incomplete", action="store_true", help="include pages not marked complete")
    d.add_argument("--tile", type=int, help="tile size in canonical px")
    d.add_argument("--overlap", type=int, default=0)
    t = sub.add_parser("suggest-threshold", help="suggest a score threshold from reviews (never applied)")
    t.add_argument("--document-id")
    t.add_argument("--template-sha256")
    t.add_argument("--settings-sha256")
    t.add_argument("--target-precision", type=float, default=0.95)
    t.add_argument("--min-labels", type=int, default=30)
    s = sub.add_parser("split", help="show or set a document's split")
    s.add_argument("document_id")
    s.add_argument("split", nargs="?", choices=["train", "val", "test", "eval"])
    s.add_argument("--force", action="store_true", help="allow moving a document out of test/eval")
    c = sub.add_parser("page-complete", help="mark a canonical page fully reviewed")
    c.add_argument("canonical_page_id")
    c.add_argument("--allow-unreviewed", action="store_true")
    a = p.parse_args(argv)

    with LearningStore(a.data_dir or default_data_dir()) as store:
        if a.cmd == "export":
            out = a.out or store.exports_dir / f"export-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json"
            doc = store.export(document_version=a.document_version, include_unlabeled=not a.labeled_only,
                               out_path=out, include_heldout=a.include_heldout)
            print(f"{out}: {len(doc['examples'])} examples, {len(doc['events'])} events, "
                  f"{len(doc['label_conflicts'])} label conflicts")
        elif a.cmd == "export-dataset":
            tile = (a.tile, a.overlap) if a.tile else None
            m = store.export_dataset(a.out_dir, split=a.split, require_page_complete=not a.allow_incomplete,
                                     tile=tile)
            print(json.dumps({"export_id": m["export_id"], "sha256": m["sha256"], "counts": m["counts"],
                              "excluded_incomplete_pages": len(m["excluded_incomplete_pages"])}))
        elif a.cmd == "suggest-threshold":
            stats = store.review_stats(a.document_id, a.template_sha256, a.settings_sha256)
            res = suggest_threshold(stats, a.target_precision, a.min_labels)
            print(json.dumps(dataclasses.asdict(res)))
        elif a.cmd == "split":
            print(store.set_split(a.document_id, a.split, force=a.force) if a.split
                  else store.get_split(a.document_id))
        elif a.cmd == "page-complete":
            pr = store.mark_page_complete(a.canonical_page_id, allow_unreviewed=a.allow_unreviewed)
            print(json.dumps(dataclasses.asdict(pr)))
        elif a.cmd == "migrate":
            print(f"store schema {store.schema_version} (migrated from {store.migrated_from or 'none'})")
        else:
            json.dump(store.crop_status_counts(), sys.stdout)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
