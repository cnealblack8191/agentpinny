"""pinny.training.synthetic: labelled synthetic pages through the real RenderService."""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from pinny.render import RenderService
from pinny.training import dataset as ds
from pinny.training import synthetic as syn
from tests.factory import receptacle_ops as factory_receptacle_ops

SMALL = syn.SynthParams(page_width_pt=288.0, page_height_pt=288.0, receptacles=(4, 8))  # 800 x 800 px


def files_under(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def test_glyph_is_the_factory_receptacle():
    for args in ((0.0, 0.0), (100.0, 200.0, 6.0), (12.5, 7.25, 5.4)):
        assert syn.receptacle_ops(*args) == factory_receptacle_ops(*args)
    assert syn.circle_ops(10, 20, 6).strip().endswith("c S")
    assert syn.circle_ops(10, 20, 6).count(" S ") == 1  # no bars


def test_build_pdf_renders_through_the_real_service(tmp_path):
    page = syn.generate_page(random.Random(3), SMALL)
    pdf = syn.build_pdf([page.content], SMALL.page_width_pt, SMALL.page_height_pt)
    assert pdf == syn.build_pdf([page.content], SMALL.page_width_pt, SMALL.page_height_pt)
    version = RenderService(tmp_path / "r").ingest_pdf(pdf)
    assert version.page_count == 1 and (version.pages[0].width_px, version.pages[0].height_px) == (800, 800)


def test_ground_truth_points_sit_on_the_glyphs(tmp_path):
    res = syn.synthesize_dataset(tmp_path / "datasets", documents=2, pages_per_document=1, params=SMALL)
    radius_px = syn.GLYPH_RADIUS_PT * syn.PX_PER_PT
    for e in res.manifest["detector"]:
        img = cv2.imread(str(res.path / e["path"]), cv2.IMREAD_UNCHANGED)
        assert e["points"]
        for p in e["points"]:
            crop = ds.crop_centered(img, p["x"], p["y"], size=int(2.6 * radius_px))
            ys, xs = np.nonzero(crop < 100)
            assert len(xs) > 50  # the ring and bars are drawn here
            c = (crop.shape[0] - 1) / 2
            assert abs(xs.mean() - c) < 2.5 and abs(ys.mean() - c) < 2.5
            # The centre of the glyph, between the bars, is paper white (bar gap ~ 0.7 r).
            centre = crop[int(c) - 1:int(c) + 2, int(c) - 1:int(c) + 2]
            assert np.median(centre) > 150


def test_rotations_scale_jitter_and_distractors():
    rng = random.Random(0)
    pages = [syn.generate_page(rng, syn.SynthParams()) for _ in range(12)]
    rotations = Counter(p["rotation"] for page in pages for p in page.points)
    assert set(rotations) == {0, 90, 180, 270}
    scales = [p["scale"] for page in pages for p in page.points]
    assert min(scales) >= 0.9 and max(scales) <= 1.1 and max(scales) - min(scales) > 0.1
    kinds = Counter(d["kind"] for page in pages for d in page.distractors)
    assert {"circle", "square", "text"} <= set(kinds)
    content = "".join(p.content for p in pages)
    assert " Tj " in content and " re f " in content and " re S " in content
    assert " w " in content.replace("0 G 0 g 1 w ", "")  # thick wall strokes
    for page in pages:  # distractors never overlap a receptacle, so labels stay exact
        for d in page.distractors:
            assert all(np.hypot(d["x"] - p["x"], d["y"] - p["y"]) > 30 for p in page.points)


def test_noise_is_seeded_and_bounded():
    gray = np.full((50, 60), 255, np.uint8)
    a = syn.add_noise(gray, np.random.default_rng(1), 6.0, 0.01)
    b = syn.add_noise(gray, np.random.default_rng(1), 6.0, 0.01)
    assert a.dtype == np.uint8 and np.array_equal(a, b) and not np.array_equal(a, gray)
    assert np.array_equal(syn.add_noise(gray, np.random.default_rng(1), 0.0, 0.0), gray)


def test_synthetic_dataset_is_marked_valid_and_deterministic(tmp_path):
    kw = dict(documents=7, pages_per_document=1, seed=5, params=SMALL)
    a = syn.synthesize_dataset(tmp_path / "a", **kw)
    b = syn.synthesize_dataset(tmp_path / "b", **kw)
    assert a.dataset_id == b.dataset_id and files_under(a.path) == files_under(b.path)
    assert syn.synthesize_dataset(tmp_path / "c", **{**kw, "seed": 6}).dataset_id != a.dataset_id

    m = a.manifest
    assert m["source"]["synthetic"] is True
    assert m["source"]["generator"]["name"] == "pinny.training.synthetic"
    assert ds.verify_dataset(a.path) == []
    # Document ids are chosen so every split gets documents, and the split is still the hash rule.
    docs = {s: {e["document_id"] for e in m["detector"] if e["split"] == s} for s in ds.SPLITS}
    assert [len(docs[s]) for s in ds.SPLITS] == [5, 1, 1]
    assert all(ds.split_for(e["document_id"]) == e["split"] for e in m["verifier"] + m["detector"])
    counts = m["counts"]
    assert sum(counts["verifier"][s]["pos"] for s in ds.SPLITS) == sum(
        counts["detector"][s]["points"] for s in ds.SPLITS)
    assert all(counts["verifier"][s]["neg"] > 0 for s in ds.SPLITS)
    kinds = Counter(e["source_example_id"].rsplit("-", 1)[1] for e in m["verifier"] if e["label"] == 0)
    assert {"circle", "background"} <= set(kinds)


def test_synthetic_pdfs_go_to_a_render_service_only_when_one_is_passed(tmp_path):
    render = RenderService(tmp_path / "render")
    syn.synthesize_dataset(tmp_path / "datasets", documents=1, pages_per_document=2, params=SMALL, render=render)
    [version] = render.list_versions()
    assert version.page_count == 2
