"""PDF user space <-> canonical raster px mapping (contracts section 2)."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pikepdf
import pytest

from pinny.vector.content import open_pdf, page_frame
from pinny.vector.frame import PT_TO_PX, PageFrame, frame_from_boxes, normalize_rotate

from .pdfgen import user_to_canonical_px

ROTATIONS = (0, 90, 180, 270)
CROPS = [
    (0.0, 0.0, 612.0, 792.0),  # letter, origin at 0
    (36.0, 72.0, 1260.0, 864.0),  # offset CropBox
    (-200.0, -100.0, 1024.0, 692.0),  # negative origin
]


@pytest.mark.parametrize("rotate,crop", list(itertools.product(ROTATIONS, CROPS)))
def test_matches_reference_formulas(rotate, crop):
    frame = PageFrame(crop=crop, rotate=rotate)
    rng = np.random.default_rng(7)
    pts = np.stack([rng.uniform(crop[0], crop[2], 200), rng.uniform(crop[1], crop[3], 200)], axis=1)
    np.testing.assert_allclose(frame.to_px(pts), user_to_canonical_px(pts, crop, rotate), atol=1e-9)


@pytest.mark.parametrize("rotate,crop", list(itertools.product(ROTATIONS, CROPS)))
def test_round_trip(rotate, crop):
    frame = PageFrame(crop=crop, rotate=rotate)
    rng = np.random.default_rng(rotate)
    pts = np.stack([rng.uniform(-500, 2000, 500), rng.uniform(-500, 2000, 500)], axis=1)
    np.testing.assert_allclose(frame.to_user(frame.to_px(pts)), pts, atol=1e-9)
    px = np.stack([rng.uniform(0, frame.width_px, 500), rng.uniform(0, frame.height_px, 500)], axis=1)
    np.testing.assert_allclose(frame.to_px(frame.to_user(px)), px, atol=1e-9)


@pytest.mark.parametrize("rotate,crop", list(itertools.product(ROTATIONS, CROPS)))
def test_page_corners_land_on_raster_corners(rotate, crop):
    frame = PageFrame(crop=crop, rotate=rotate)
    x0, y0, x1, y1 = crop
    W, H = frame.width_pt * PT_TO_PX, frame.height_pt * PT_TO_PX
    corners_px = frame.to_px(np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]]))
    got = {(round(x, 6), round(y, 6)) for x, y in corners_px}
    assert got == {(0.0, 0.0), (round(W, 6), 0.0), (0.0, round(H, 6)), (round(W, 6), round(H, 6))}
    # The top-left of the upright view is the corner the rotation brings there.
    top_left_user = {0: (x0, y1), 90: (x0, y0), 180: (x1, y0), 270: (x1, y1)}[rotate]
    np.testing.assert_allclose(frame.to_px(np.array([top_left_user])), [[0.0, 0.0]], atol=1e-9)


@pytest.mark.parametrize("rotate", ROTATIONS)
def test_size_and_descriptor(rotate):
    frame = PageFrame(crop=(10, 20, 10 + 1224, 20 + 792), rotate=rotate)
    w, h = (1224, 792) if rotate in (0, 180) else (792, 1224)
    assert frame.width_px == math.ceil(w * 200 / 72)
    assert frame.height_px == math.ceil(h * 200 / 72)
    assert frame.descriptor() == {
        "space": "canonical_raster_px", "dpi": 200, "width": frame.width_px,
        "height": frame.height_px, "origin": "top-left", "y_axis": "down",
    }


def test_rotation_is_clockwise_on_screen():
    # A point near the top of an unrotated page ends up near the right edge
    # when the page is shown rotated 90 degrees clockwise.
    f0 = PageFrame(crop=(0, 0, 100, 200), rotate=0)
    f90 = PageFrame(crop=(0, 0, 100, 200), rotate=90)
    p = np.array([[50.0, 190.0]])  # near the top edge in PDF space
    assert f0.to_px(p)[0, 1] < 0.1 * f0.height_px
    assert f90.to_px(p)[0, 0] > 0.9 * f90.width_px


def test_normalize_rotate_and_boxes():
    assert normalize_rotate(-90) == 270
    assert normalize_rotate(450) == 90
    assert normalize_rotate(45) == 0
    assert normalize_rotate(None) == 0
    f = frame_from_boxes([612, 792, 0, 0], [700, 50, 50, 900], 0)  # corners in any order
    assert f.crop == (50.0, 50.0, 612.0, 792.0)  # CropBox clipped to MediaBox
    assert frame_from_boxes([0, 0, 100, 100], [200, 200, 300, 300], 0).crop == (0.0, 0.0, 100.0, 100.0)


def test_inherited_attributes(tmp_path):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 400))
    pages = pdf.Root.Pages
    del pdf.pages[0].obj["/MediaBox"]
    pages.MediaBox = pikepdf.Array([0, 0, 500, 600])
    pages.Rotate = 270
    pdf.save(tmp_path / "inh.pdf")
    with open_pdf(tmp_path / "inh.pdf") as doc:
        f = page_frame(doc.pages[0])
    assert f.crop == (0.0, 0.0, 500.0, 600.0)
    assert f.rotate == 270


@pytest.mark.parametrize("rotate", ROTATIONS)
def test_agrees_with_pdfium_render(tmp_path, rotate):
    """Independent check: render with pdfium at 200 DPI and find a black mark
    where the mapping predicts it."""
    pdfium = pytest.importorskip("pypdfium2")
    crop = (40.0, 60.0, 440.0, 360.0)
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(500, 400))
    page.obj.CropBox = pikepdf.Array(list(crop))
    page.obj.Rotate = rotate
    mx, my = 100.0, 300.0  # mark centre (upper-left area of the crop)
    page.obj.Contents = pdf.make_stream(f"0 g {mx - 4} {my - 4} 8 8 re f".encode())
    path = tmp_path / f"mark{rotate}.pdf"
    pdf.save(path)

    frame = PageFrame(crop=crop, rotate=rotate)
    doc = pdfium.PdfDocument(str(path))
    img = np.asarray(doc[0].render(scale=200 / 72, grayscale=True).to_pil())
    doc.close()
    assert abs(img.shape[1] - frame.width_px) <= 1 and abs(img.shape[0] - frame.height_px) <= 1
    ys, xs = np.nonzero(img < 128)
    got = np.array([xs.mean() + 0.5, ys.mean() + 0.5])
    want = frame.to_px(np.array([[mx, my]]))[0]
    assert np.abs(got - want).max() <= 1.5, (got, want)
