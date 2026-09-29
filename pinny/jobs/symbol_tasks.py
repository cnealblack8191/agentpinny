"""Train one symbol type's learned package (``train_symbol``; see
``pinny.model.symbols``). Runs in the ``train`` pool, which may read the
learning store, the crops and the documents and write ``models/``.

The payload names the tag. The examples are the §6 crops of that tag's
reviewed machine matches (approved and rejected). Pins added by hand are
left out: their crop is a fixed square without a symbol box. The package
starts from the legend's drawing of the symbol, as the newest whole-set
scan with that tag cut it (``set_scans/<run>/symbols/<entry>.png``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tasks import TaskError

#: Tags are short labels written on the legend (``class_label``).
TAG_RE = re.compile(r"^[^\x00-\x1f/\\]{1,40}$")


def _decode_rgb(path: str):
    import cv2
    import numpy as np

    data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def legend_drawing(data_dir: Path, tag: str):
    """The newest legend drawing of ``tag`` a whole-set scan cut, or None."""
    best: Optional[tuple] = None
    for listed in (Path(data_dir) / "documents").glob("*/set_scans/*/symbols.json"):
        try:
            symbols = json.loads(listed.read_text()).get("symbols") or {}
        except (OSError, ValueError):
            continue
        for entry_id, t in symbols.items():
            if t != tag or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(entry_id)):
                continue
            png = listed.parent / "symbols" / f"{entry_id}.png"
            try:
                mtime = png.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, png)
    return None if best is None else _decode_rgb(str(best[1]))


def train_symbol(p: Dict[str, Any], data_dir: Path, progress, job_id: str) -> Dict[str, Any]:
    from pinny.learning import LearningStore
    from pinny.model.judge import Example
    from pinny.model.symbols import SymbolIndex
    from pinny.model.symbols import train_symbol as _train

    tag = p.get("tag")
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise TaskError("invalid_job", "Bad symbol tag.")
    progress.update(0.02, f"collecting the reviews of {tag}")
    with LearningStore(data_dir) as store:
        pos = store.template_bank_crops(class_label=tag, include_heldout=True)
        neg = store.negative_crops(class_label=tag, include_heldout=True)
    records = [(r, True) for r in pos if r.origin == "machine"] + [(r, False) for r in neg if r.origin == "machine"]
    docs = {r.document_id for r, _ in records}
    by_document = len(docs) >= 2
    examples: List[Example] = []
    missing = 0
    for r, approved in records:
        img = _decode_rgb(r.path) if r.path else None
        if img is None:
            missing += 1
            continue
        examples.append(Example(img, approved, r.document_id if by_document else r.canonical_page_id,
                                int(r.rotation or 0)))
    progress.update(0.2, f"{sum(e.approved for e in examples)} approved and "
                    f"{sum(not e.approved for e in examples)} rejected match(es) of {tag}"
                    + (f"; {missing} crop(s) not cut yet" if missing else ""))
    original = legend_drawing(data_dir, tag)
    if original is None:
        raise TaskError("no_legend_drawing", f"No whole-set scan has used {tag} yet. Scan a drawing set "
                        "with its legend first.", 422)
    training = _train(tag, original, examples,
                      provenance={"documents": len(docs), "grouped_by": "document" if by_document else "sheet"})
    progress.update(0.9, "saving")
    entry = SymbolIndex(data_dir).save(tag, training, trained_by=p.get("requested_by"))
    progress.update(1.0, ("Switched on. " if entry["active"] else "Saved, not switched on. ") + entry["reason"])
    return {k: entry[k] for k in ("tag", "active", "reason", "check", "templates", "negatives", "verifier",
                                   "trained_at")} | {"missing_crops": missing}
