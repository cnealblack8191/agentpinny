"""(d) Page classification, errors and the result shape."""

from __future__ import annotations

import json

import pikepdf
import pytest

from pinny.detection import DetectionError
from pinny.vector import (
    RasterPageError,
    VectorMatcher,
    VectorMatchError,
    VectorSettings,
    classify_page,
)

from .pdfgen import (
    Placement,
    box_for,
    build_flat_page,
    build_raster_page,
    random_lines_ops,
    standard_placements,
)

MEDIA = (0, 0, 612, 792)


def _save(pdf, tmp_path, name="p.pdf", **kw):
    path = tmp_path / name
    pdf.save(path, **kw)
    return path


@pytest.fixture
def vector_pdf(tmp_path):
    pdf = pikepdf.new()
    build_flat_page(pdf, standard_placements(6), mediabox=MEDIA)
    return _save(pdf, tmp_path, "v.pdf")


def test_raster_page_classified_raster(tmp_path):
    pdf = pikepdf.new()
    build_raster_page(pdf, extra_ops="10 10 m 50 50 l S\n")  # a markup line
    path = _save(pdf, tmp_path)
    kind = classify_page(path, 0)
    assert kind.kind == "raster"
    assert kind.stats["image_coverage"] == pytest.approx(1.0)
    assert kind.stats["image_xobjects"] == 1


def test_raster_page_detect_raises(tmp_path):
    pdf = pikepdf.new()
    build_raster_page(pdf)
    path = _save(pdf, tmp_path)
    with pytest.raises(RasterPageError) as exc:
        VectorMatcher().detect(path, 0, (10, 10, 40, 40))
    assert exc.value.code == "raster_page"
    assert "raster matcher" in str(exc.value)
    assert isinstance(exc.value, DetectionError)


def test_scan_with_vector_overlay_is_mixed(tmp_path):
    pdf = pikepdf.new()
    build_raster_page(pdf, extra_ops=random_lines_ops(400, 612, 792))
    path = _save(pdf, tmp_path)
    assert classify_page(path, 0).kind == "mixed"


def test_vector_and_text_pages(tmp_path, vector_pdf):
    k = classify_page(vector_pdf, 0)
    assert k.kind == "vector" and k.stats["path_segments"] > 0
    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page.obj.Contents = pdf.make_stream(b"BT /F1 12 Tf 72 72 Td (hi) Tj ET")
    assert classify_page(_save(pdf, tmp_path, "t.pdf"), 0).kind == "vector"


def test_encrypted_pdf(tmp_path):
    pdf = pikepdf.new()
    build_flat_page(pdf, standard_placements(2), mediabox=MEDIA)
    path = _save(pdf, tmp_path, "enc.pdf", encryption=pikepdf.Encryption(user="u", owner="o"))
    with pytest.raises(VectorMatchError) as exc:
        VectorMatcher().detect(path, 0, (0, 0, 10, 10))
    assert exc.value.code == "pdf_encrypted"
    with pytest.raises(VectorMatchError):
        classify_page(path, 0)


def test_permission_only_encryption_reads_with_warning(tmp_path):
    placements = standard_placements(4)
    pdf = pikepdf.new()
    build_flat_page(pdf, placements, mediabox=MEDIA)
    path = _save(pdf, tmp_path, "perm.pdf", encryption=pikepdf.Encryption(user="", owner="o"))
    d = VectorMatcher().detect(path, 0, box_for(placements[0], MEDIA, 0)).to_dict()
    assert len(d["detections"]) == 4
    assert any("encrypted" in w for w in d["warnings"])


@pytest.mark.parametrize("index", [-1, 1, 99, "0", 1.0, True])
def test_bad_page_index(vector_pdf, index):
    with pytest.raises(VectorMatchError) as exc:
        VectorMatcher().detect(vector_pdf, index, (0, 0, 10, 10))
    assert exc.value.code == "page_index_out_of_range"


def test_unreadable_and_missing(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    for p in (bad, tmp_path / "missing.pdf"):
        with pytest.raises(VectorMatchError) as exc:
            VectorMatcher().detect(p, 0, (0, 0, 10, 10))
        assert exc.value.code == "pdf_unreadable"


@pytest.mark.parametrize("box", [(0, 0, 0, 10), "nope", (1, 2, 3), {"x": 1}, (99999, 99999, 5, 5)])
def test_invalid_exemplar_box(vector_pdf, box):
    with pytest.raises(VectorMatchError) as exc:
        VectorMatcher().detect(vector_pdf, 0, box)
    assert exc.value.code == "invalid_exemplar_box"


def test_empty_exemplar_box(vector_pdf):
    with pytest.raises(VectorMatchError) as exc:
        VectorMatcher().detect(vector_pdf, 0, (1500, 50, 30, 30))  # blank area
    assert exc.value.code == "no_vector_geometry"


@pytest.mark.parametrize("kw", [{"threshold": 0}, {"threshold": 1.5}, {"strategy": "x"},
                                {"rotations": (45,)}, {"tolerance_pt": 0}, {"max_anchors": 0}])
def test_invalid_settings(vector_pdf, kw):
    with pytest.raises(VectorMatchError) as exc:
        VectorMatcher().detect(vector_pdf, 0, (0, 0, 10, 10), VectorSettings(**kw))
    assert exc.value.code == "invalid_settings"


def test_result_shape_follows_contract(vector_pdf):
    placements = standard_placements(6)
    box = box_for(placements[0], MEDIA, 0)
    r = VectorMatcher().detect(vector_pdf, 0, {"x": box[0], "y": box[1], "width": box[2], "height": box[3]})
    d = r.to_dict()
    json.dumps(d)  # serialisable
    assert d["coordinate_frame"] == {
        "space": "canonical_raster_px", "dpi": 200, "width": 1700, "height": 2200,
        "origin": "top-left", "y_axis": "down",
    }
    assert d["template"]["box"] == dict(zip(("x", "y", "width", "height"), box))
    assert [det["id"] for det in d["detections"]] == [f"det-{i}" for i in range(1, 7)]
    for det in d["detections"]:
        assert set(det) <= {"id", "box", "x", "y", "score", "rotation", "mirrored", "source", "angle"}
        b = det["box"]
        assert all(isinstance(b[k], int) for k in ("x", "y", "width", "height"))
        assert det["x"] == b["x"] + b["width"] / 2 and det["y"] == b["y"] + b["height"] / 2
        assert det["rotation"] in (0, 90, 180, 270)
    assert isinstance(d["elapsed_seconds"], float)


def test_timeout(tmp_path):
    placements = standard_placements(4)
    pdf = pikepdf.new()
    build_flat_page(pdf, placements, mediabox=MEDIA)
    path = _save(pdf, tmp_path)
    with pytest.raises(VectorMatchError) as exc:
        VectorMatcher().detect(path, 0, box_for(placements[0], MEDIA, 0),
                               VectorSettings(max_runtime_seconds=1e-9))
    assert exc.value.code == "timeout"
