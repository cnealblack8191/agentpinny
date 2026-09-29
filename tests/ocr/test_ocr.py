"""Tests for the optional OCR module.

Selection, validation and TSV parsing run everywhere. The recognition tests
need the Tesseract executable and are skipped without it.
"""

from __future__ import annotations

import os
import stat

import numpy as np
import pytest

from pinny.ocr import (
    OCR_ENGINE_ENV,
    OcrBox,
    OcrEngine,
    OcrError,
    OcrSettings,
    OcrUnavailable,
    TesseractEngine,
    engine_names,
    engine_options,
    get_engine,
    selected_engine_name,
)
from pinny.ocr.tesseract import TESSERACT_CMD_ENV, _encode_pnm, parse_tsv

HAVE_TESSERACT = TesseractEngine().is_available()
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract executable not installed")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Never read or write the developer's real Pinny settings."""
    monkeypatch.setenv("PINNY_DATA_DIR", str(tmp_path / "pinny-data"))
    monkeypatch.delenv(OCR_ENGINE_ENV, raising=False)
    monkeypatch.delenv(TESSERACT_CMD_ENV, raising=False)


# -- selection: off by default ---------------------------------------------

def test_ocr_is_off_by_default(monkeypatch):
    monkeypatch.delenv(OCR_ENGINE_ENV, raising=False)
    assert selected_engine_name() == "none"
    assert get_engine() is None


def test_blank_env_means_off(monkeypatch):
    monkeypatch.setenv(OCR_ENGINE_ENV, "  ")
    assert get_engine() is None


def test_engine_options_list_off_first_as_default():
    options = engine_options()
    assert options[0]["name"] == "none" and options[0]["default"] is True
    assert [o["name"] for o in options] == engine_names()
    tess = next(o for o in options if o["name"] == "tesseract")
    assert tess["license"] == "Apache-2.0" and tess["default"] is False
    assert sum(o["default"] for o in options) == 1


def test_unknown_engine_is_rejected(monkeypatch):
    monkeypatch.setenv(OCR_ENGINE_ENV, "abbyy")
    with pytest.raises(OcrError) as err:
        get_engine()
    assert err.value.code == "unknown_engine"


def test_selected_but_missing_engine_raises_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv(TESSERACT_CMD_ENV, str(tmp_path / "no-such-tesseract"))
    with pytest.raises(OcrUnavailable) as err:
        get_engine("tesseract")
    assert err.value.code == "ocr_unavailable"


def test_broken_executable_is_unavailable(tmp_path):
    fake = tmp_path / "tesseract"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    engine = TesseractEngine(command=str(fake))
    assert engine.is_available() is False


def test_engine_satisfies_protocol():
    assert isinstance(TesseractEngine(), OcrEngine)


# -- validation --------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"language": ""}, {"language": "eng;rm"}, {"language": "eng+"},
    {"page_segmentation_mode": 14}, {"min_confidence": 101}, {"dpi": 0},
    {"rotations": ()}, {"rotations": (0, 45)}, {"rotations": (90, 90)},
    {"upscale_to_dpi": 150}, {"upscale_to_dpi": 1000}, {"duplicate_overlap_ratio": 0},
])
def test_invalid_settings(kwargs):
    with pytest.raises(OcrError) as err:
        OcrSettings(**kwargs).validate()
    assert err.value.code == "invalid_settings"


def test_multi_language_setting_is_valid():
    OcrSettings(language="eng+chi_sim").validate()


@pytest.mark.parametrize("page", [
    np.zeros((10, 10), dtype=np.float32),
    np.zeros((10, 10, 2), dtype=np.uint8),
    np.zeros((0, 10), dtype=np.uint8),
    [[0, 0]],
])
def test_invalid_page(page):
    with pytest.raises(OcrError) as err:
        TesseractEngine(command="/nonexistent").recognize(page)
    assert err.value.code == "invalid_page"


def test_region_outside_page():
    page = np.full((50, 50, 3), 255, dtype=np.uint8)
    with pytest.raises(OcrError) as err:
        TesseractEngine(command="/nonexistent").recognize(page, region=OcrBox(40, 40, 20, 20))
    assert err.value.code == "invalid_region"


def test_page_too_large():
    page = np.full((100, 100), 255, dtype=np.uint8)
    with pytest.raises(OcrError) as err:
        TesseractEngine(command="/nonexistent").recognize(page, OcrSettings(max_page_pixels=100))
    assert err.value.code == "page_too_large"


def test_invalid_box():
    with pytest.raises(OcrError):
        OcrBox(0, 0, 0, 5)
    with pytest.raises(OcrError):
        OcrBox(0.5, 0, 5, 5)  # type: ignore[arg-type]


# -- TSV parsing -------------------------------------------------------------

TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t\n"
    "4\t1\t1\t1\t1\t0\t10\t20\t200\t30\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t10\t20\t90\t30\t96.5\tPANEL\n"
    "5\t1\t1\t1\t1\t2\t110\t20\t60\t30\t40.0\tLP-1\n"
    "5\t1\t2\t1\t1\t1\t300\t400\t50\t25\t88\t\"20A\n"
    "5\t1\t2\t1\t1\t2\t360\t400\t5\t25\t90\t \n"
)


def test_parse_tsv_keeps_words_and_offsets_boxes():
    words, warnings = parse_tsv(TSV, dx=1000, dy=2000)
    assert warnings == []
    assert [w.text for w in words] == ["PANEL", "LP-1", "\"20A"]
    assert words[0].box == OcrBox(1010, 2020, 90, 30)
    assert words[0].confidence == 96.5
    assert words[0].line_key == words[1].line_key != words[2].line_key


def test_parse_tsv_min_confidence():
    words, _ = parse_tsv(TSV, min_confidence=50)
    assert [w.text for w in words] == ["PANEL", "\"20A"]


def test_parse_tsv_skips_bad_rows_with_warning():
    bad = TSV.splitlines()[0] + "\n5\t1\t1\t1\t1\t1\tx\t20\t90\t30\t96\tBAD\n"
    words, warnings = parse_tsv(bad)
    assert words == [] and len(warnings) == 1


def test_encode_pnm_headers():
    assert _encode_pnm(np.zeros((2, 3), np.uint8)).startswith(b"P5\n3 2\n255\n")
    assert len(_encode_pnm(np.zeros((2, 3, 3), np.uint8))) == len(b"P6\n3 2\n255\n") + 18


# -- recognition with the real engine ----------------------------------------

def _text_page(lines, size=(400, 1200)):
    cv2 = pytest.importorskip("cv2")
    page = np.full((*size, 3), 255, dtype=np.uint8)
    for text, (x, y) in lines:
        cv2.putText(page, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3, cv2.LINE_AA)
    return page


@needs_tesseract
def test_recognize_reads_text_in_page_coordinates():
    page = _text_page([("PANEL LP1", (60, 120)), ("CIRCUIT 12", (600, 320))])
    result = get_engine("tesseract").recognize(page)
    texts = [w.text.upper() for w in result.words]
    assert "PANEL" in texts and "CIRCUIT" in texts
    panel = result.words[texts.index("PANEL")]
    assert 40 <= panel.box.x <= 80 and 60 <= panel.box.y <= 125
    assert result.engine == "tesseract" and result.engine_version
    d = result.to_dict()
    assert d["engine"]["name"] == "tesseract" and d["words"][0]["box"].keys() == {"x", "y", "width", "height"}


@needs_tesseract
def test_recognize_region_returns_page_coordinates():
    page = _text_page([("PANEL LP1", (60, 120)), ("CIRCUIT 12", (600, 320))])
    region = OcrBox(550, 240, 600, 120)
    result = TesseractEngine().recognize(page, region=region)
    texts = [w.text.upper() for w in result.words]
    assert "CIRCUIT" in texts and "PANEL" not in texts
    word = result.words[texts.index("CIRCUIT")]
    assert word.box.x >= region.x and word.box.y >= region.y
    assert word.box.x2 <= region.x2 and word.box.y2 <= region.y2
    assert result.region == region


@needs_tesseract
def test_blank_page_has_no_words():
    page = np.full((200, 200), 255, dtype=np.uint8)
    assert TesseractEngine().recognize(page).words == ()


@needs_tesseract
def test_missing_language_data():
    with pytest.raises(OcrError) as err:
        TesseractEngine().recognize(np.full((50, 50), 255, np.uint8), OcrSettings(language="zzz"))
    assert err.value.code == "language_unavailable"


def test_result_text_groups_lines():
    from pinny.ocr import OcrResult
    words, _ = parse_tsv(TSV)
    result = OcrResult("tesseract", "5", OcrSettings(), tuple(words))
    assert result.text == "PANEL LP-1\n\"20A"
