"""Title-block parsing. Layouts are built in the reading frame and mapped to
the page, so the same layout is also checked at every rotation."""

from __future__ import annotations

import numpy as np
import pytest

from pinny.ocr import (
    OcrBox, OcrError, OcrResult, OcrSettings, OcrWord, TesseractEngine, parse_sheet_info, read_sheet_info,
    title_block_region,
)
from pinny.ocr.geometry import unrotate_box

PAGE = (4000, 3000)
HAVE_TESSERACT = TesseractEngine().is_available()


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("PINNY_DATA_DIR", str(tmp_path / "pinny-data"))
    monkeypatch.delenv("PINNY_OCR_ENGINE", raising=False)


def layout(rows, rotation=0, conf=90.0):
    """rows: [(text, x, y, char_height)] in the reading frame, one line each."""
    words = []
    for line_no, (text, x, y, h) in enumerate(rows, start=1):
        cx = x
        for word_no, token in enumerate(text.split()):
            w = max(1, int(len(token) * h * 0.6))
            box = unrotate_box(OcrBox(cx, y, w, h), PAGE, rotation)
            words.append(OcrWord(token, box, conf, (1, 1, line_no), rotation))
            cx += w + int(h * 0.4)
    return OcrResult("fake", "0", OcrSettings(), tuple(words))


TITLE_BLOCK = [
    ("REV", 10, 10, 20), ("DATE", 110, 10, 20), ("DESCRIPTION", 300, 10, 20),
    ("1", 20, 50, 20), ("08/01/26", 110, 50, 20), ("PERMIT SET", 300, 50, 20),
    ("3", 20, 90, 20), ("09/15/26", 110, 90, 20), ("BID SET", 300, 90, 20),
    ("2", 20, 130, 20), ("09/01/26", 110, 130, 20), ("ADDENDUM", 300, 130, 20),
    ("SHEET TITLE:", 10, 200, 20),
    ("FIRST FLOOR", 10, 240, 40), ("POWER PLAN", 10, 290, 40),
    ("SCALE: 1/8\" = 1'-0\"", 10, 380, 20),
    ("SHEET NUMBER", 500, 380, 20),
    ("E-101", 500, 420, 90),
]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_full_title_block_at_every_rotation(rotation):
    info = parse_sheet_info(layout(TITLE_BLOCK, rotation))
    assert info.sheet_number.value == "E-101" and info.sheet_number.method == "label"
    assert info.sheet_title.value == "FIRST FLOOR POWER PLAN"
    assert info.revision.value == "3" and info.revision.method == "table"
    assert info.sheet_number.rotation == rotation
    assert info.warnings == ()


def test_inline_labels():
    info = parse_sheet_info(layout([
        ("SHEET NO.: E2.01", 0, 0, 20), ("TITLE: LIGHTING PLAN", 0, 40, 20), ("REV: B", 0, 80, 20),
    ]))
    assert (info.sheet_number.value, info.sheet_title.value, info.revision.value) == ("E2.01", "LIGHTING PLAN", "B")


def test_number_split_into_two_words():
    info = parse_sheet_info(layout([("DWG NO", 0, 0, 20), ("E 101", 0, 30, 60)]))
    assert info.sheet_number.value == "E101"


def test_value_beside_label():
    info = parse_sheet_info(layout([("SHEET", 0, 0, 20), ("FP-2", 200, 0, 20)]))
    assert info.sheet_number.value == "FP-2" and info.sheet_number.method == "label"


def test_title_stops_at_the_next_label():
    info = parse_sheet_info(layout([
        ("DRAWING TITLE", 0, 0, 20), ("ENLARGED PLANS", 0, 30, 30), ("DATE: 09/01/26", 0, 65, 30),
    ]))
    assert info.sheet_title.value == "ENLARGED PLANS"


def test_single_rev_field_is_not_a_table():
    info = parse_sheet_info(layout([("REVISION", 0, 0, 20), ("4", 0, 30, 30)]))
    assert info.revision.value == "4" and info.revision.method == "label"


def test_unlabelled_number_falls_back_to_tallest_match_with_warning():
    info = parse_sheet_info(layout([("LP1", 0, 0, 20), ("E-301", 0, 100, 80)]))
    assert info.sheet_number.value == "E-301" and info.sheet_number.method == "pattern"
    assert "LP1" in info.sheet_number.alternatives
    assert any("tallest" in w for w in info.warnings)
    assert info.sheet_title is None and info.revision is None


def test_nothing_found():
    info = parse_sheet_info(OcrResult("fake", "0", OcrSettings(), ()))
    assert info.sheet_number is None and len(info.warnings) == 3
    assert info.to_dict()["sheet_number"] is None


def test_edge_warning_from_parse():
    result = layout([("SHEET NO", 100, 100, 20), ("E-101", 100, 130, 60)])
    number_box = [w for w in result.words if w.text == "E-101"][0].box
    region = OcrBox(0, 0, number_box.x2, 1000)
    info = parse_sheet_info(OcrResult("fake", "0", OcrSettings(), result.words, region))
    assert any("sheet number touches" in w for w in info.warnings)


def test_to_dict_shape():
    d = parse_sheet_info(layout(TITLE_BLOCK)).to_dict()
    assert set(d) == {"sheet_number", "sheet_title", "revision", "region", "engine", "warnings"}
    assert set(d["sheet_number"]) == {"value", "box", "confidence", "method", "rotation", "alternatives"}


def test_title_block_region_default_is_bottom_right():
    assert title_block_region(1000, 1000) == OcrBox(650, 550, 350, 450)
    with pytest.raises(OcrError):
        title_block_region(100, 100, (0.5, 0.5, 0.4, 1.0))


def test_read_sheet_info_needs_ocr_turned_on():
    with pytest.raises(OcrError) as err:
        read_sheet_info(np.full((100, 100), 255, np.uint8))
    assert err.value.code == "ocr_disabled"


class FakeEngine:
    name, license = "fake", "MIT"

    def __init__(self):
        self.regions = []

    def is_available(self):
        return True

    def recognize(self, page, settings=OcrSettings(), region=None):
        self.regions.append(region)
        return OcrResult("fake", "0", settings, (), region)


def test_read_sheet_info_searches_title_block_unless_told_otherwise():
    engine = FakeEngine()
    page = np.full((1000, 2000, 3), 255, np.uint8)
    read_sheet_info(page, engine)
    read_sheet_info(page, engine, full_page=True)
    read_sheet_info(page, engine, region=OcrBox(1, 2, 3, 4))
    assert engine.regions == [title_block_region(2000, 1000), None, OcrBox(1, 2, 3, 4)]


# -- with the real engine -----------------------------------------------------

def _drawn_title_block():
    cv2 = pytest.importorskip("cv2")
    b = np.full((560, 900, 3), 255, np.uint8)
    F = cv2.FONT_HERSHEY_SIMPLEX

    def t(s, x, y, scale, thick):
        cv2.putText(b, s, (x, y), F, scale, (0, 0, 0), thick, cv2.LINE_AA)
    t("REV", 20, 40, 0.8, 2); t("DATE", 120, 40, 0.8, 2); t("DESCRIPTION", 290, 40, 0.8, 2)
    t("1", 30, 85, 0.8, 2); t("08/01/26", 120, 85, 0.8, 2); t("PERMIT SET", 290, 85, 0.8, 2)
    t("2", 30, 130, 0.8, 2); t("09/15/26", 120, 130, 0.8, 2); t("BID SET", 290, 130, 0.8, 2)
    t("SHEET TITLE:", 20, 210, 0.8, 2)
    t("FIRST FLOOR", 20, 275, 1.4, 3); t("POWER PLAN", 20, 335, 1.4, 3)
    t("SHEET NUMBER", 520, 420, 0.8, 2)
    t("E-101", 520, 530, 2.6, 6)
    return b


@pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract executable not installed")
@pytest.mark.parametrize("vertical", [False, True])
def test_reads_drawn_title_block(vertical):
    block = _drawn_title_block()
    W, H = 4000, 3000  # a small sheet at 200 DPI; the block fits the default corner
    page = np.full((H, W, 3), 255, np.uint8)
    if vertical:  # set along the right edge, reading bottom to top
        block = np.ascontiguousarray(np.rot90(block, k=1))
    h, w = block.shape[:2]
    x0, y0 = W - w - 40, H - h - 40
    page[y0:y0 + h, x0:x0 + w] = block
    info = read_sheet_info(page, TesseractEngine())
    assert info.sheet_number.value == "E-101"
    assert info.sheet_title.value == "FIRST FLOOR POWER PLAN"
    assert info.revision.value == "2"
    assert info.sheet_number.rotation == (90 if vertical else 0)
    assert info.warnings == ()
    b = info.sheet_number.box
    assert x0 <= b.x and b.x2 <= x0 + w and y0 <= b.y and b.y2 <= y0 + h


@pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract executable not installed")
def test_warns_when_a_field_is_cut_by_the_region():
    block = _drawn_title_block()
    page = np.full((800, 1200, 3), 255, np.uint8)
    page[200:760, 250:1150] = block
    # Cut the region through the sheet number.
    info = read_sheet_info(page, TesseractEngine(), region=OcrBox(250, 200, 700, 600))
    assert any("cut off" in w for w in info.warnings)
