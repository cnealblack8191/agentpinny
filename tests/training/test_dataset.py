"""pinny.training.dataset: P3 label rules and the P4 dataset format."""

from __future__ import annotations

import json
import random
import uuid
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import pytest

from pinny.training import dataset as ds
from tests.training.conftest import PAGE_PX, pt_to_px

GLYPHS_PT = [(50.0, 60.0), (150.0, 150.0), (100.0, 40.0)]
GLYPHS_PX = [pt_to_px(*p) for p in GLYPHS_PT]
STORED = [(round(x, 3), round(y, 3)) for x, y in GLYPHS_PX]  # the store keeps 3 decimals


def build(world, root, **kw):
    return ds.build_dataset(world.export(), world.render, root, **kw)


def files_under(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def reviewed_document(world, *, shift: float = 0.0, document_id=None):
    """One document, one scan, every pin approved (a complete page)."""
    version = world.add_document([(x + shift, y) for x, y in GLYPHS_PT], document_id=document_id)
    scan = world.add_scan(version, [(x + shift * 200 / 72, y) for x, y in GLYPHS_PX])
    for i in range(len(GLYPHS_PX)):
        world.approve(scan, f"det-{i}")
    return version, scan


# ------------------------------------------------------------------ split


def test_split_function_is_by_document_and_matches_fractions():
    ids = [str(uuid.UUID(int=random.Random(1).getrandbits(128) ^ i)) for i in range(4000)]
    counts = Counter(ds.split_for(i) for i in ids)
    for name, frac in ds.DEFAULT_FRACTIONS.items():
        assert abs(counts[name] / len(ids) - frac) < 0.03
    assert all(0.0 <= ds.split_value(i) < 1.0 for i in ids[:100])
    assert ds.split_for(ids[0]) == ds.split_for(ids[0])
    # A non-zero seed reshuffles documents; seed 0 is the bare hash.
    assert [ds.split_for(i, seed=7) for i in ids[:200]] != [ds.split_for(i) for i in ids[:200]]


def test_split_has_no_document_leakage(world, tmp_path):
    docs = [reviewed_document(world, shift=3.0 * k) for k in range(10)]
    # A second version and a second page of the first drawing set: same document_id.
    v2 = world.add_document([(60.0, 70.0), (120.0, 90.0)], pages=2, document_id=docs[0][0].document_id)
    for page in (0, 1):
        scan = world.add_scan(v2, [pt_to_px(60.0, 70.0), pt_to_px(120.0, 90.0)], page_index=page)
        world.approve(scan, "det-0")
        world.reject(scan, "det-1")

    m = build(world, tmp_path / "datasets").manifest
    splits_by_doc: dict[str, set[str]] = {}
    for e in m["verifier"] + m["detector"]:
        splits_by_doc.setdefault(e["document_id"], set()).add(e["split"])
        assert e["split"] == ds.split_for(e["document_id"])
        assert e["path"].split("/")[1] == e["split"]
    assert len(splits_by_doc) == 10
    assert all(len(s) == 1 for s in splits_by_doc.values())
    first = docs[0][0].document_id
    pages = {e["canonical_page_id"] for e in m["verifier"] if e["document_id"] == first}
    assert len(pages) == 3  # v1 p0, v2 p0, v2 p1 all land in the one split


# ----------------------------------------------------------- label rules


def test_completeness_rule_excludes_pages_with_unreviewed_pins(world, tmp_path):
    # Page A: one pin left unreviewed -> no detector entry, but its labelled pins still train the verifier.
    va = world.add_document(GLYPHS_PT)
    sa = world.add_scan(va, GLYPHS_PX)
    world.approve(sa, "det-0")
    world.reject(sa, "det-1")

    # Page B: an old scan is half reviewed, the latest scan is complete -> included, latest points only.
    vb = world.add_document([(x, y + 5) for x, y in GLYPHS_PT])
    old = world.add_scan(vb, [(10.0, 10.0), (20.0, 20.0)])
    world.approve(old, "det-0")
    new = world.add_scan(vb, GLYPHS_PX[:2])
    world.approve(new, "det-0")
    world.reject(new, "det-1")
    manual = world.add_manual(new, 300.5, 400.25)

    # Page C: complete, then rescanned; the rescan is unreviewed -> excluded.
    vc = world.add_document([(x + 7, y) for x, y in GLYPHS_PT])
    sc = world.add_scan(vc, GLYPHS_PX)
    for i in range(3):
        world.approve(sc, f"det-{i}")
    world.add_scan(vc, GLYPHS_PX)

    m = build(world, tmp_path / "datasets").manifest
    det = {e["canonical_page_id"]: e for e in m["detector"]}
    assert set(det) == {vb.pages[0].canonical_page_id}
    b = det[vb.pages[0].canonical_page_id]
    assert b["source_scan_id"] == new
    assert b["points"] == sorted([{"x": STORED[0][0], "y": STORED[0][1]}, {"x": 300.5, "y": 400.25}],
                                 key=lambda p: (p["y"], p["x"]))  # rejected det-1 is background

    labels = {e["source_example_id"]: e["label"] for e in m["verifier"]}
    assert labels[f"{sa}/det-0"] == 1 and labels[f"{sa}/det-1"] == 0
    assert f"{sa}/det-2" not in labels  # unreviewed pins are never samples
    assert labels[f"{old}/det-0"] == 1 and f"{old}/det-1" not in labels
    assert labels[f"{new}/{manual}"] == 1
    assert all(labels[f"{sc}/det-{i}"] == 1 for i in range(3))


def test_page_scanned_with_no_detections_and_nothing_added_counts_as_complete(world, tmp_path):
    # P3 as written: zero unreviewed pins on the latest scan -> the page is used, as all background.
    v = world.add_document(GLYPHS_PT)
    world.add_scan(v, [])
    m = build(world, tmp_path / "datasets").manifest
    assert [e["points"] for e in m["detector"]] == [[]]
    assert m["verifier"] == []


def test_removed_manual_pins_are_unused(world, tmp_path):
    v = world.add_document(GLYPHS_PT)
    scan = world.add_scan(v, GLYPHS_PX[:1])
    world.approve(scan, "det-0")
    kept = world.add_manual(scan, 250.0, 260.0)
    gone = world.add_manual(scan, 400.0, 420.0)
    world.remove_manual(scan, gone)

    m = build(world, tmp_path / "datasets").manifest
    used = {e["source_example_id"] for e in m["verifier"]}
    assert used == {f"{scan}/det-0", f"{scan}/{kept}"}
    assert all(e["label"] == 1 for e in m["verifier"])  # removal is never a negative
    [page] = m["detector"]  # a removed pin does not make the page incomplete
    assert {(p["x"], p["y"]) for p in page["points"]} == {STORED[0], (250.0, 260.0)}


def test_labeled_only_export_is_refused(world, tmp_path):
    reviewed_document(world)
    with pytest.raises(ds.DatasetError) as err:
        ds.build_dataset(world.export(include_unlabeled=False), world.render, tmp_path / "datasets")
    assert err.value.code == "export_missing_unlabeled"
    assert not any((tmp_path / "datasets").glob(".staging-*"))


# ------------------------------------------------------------------ crops


def test_crop_centered_pads_with_white_at_page_edges():
    page = np.arange(200 * 300, dtype=np.uint32).reshape(200, 300).astype(np.uint8) // 2  # never 255
    corner = ds.crop_centered(page, 0.0, 0.0)
    assert corner.shape == (96, 96) and corner.dtype == np.uint8
    assert (corner[:48, :] == 255).all() and (corner[:, :48] == 255).all()
    assert np.array_equal(corner[48:, 48:], page[:48, :48])
    far = ds.crop_centered(page, 300.0, 200.0)
    assert (far[48:, :] == 255).all() and (far[:, 48:] == 255).all()
    assert np.array_equal(far[:48, :48], page[152:, 252:])
    inside = ds.crop_centered(page, 150.4, 100.6)  # nearest whole-pixel window: x0 = 102, y0 = 53
    assert np.array_equal(inside, page[53:149, 102:198])
    assert (ds.crop_centered(page, -500.0, -500.0) == 255).all()


def test_dataset_crops_are_padded_at_page_edges(world, tmp_path):
    v = world.add_document([(3.0, 213.0)])  # glyph near the top-left corner
    scan = world.add_scan(v, [pt_to_px(3.0, 213.0)])
    world.approve(scan, "det-0")
    corner = world.add_manual(scan, 0.0, 0.0)
    far = world.add_manual(scan, float(PAGE_PX), float(PAGE_PX))

    res = build(world, tmp_path / "datasets")
    by_ex = {e["source_example_id"]: e for e in res.manifest["verifier"]}
    page = cv2.cvtColor(np.asarray(world.render.render_page(v.document_version, 0)), cv2.COLOR_RGB2GRAY)
    for ex_id in (f"{scan}/det-0", f"{scan}/{corner}", f"{scan}/{far}"):
        e = by_ex[ex_id]
        crop = cv2.imread(str(res.path / e["path"]), cv2.IMREAD_UNCHANGED)
        assert crop.shape == (96, 96) and crop.dtype == np.uint8
        assert np.array_equal(crop, ds.crop_centered(page, e["x"], e["y"]))
    top_left = cv2.imread(str(res.path / by_ex[f"{scan}/{corner}"]["path"]), cv2.IMREAD_UNCHANGED)
    assert (top_left[:48] == 255).all() and (top_left[:, :48] == 255).all()
    assert top_left[48:, 48:].min() < 128  # the glyph near the corner is in the in-page quadrant
    bottom_right = cv2.imread(str(res.path / by_ex[f"{scan}/{far}"]["path"]), cv2.IMREAD_UNCHANGED)
    assert (bottom_right[48:] == 255).all() and (bottom_right[:, 48:] == 255).all()


# ------------------------------------------------------------ determinism


def test_rebuild_is_deterministic_and_byte_identical(world, tmp_path):
    reviewed_document(world)
    v, scan = reviewed_document(world, shift=20.0)
    world.add_manual(scan, 33.3, 44.4)

    a = build(world, tmp_path / "a")
    b = ds.build_dataset(world.export(), world.render, tmp_path / "b")  # a fresh export: new exported_at
    assert a.dataset_id == b.dataset_id
    assert a.path.name == a.dataset_id
    assert files_under(a.path) == files_under(b.path)

    again = build(world, tmp_path / "a")
    assert again.reused and again.dataset_id == a.dataset_id
    assert not any((tmp_path / "a").glob(".staging-*"))

    # Different content -> different id; an explicit created_at is part of the id too.
    world.reject(scan, "det-0")
    assert build(world, tmp_path / "c").dataset_id != a.dataset_id
    assert build(world, tmp_path / "d", created_at="2030-01-01T00:00:00Z").dataset_id != a.dataset_id


def test_dataset_id_is_the_hash_of_the_manifest(world, tmp_path):
    reviewed_document(world)
    res = build(world, tmp_path / "datasets")
    on_disk = json.loads((res.path / "manifest.json").read_text())
    assert on_disk == res.manifest
    body = {k: v for k, v in on_disk.items() if k != "dataset_id"}
    assert ds.sha256_hex(ds.canonical_json(body)) == on_disk["dataset_id"]
    assert on_disk["format"] == "pinny.dataset" and on_disk["format_version"] == 1
    assert on_disk["source"]["synthetic"] is False
    assert on_disk["source"]["export_sha256"] == ds.export_sha256(world.export())
    assert on_disk["split"] == {"method": "document_id_hash", "seed": 0,
                                "fractions": {"train": 0.7, "val": 0.15, "test": 0.15}}


# ------------------------------------------------------------------ counts


def test_manifest_counts_match_the_files(world, tmp_path):
    for k in range(6):
        v, scan = reviewed_document(world, shift=4.0 * k)
        world.reject(scan, "det-2")
    res = build(world, tmp_path / "datasets")
    m = res.manifest
    assert ds.verify_dataset(res.path) == []

    for split in ds.SPLITS:
        vdir, ddir = res.path / "verifier" / split, res.path / "detector" / split
        v_entries = [e for e in m["verifier"] if e["split"] == split]
        d_entries = [e for e in m["detector"] if e["split"] == split]
        assert len(list(vdir.glob("*.png")) if vdir.exists() else []) == len(v_entries)
        assert len(list(ddir.glob("*.png")) if ddir.exists() else []) == len(d_entries)
        c = m["counts"]["verifier"][split]
        assert c == {"pos": sum(e["label"] == 1 for e in v_entries), "neg": sum(e["label"] == 0 for e in v_entries)}
        assert m["counts"]["detector"][split] == {"pages": len(d_entries),
                                                  "points": sum(len(e["points"]) for e in d_entries)}
        for e in d_entries:
            img = cv2.imread(str(res.path / e["path"]), cv2.IMREAD_UNCHANGED)
            assert img.shape == (e["height"], e["width"]) == (PAGE_PX, PAGE_PX)
    totals = Counter()
    for split in ds.SPLITS:
        totals.update(m["counts"]["verifier"][split])
    assert totals == {"pos": 12, "neg": 6}
    assert sum(m["counts"]["detector"][s]["pages"] for s in ds.SPLITS) == 6


def test_verify_dataset_reports_tampering(world, tmp_path):
    reviewed_document(world)
    res = build(world, tmp_path / "datasets")
    first = res.path / res.manifest["verifier"][0]["path"]
    first.unlink()
    (res.path / "verifier" / "stray.png").write_bytes(b"x")
    problems = ds.verify_dataset(res.path)
    assert any("missing" in p for p in problems)
    assert any("stray.png" in p for p in problems)
