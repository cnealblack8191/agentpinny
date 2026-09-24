"""Model registry and promotion (docs/phase2-contracts.md P8). No torch needed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pinny.errors import PinnyError
from pinny.models import registry
from pinny.models.registry import ModelRegistry
from tests.viewer.test_modes_fakes import make_model, write_evidence

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def reg(tmp_path):
    return ModelRegistry(tmp_path / "data")


def _code(fn, *a):
    with pytest.raises(PinnyError) as e:
        fn(*a)
    return e.value.code


def test_list_and_get(reg):
    assert reg.list_models() == [] and reg.active("verifier") is None
    v = make_model(reg.data_dir, "verifier", stamp="20260101T000000Z")
    d = make_model(reg.data_dir, "detector", stamp="20260102T000000Z")
    (reg.root / "junk").mkdir()  # not an artifact: ignored by list, refused by get
    (reg.root / "active.json").write_text("{}")
    assert [m["model_id"] for m in reg.list_models()] == [v, d]
    assert [m["model_id"] for m in reg.list_models("detector")] == [d]
    assert reg.get(v)["kind"] == "verifier"
    assert _code(reg.get, "junk") == "unknown_model"
    assert _code(reg.get, "../etc") == "invalid_model_id"
    assert _code(reg.list_models, "yolo") == "invalid_model_kind"


def test_get_refuses_a_mislabelled_artifact(reg):
    v = make_model(reg.data_dir, "verifier")
    meta = json.loads((reg.root / v / "model.json").read_text())
    meta["model_id"] = "verifier-other"
    (reg.root / v / "model.json").write_text(json.dumps(meta))
    assert _code(reg.get, v) == "invalid_model"
    assert reg.list_models() == []


def test_promote_refused_without_evidence(reg, tmp_path):
    v = make_model(reg.data_dir, "verifier")
    assert _code(reg.promote, v, tmp_path / "missing.json") == "evidence_missing"
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert _code(reg.promote, v, bad) == "evidence_invalid"
    bad.write_text(json.dumps({"format": "pinny.detections", "format_version": 1,
                               "promote": True, "model_id": v}))
    assert _code(reg.promote, v, bad) == "evidence_invalid"
    bad.write_text(json.dumps({"format": "pinny.promotion", "format_version": 2,
                               "promote": True, "model_id": v}))
    assert _code(reg.promote, v, bad) == "evidence_invalid"
    assert reg.active("verifier") is None
    assert not reg.active_path.exists()


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_promote_refused_unless_promote_is_true(reg, tmp_path, flag):
    v = make_model(reg.data_dir, "verifier")
    ev = write_evidence(tmp_path / "ev.json", v, promote=flag,
                        reasons=["precision fell by 0.05"])
    with pytest.raises(PinnyError) as e:
        reg.promote(v, ev)
    assert e.value.code == "promotion_not_recommended"
    assert v in e.value.message
    assert reg.active("verifier") is None


def test_promote_refused_for_another_model_or_mode(reg, tmp_path):
    v1 = make_model(reg.data_dir, "verifier", stamp="20260101T000000Z")
    v2 = make_model(reg.data_dir, "verifier", stamp="20260102T000000Z")
    ev = write_evidence(tmp_path / "ev.json", v2)
    assert _code(reg.promote, v1, ev) == "evidence_wrong_model"
    ev = write_evidence(tmp_path / "ev2.json", v1, mode="model")
    assert _code(reg.promote, v1, ev) == "evidence_wrong_mode"
    assert _code(reg.promote, "verifier-nope", write_evidence(tmp_path / "ev3.json",
                                                               "verifier-nope")) == "unknown_model"
    assert reg.active("verifier") is None


def test_promote_refused_when_weights_changed(reg, tmp_path):
    v = make_model(reg.data_dir, "verifier")
    (reg.root / v / "weights.pt").write_bytes(b"tampered")
    assert _code(reg.promote, v, write_evidence(tmp_path / "ev.json", v)) == "weights_mismatch"


def test_promote_and_active_survive_restart(tmp_path):
    data = tmp_path / "data"
    reg = ModelRegistry(data)
    v = make_model(data, "verifier")
    d = make_model(data, "detector")
    ev = write_evidence(tmp_path / "ev.json", v)
    entry = reg.promote(v, ev)
    assert entry["model_id"] == v and entry["kind"] == "verifier"
    reg.promote(d, write_evidence(tmp_path / "evd.json", d))
    # The evidence file can go away; the registry kept its own copy.
    ev.unlink()

    again = ModelRegistry(data)  # a fresh process would do exactly this
    assert again.active("verifier") == v and again.active("detector") == d
    doc = json.loads((data / "models" / "active.json").read_text())
    assert doc["format"] == "pinny.models.active" and doc["format_version"] == 1
    assert [h["model_id"] for h in doc["history"]] == [v, d]
    copy = data / "models" / again.active_entry("verifier")["evidence_copy"]
    assert json.loads(copy.read_text())["candidate"]["model_id"] == v
    assert not [p for p in (data / "models").iterdir() if p.name.endswith(".tmp")]

    # A newer verifier replaces the old one; the detector is untouched.
    v2 = make_model(data, "verifier", stamp="20260925T000000Z")
    again.promote(v2, write_evidence(tmp_path / "ev2.json", v2))
    assert ModelRegistry(data).active("verifier") == v2
    assert ModelRegistry(data).active("detector") == d
    assert again.deactivate("verifier") == v2
    assert ModelRegistry(data).active("verifier") is None


def test_active_json_is_never_left_half_written(tmp_path, monkeypatch):
    data = tmp_path / "data"
    reg = ModelRegistry(data)
    v1 = make_model(data, "verifier", stamp="20260101T000000Z")
    v2 = make_model(data, "verifier", stamp="20260102T000000Z")
    reg.promote(v1, write_evidence(tmp_path / "e1.json", v1))
    before = reg.active_path.read_bytes()

    def crash(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(registry.os, "replace", crash)
    with pytest.raises(OSError):
        reg.promote(v2, write_evidence(tmp_path / "e2.json", v2))
    monkeypatch.undo()
    assert reg.active_path.read_bytes() == before
    assert ModelRegistry(data).active("verifier") == v1
    assert not [p for p in reg.root.rglob("*.tmp")]


def test_unreadable_active_json_is_a_clear_error(reg):
    reg.root.mkdir(parents=True)
    reg.active_path.write_text("{truncated")
    assert _code(reg.active, "verifier") == "registry_unreadable"


def test_module_functions_use_pinny_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PINNY_DATA_DIR", str(tmp_path))
    d = make_model(tmp_path, "detector")
    assert [m["model_id"] for m in registry.list_models()] == [d]
    assert registry.get(d)["kind"] == "detector"
    registry.promote(d, write_evidence(tmp_path / "ev.json", d))
    assert registry.active("detector") == d
    assert json.loads((tmp_path / "models" / "active.json").read_text())["active"]["detector"]


def test_cli_promote_and_refusal(tmp_path):
    d = make_model(tmp_path, "detector")
    env = dict(os.environ, PINNY_DATA_DIR=str(tmp_path))
    run = lambda *a: subprocess.run([sys.executable, "-m", "pinny.models.registry", *a],  # noqa: E731
                                    cwd=REPO, env=env, capture_output=True, text=True)
    r = run("promote", d, "--evidence", str(write_evidence(tmp_path / "no.json", d,
                                                            promote=False)))
    assert r.returncode == 2 and "promotion_not_recommended" in r.stderr
    r = run("promote", d, "--evidence", str(write_evidence(tmp_path / "ok.json", d)))
    assert r.returncode == 0, r.stderr
    r = run("active")
    assert f"detector\t{d}" in r.stdout


def test_registry_and_viewer_import_without_torch():
    code = ("import sys, pinny.models.registry, pinny.viewer.server; "
            "assert 'torch' not in sys.modules, 'torch was imported'")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
