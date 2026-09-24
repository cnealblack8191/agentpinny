"""A tiny hand-built ``pinny.dataset`` v1 fixture for the verifier (P4).

Pages are rendered from ``tests/factory.py`` receptacle glyphs (positives)
and filled squares, blank paper and off-centre glyphs (negatives). Crops are
cut here with an independent pad-and-slice implementation of the P4 rule,
so the Verifier tests can check that ``score()`` sees exactly these crops.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from tests.factory import PageSpec, build_pdf, square_ops

PAGE_PT = 360.0
SCALE = 200 / 72
CROP = 96
DOCS = {"doc-train-a": "train", "doc-train-b": "train", "doc-val": "val", "doc-test": "test"}


def reference_crop(gray: np.ndarray, x: float, y: float) -> np.ndarray:
    """P4 crop: 96x96 centred on (x, y), white padding, by padding the whole page.

    Valid for points up to 144 px outside the page.
    """
    pad = 2 * CROP
    padded = np.pad(gray, pad, constant_values=255)
    x0 = math.floor(x + 0.5) - CROP // 2 + pad
    y0 = math.floor(y + 0.5) - CROP // 2 + pad
    return padded[y0:y0 + CROP, x0:x0 + CROP].copy()


def page_layout(seed: int):
    """(PageSpec, positive points px, negative points px) for one synthetic page."""
    rng = np.random.default_rng(seed)
    slots = [(60.0 + 60 * i, 60.0 + 60 * j) for i in range(5) for j in range(5)]
    rng.shuffle(slots)
    glyphs, squares, blanks = slots[:9], slots[9:17], slots[17:21]
    jitter = lambda: float(rng.uniform(-3, 3))  # noqa: E731
    glyphs = [(x + jitter(), y + jitter()) for x, y in glyphs]
    content = "".join(square_ops(x - 5, y - 5, 10) for x, y in squares)
    spec = PageSpec(width_pt=PAGE_PT, height_pt=PAGE_PT, content=content, receptacles=glyphs)
    to_px = lambda x, y: (x * SCALE, (PAGE_PT - y) * SCALE)  # noqa: E731
    pos = [to_px(x, y) for x, y in glyphs]
    neg = [to_px(x, y) for x, y in squares + blanks]
    neg += [(px + 34.0, py - 30.0) for px, py in pos[:3]]  # glyph off-centre
    return spec, pos, neg


def render_gray(render_service, spec: PageSpec) -> tuple[str, np.ndarray, np.ndarray]:
    import cv2

    version = render_service.ingest_pdf(build_pdf([spec]), original_filename="fixture.pdf").document_version
    rgb = render_service.render_page(version, 0)
    return version, rgb, cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def build_verifier_dataset(root: Path, render_service, *, synthetic: bool = True) -> tuple[Path, dict]:
    """Write ``root/manifest.json`` and crops; return (root, {doc: (rgb, samples)})."""
    import cv2

    root.mkdir(parents=True, exist_ok=True)
    samples, pages = [], {}
    for n, (doc, split) in enumerate(DOCS.items()):
        spec, pos, neg = page_layout(seed=n)
        version, rgb, gray = render_gray(render_service, spec)
        page_id = f"{version}#p0"
        doc_samples = []
        for label, pts in ((1, pos), (0, neg)):
            for k, (x, y) in enumerate(pts):
                sid = hashlib.sha256(f"{page_id}|{label}|{k}".encode()).hexdigest()[:16]
                rel = f"verifier/{split}/{sid}.png"
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(root / rel), reference_crop(gray, x, y))
                s = {"sample_id": sid, "split": split, "path": rel, "label": label,
                     "canonical_page_id": page_id, "document_id": doc, "x": x, "y": y,
                     "source_example_id": f"scan-{n}/pin-{label}-{k}"}
                samples.append(s)
                doc_samples.append(s)
        pages[doc] = (rgb, doc_samples)
    counts = {s: {"pos": sum(1 for r in samples if r["split"] == s and r["label"] == 1),
                  "neg": sum(1 for r in samples if r["split"] == s and r["label"] == 0)}
              for s in ("train", "val", "test")}
    body = {
        "format": "pinny.dataset", "format_version": 1, "created_at": "2026-09-24T00:00:00Z",
        "source": {"export_sha256": "0" * 64, "store_schema_version": 1, "synthetic": synthetic},
        "split": {"method": "document_id_hash", "seed": 0,
                  "fractions": {"train": 0.7, "val": 0.15, "test": 0.15}},
        "verifier": samples, "detector": [], "counts": {"verifier": counts, "detector": {}},
    }
    body["dataset_id"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    return root, pages


def test_fixture_is_a_valid_p4_manifest(tmp_path, render_service):
    import cv2

    root, pages = build_verifier_dataset(tmp_path / "ds", render_service)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["format"] == "pinny.dataset" and manifest["format_version"] == 1
    for split in ("train", "val", "test"):
        c = manifest["counts"]["verifier"][split]
        assert c["pos"] > 0 and c["neg"] > 0
    for s in manifest["verifier"]:
        img = cv2.imread(str(root / s["path"]), cv2.IMREAD_UNCHANGED)
        assert img.shape == (96, 96) and img.dtype == np.uint8
    # Positives contain ink at the centre; blank negatives are pure white.
    rgb, doc_samples = pages["doc-train-a"]
    pos = [s for s in doc_samples if s["label"] == 1]
    assert all(cv2.imread(str(root / s["path"]), 0)[38:58, 38:58].min() < 128 for s in pos)


def test_reference_crop_pads_white_off_page():
    gray = np.zeros((50, 60), np.uint8)
    c = reference_crop(gray, 0.0, 0.0)
    assert (c[:48, :] == 255).all() and (c[:, :48] == 255).all() and (c[48:, 48:] == 0).all()
    assert (reference_crop(gray, 150.0, 140.0) == 255).all()
