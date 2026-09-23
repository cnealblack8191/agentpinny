import math

import numpy as np
import pytest

from pinny.errors import PinnyError
from pinny.render import (
    CANONICAL_DPI,
    PageNotFoundError,
    PdfEncryptedError,
    RenderService,
    canonical_page_id,
    decode_png_rgb,
    document_version_for_bytes,
    parse_canonical_page_id,
)
from tests.factory import PageSpec, build_pdf, square_ops


def _ink_bounds(rgb):
    ys, xs = np.where(rgb[:, :, 0] < 128)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def test_errors_are_pinny_errors():
    e = PdfEncryptedError("pdf_encrypted", "msg")
    assert isinstance(e, PinnyError) and e.http_status == 422
    assert e.to_dict() == {"error": {"code": "pdf_encrypted", "message": "msg"}}
    with pytest.raises(ValueError):
        PinnyError("Not Snake", "x")


def test_ingest_is_content_addressed(render_service):
    data = build_pdf([PageSpec(), PageSpec(rotate=90)])
    v = render_service.ingest_pdf(data, original_filename="..\\x/E-101.pdf")
    assert v.document_version == document_version_for_bytes(data)
    assert v.original_filename == "E-101.pdf" and v.page_count == 2
    assert v.pages[1].canonical_page_id == f"{v.document_version}#p1"
    again = render_service.ingest_pdf(data)
    assert again == v
    new = render_service.ingest_pdf(build_pdf([PageSpec(content=square_ops(1, 1, 2))]), document_id=v.document_id)
    assert new.document_id == v.document_id and new.document_version != v.document_version
    assert len(render_service.list_versions(v.document_id)) == 2


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_canonical_raster_frame_and_rotation(render_service, rotate):
    # 10x10 pt square, lower-left corner at user (100, 500), page 612x792 pt.
    v = render_service.ingest_pdf(build_pdf([PageSpec(rotate=rotate, content=square_ops(100, 500, 10))]))
    rgb = render_service.render_page(v.document_version, 0)
    w_pt, h_pt = (792, 612) if rotate in (90, 270) else (612, 792)
    W, H = math.ceil(w_pt * CANONICAL_DPI / 72), math.ceil(h_pt * CANONICAL_DPI / 72)
    assert rgb.shape == (H, W, 3) and rgb.dtype == np.uint8
    assert render_service.page_frame(v.document_version, 0) == {
        "space": "canonical_raster_px", "dpi": 200, "width": W, "height": H, "origin": "top-left", "y_axis": "down",
    }
    s = CANONICAL_DPI / 72
    # Where the square's user-space box lands once /Rotate is applied (displayed, y down).
    expected = {
        0: (100, 792 - 510, 110, 792 - 500),
        90: (500, 100, 510, 110),
        180: (612 - 110, 500, 612 - 100, 510),
        270: (792 - 510, 612 - 110, 792 - 500, 612 - 100),
    }[rotate]
    got = _ink_bounds(rgb)
    for g, e in zip(got, expected, strict=True):
        assert abs(g - e * s) <= 1.5, (rotate, got, [e * s for e in expected])


def test_render_is_cached_and_deterministic(tmp_path):
    data = build_pdf([PageSpec(receptacles=[(200, 300)])])
    a = RenderService(tmp_path / "d")
    v = a.ingest_pdf(data)
    first = a.render_page(v.document_version, 0)
    assert not first.flags.writeable
    b = RenderService(tmp_path / "d")  # fresh process: reads the PNG cache
    assert np.array_equal(first, b.render_page(v.document_version, 0))


def test_crop_renderer(render_service):
    v = render_service.ingest_pdf(build_pdf([PageSpec(content=square_ops(100, 500, 10))]))
    png = render_service.crop_renderer({"document_version": v.document_version, "page_index": 0, "pixel_box": (270, 780, 320, 830), "dpi": 200})
    crop = decode_png_rgb(png)
    assert crop.shape == (50, 50, 3)
    full = render_service.render_page(v.document_version, 0)
    assert np.array_equal(crop, full[780:830, 270:320])
    clipped = decode_png_rgb(render_service.crop_renderer({"canonical_page_id": f"{v.document_version}#p0", "box": {"x": -10, "y": -10, "width": 30, "height": 30}}))
    assert clipped.shape == (20, 20, 3)
    with pytest.raises(PinnyError) as ei:
        render_service.crop_renderer({"document_version": v.document_version, "page_index": 0, "pixel_box": (0, 0, 5, 5), "dpi": 150})
    assert ei.value.code == "invalid_crop_spec"


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", "empty_upload"),
        (b"hello world", "pdf_unreadable"),
        (b"%PDF-1.7 garbage", "pdf_unreadable"),
    ],
)
def test_bad_uploads(render_service, data, code):
    with pytest.raises(PinnyError) as ei:
        render_service.ingest_pdf(data)
    assert ei.value.code == code


def test_encrypted_and_limits(tmp_path):
    svc = RenderService(tmp_path / "d", max_upload_bytes=50_000, max_pages=2, max_raster_pixels=1_000_000)
    with pytest.raises(PinnyError) as ei:
        svc.ingest_pdf(build_pdf([PageSpec()], password="pw"))
    assert ei.value.code == "pdf_encrypted" and ei.value.http_status == 422
    with pytest.raises(PinnyError) as ei:
        svc.ingest_pdf(build_pdf([PageSpec()] * 3))
    assert ei.value.code == "pdf_too_many_pages"
    with pytest.raises(PinnyError) as ei:
        svc.ingest_pdf(b"%PDF-" + b"0" * 60_000)
    assert ei.value.code == "upload_too_large" and ei.value.http_status == 413
    v = svc.ingest_pdf(build_pdf([PageSpec()]))  # 1700x2200 = 3.7 MP > 1 MP
    with pytest.raises(PinnyError) as ei:
        svc.render_page(v.document_version, 0)
    assert ei.value.code == "page_too_large"
    with pytest.raises(PageNotFoundError):
        svc.page_frame(v.document_version, 5)
    assert not list((tmp_path / "d" / "tmp").iterdir()), "staging files leaked"


def test_page_ids_and_path_safety(render_service):
    v = "sha256:" + "a" * 64
    assert parse_canonical_page_id(canonical_page_id(v, 3)) == (v, 3)
    for bad in ("sha256:../../etc", "doc_123", "sha256:" + "A" * 64):
        with pytest.raises(PinnyError):
            render_service.render_page(bad, 0)


def test_raster_is_rgb_not_bgr(render_service):
    v = render_service.ingest_pdf(build_pdf([PageSpec(content="1 0 0 rg 100 500 20 20 re f ")]))
    rgb = render_service.render_page(v.document_version, 0)
    s = CANONICAL_DPI / 72
    px = rgb[int((792 - 510) * s), int(110 * s)]
    assert tuple(px) == (255, 0, 0)
