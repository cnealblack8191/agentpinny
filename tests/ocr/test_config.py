"""The OCR app setting: off by default, saved with local data, env override."""

from __future__ import annotations

import json

import pytest

from pinny.ocr import OCR_ENGINE_ENV, OcrError, engine_options, get_engine, selected_engine_name, set_engine
from pinny.ocr.__main__ import main
from pinny.ocr.config import SETTINGS_FILENAME, stored_engine


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    d = tmp_path / "pinny-data"
    monkeypatch.setenv("PINNY_DATA_DIR", str(d))
    monkeypatch.delenv(OCR_ENGINE_ENV, raising=False)
    return d


def test_fresh_install_is_off(data_dir):
    assert stored_engine() is None
    assert selected_engine_name() == "none"
    assert get_engine() is None
    assert not (data_dir / SETTINGS_FILENAME).exists()


def test_set_engine_persists_and_is_selected(data_dir):
    assert set_engine("Tesseract") == "tesseract"
    assert json.loads((data_dir / SETTINGS_FILENAME).read_text()) == {"ocr": {"engine": "tesseract"}}
    assert selected_engine_name() == "tesseract"
    selected = [o["name"] for o in engine_options() if o["selected"]]
    assert selected == ["tesseract"]


def test_turning_it_back_off(data_dir):
    set_engine("tesseract")
    set_engine("none")
    assert get_engine() is None


def test_other_modules_keys_are_preserved(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / SETTINGS_FILENAME).write_text(json.dumps({"viewer": {"theme": "dark"}, "ocr": {"lang": "eng"}}))
    set_engine("tesseract")
    data = json.loads((data_dir / SETTINGS_FILENAME).read_text())
    assert data == {"viewer": {"theme": "dark"}, "ocr": {"lang": "eng", "engine": "tesseract"}}


def test_env_overrides_saved_setting(monkeypatch):
    set_engine("tesseract")
    monkeypatch.setenv(OCR_ENGINE_ENV, "none")
    assert get_engine() is None
    options = engine_options()
    assert all(o["overridden"] for o in options)
    assert [o["name"] for o in options if o["selected"]] == ["none"]


def test_explicit_name_overrides_everything(monkeypatch):
    monkeypatch.setenv(OCR_ENGINE_ENV, "tesseract")
    assert selected_engine_name("none") == "none"


def test_explicit_data_dir(tmp_path):
    other = tmp_path / "other"
    set_engine("tesseract", data_dir=other)
    assert selected_engine_name(data_dir=other) == "tesseract"
    assert selected_engine_name() == "none"


def test_unknown_engine_cannot_be_saved(data_dir):
    with pytest.raises(OcrError) as err:
        set_engine("abbyy")
    assert err.value.code == "unknown_engine"
    assert not (data_dir / SETTINGS_FILENAME).exists()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"ocr": {"engine": 3}}'])
def test_corrupt_settings_are_reported(data_dir, content):
    data_dir.mkdir(parents=True)
    (data_dir / SETTINGS_FILENAME).write_text(content)
    with pytest.raises(OcrError) as err:
        selected_engine_name()
    assert err.value.code == "invalid_config"
    assert str(data_dir / SETTINGS_FILENAME) in str(err.value)


def test_cli_engines_and_set_engine(capsys, data_dir):
    assert main(["engines"]) == 0
    options = json.loads(capsys.readouterr().out)
    assert [o["name"] for o in options if o["selected"]] == ["none"]
    assert main(["set-engine", "tesseract"]) == 0
    capsys.readouterr()
    assert stored_engine() == "tesseract"


def test_cli_refuses_to_run_while_off(capsys, tmp_path):
    assert main(["sheet-info", str(tmp_path / "missing.png")]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "ocr_disabled"
