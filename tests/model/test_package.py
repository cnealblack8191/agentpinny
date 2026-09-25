"""Model packages: train, save/load round trip, integrity, signing, runtime,
promotion gate and CLI."""

from __future__ import annotations

import dataclasses
import json
import zipfile

import cv2
import numpy as np
import pytest

from pinny.detection import BoundingBox, ScanSettings
from pinny.detection.calibration import IsotonicCalibrator, PlattCalibrator
from pinny.detection.types import DetectionError
from pinny.detection.verifier import KnnVerifier, crop_with_margin
from pinny.model import (
    Decision,
    ModelPackage,
    ModelPackageError,
    PackageTemplate,
    promotion_check,
    train_model,
)
from pinny.model.__main__ import main as cli

KEY = b"test-signing-key"


def receptacle() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (8, 16), (8, 24), 0, 2)
    cv2.line(g, (16, 16), (16, 24), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def lookalike() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (6, 20), (18, 20), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def make_page(seed: int):
    """RGB page with 5 receptacles and 5 look-alikes at random quarter turns
    plus light noise. Returns (page, real_boxes, fake_boxes)."""
    rng = np.random.default_rng(seed)
    page = np.full((300, 600), 255, dtype=np.uint8)
    real, fake = [], []
    slots = [(40 + 110 * i, 40 + 130 * j) for j in range(2) for i in range(5)]
    for k, si in enumerate(rng.permutation(len(slots))):
        x, y = slots[si]
        glyph = receptacle() if k % 2 == 0 else lookalike()
        glyph = np.rot90(glyph, -int(rng.integers(0, 4))).copy()
        h, w = glyph.shape
        page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], glyph)
        (real if k % 2 == 0 else fake).append(BoundingBox(x, y, w, h))
    noise = rng.integers(-10, 11, page.shape)
    page = np.clip(page.astype(int) + noise, 0, 255).astype(np.uint8)
    return cv2.cvtColor(page, cv2.COLOR_GRAY2RGB), real, fake


def near(box: BoundingBox, boxes, tol: int = 3) -> bool:
    return any(abs(box.x - b.x) <= tol and abs(box.y - b.y) <= tol for b in boxes)


@pytest.fixture(scope="module")
def trained():
    pos, neg = [], []
    for seed in (1, 2):
        page, real, fake = make_page(seed)
        pos += [crop_with_margin(page, b) for b in real]
        neg += [crop_with_margin(page, b) for b in fake]
    page, real, _ = make_page(1)
    upright = next(b for b in real if b.height == 36)
    original = page[upright.y : upright.y2, upright.x : upright.x2].copy()
    pairs = [(0.95, True)] * 20 + [(0.85, False)] * 10 + [(0.9, True)] * 10
    return train_model(
        name="receptacles",
        version="1",
        symbol_class="duplex",
        original_template=original,
        positive_crops=pos,
        negative_crops=neg,
        review_pairs=pairs,
        scan_settings=ScanSettings(threshold=0.6),
        provenance={"dataset_sha256": "abc"},
        evaluation={"corpus_sha256": "c1", "ap": 0.9, "f1": 0.88, "recall": 0.92},
    )


def result_key(result):
    d = result.to_dict()
    d.pop("elapsed_seconds")
    return d


# --------------------------------------------------------------------------
# Training and runtime


def test_trained_model_accepts_receptacles_and_rejects_lookalikes(trained):
    pkg = trained.package
    assert pkg.decision.method == "verifier"
    assert pkg.verifier is not None and pkg.calibration is not None
    page, real, fake = make_page(7)
    result = pkg.detect(page)
    accepted = [d.candidate.box for d in result.accepted]
    assert len(accepted) == len(real)
    assert all(near(b, real) for b in accepted)
    assert not any(near(d.candidate.box, fake) for d in result.accepted)
    for d in result.detections:
        assert d.verifier_p is not None and d.probability is not None


def test_verifier_alone_rejects_lookalikes(trained):
    # Without the negative veto the look-alikes reach the verifier, which
    # must turn them down on its own.
    pkg = dataclasses.replace(trained.package, negatives=[])
    page, real, fake = make_page(7)
    result = pkg.detect(page)
    fakes = [d for d in result.detections if near(d.candidate.box, fake)]
    assert fakes and result.vetoed == 0
    assert not any(d.accepted for d in fakes)
    assert all(d.verifier_p < 0.5 for d in fakes)
    assert sum(d.accepted for d in result.detections) == len(real)


def test_result_dict_follows_contract_shape(trained):
    page, _, _ = make_page(7)
    d = trained.package.detect(page).to_dict(accepted_only=True)
    assert d["model"]["name"] == "receptacles" and d["decision"]["method"] == "verifier"
    det = d["detections"][0]
    for key in ("id", "box", "x", "y", "score", "rotation", "mirrored", "source", "verifier_p", "accepted"):
        assert key in det
    assert det["x"] == det["box"]["x"] + det["box"]["width"] / 2
    json.dumps(d)


def test_training_without_enough_examples_falls_back_to_score(trained):
    tpl = trained.package.templates[0].image
    t = train_model(name="m", version="1", symbol_class="duplex", original_template=tpl)
    assert t.package.verifier is None and t.package.calibration is None
    assert t.package.decision == Decision("score", ScanSettings().threshold)
    assert any("No verifier" in n for n in t.notes)
    assert t.threshold_suggestion.reason == "insufficient_labels"


def test_suggested_threshold_becomes_score_decision(trained):
    tpl = trained.package.templates[0].image
    pairs = [(0.95, True)] * 30 + [(0.82, False)] * 10
    t = train_model(name="m", version="1", symbol_class="duplex", original_template=tpl, review_pairs=pairs)
    assert t.package.decision == Decision("score", 0.95)


def test_detect_honours_search_region(trained):
    page, real, _ = make_page(7)
    b = real[0]
    region = BoundingBox(max(0, b.x - 30), max(0, b.y - 30), 100, 100)
    result = trained.package.detect(page, search_region=region)
    assert result.accepted and all(
        region.x <= d.candidate.box.x and d.candidate.box.x2 <= region.x2 for d in result.detections
    )


# --------------------------------------------------------------------------
# Save / load


def test_round_trip_gives_identical_results(trained, tmp_path):
    pkg = trained.package
    path = tmp_path / "m.pinny"
    digest = pkg.save(str(path))
    loaded = ModelPackage.load(str(path))
    assert loaded.package_sha256 == digest and not loaded.signed
    assert loaded.identity() == pkg.identity()
    assert loaded.decision == pkg.decision
    assert loaded.provenance["dataset_sha256"] == "abc"
    assert loaded.evaluation["ap"] == 0.9
    assert len(loaded.templates) == len(pkg.templates)
    for a, b in zip(loaded.templates, pkg.templates):
        assert np.array_equal(a.image, b.image) and a.support == b.support and a.is_original == b.is_original
    page, _, _ = make_page(11)
    r1, r2 = pkg.detect(page), loaded.detect(page)
    assert [d.candidate for d in r1.detections] == [d.candidate for d in r2.detections]
    assert np.allclose([d.verifier_p for d in r1.detections], [d.verifier_p for d in r2.detections], atol=1e-5)
    assert [d.probability for d in r1.detections] == [d.probability for d in r2.detections]


def test_save_is_deterministic(trained, tmp_path):
    a = trained.package.save(str(tmp_path / "a.pinny"))
    b = trained.package.save(str(tmp_path / "b.pinny"))
    assert a == b
    assert (tmp_path / "a.pinny").read_bytes() == (tmp_path / "b.pinny").read_bytes()


def test_machine_settings_are_not_saved(trained, tmp_path):
    m = trained.package.manifest()
    assert "num_threads" not in m["scan_settings"] and "search_region" not in m["scan_settings"]
    assert m["format"] == "pinny.model" and m["format_version"] == 1
    assert m["input"]["dpi"] == 200 and m["contracts_version"] == "1.1"


def test_rgb_template_round_trip_keeps_channel_order(tmp_path):
    img = np.zeros((20, 30, 3), np.uint8)
    img[:, :10] = (255, 0, 0)
    img[5:15, 12:20] = (0, 0, 255)
    pkg = ModelPackage("m", "1", "x", [PackageTemplate(img)])
    pkg.save(str(tmp_path / "m.pinny"))
    assert np.array_equal(ModelPackage.load(str(tmp_path / "m.pinny")).templates[0].image, img)


def test_platt_and_isotonic_calibration_round_trip(tmp_path):
    tpl = receptacle()
    for cal in (
        PlattCalibrator().fit([0.7, 0.8, 0.9, 0.95], [0, 0, 1, 1]),
        IsotonicCalibrator().fit([0.7, 0.8, 0.9, 0.95], [0, 1, 0, 1]),
    ):
        pkg = ModelPackage("m", "1", "x", [PackageTemplate(tpl)], calibration=cal,
                           decision=Decision("calibrated", 0.5))
        pkg.save(str(tmp_path / "c.pinny"))
        loaded = ModelPackage.load(str(tmp_path / "c.pinny"))
        s = [0.6, 0.75, 0.85, 0.99]
        assert np.allclose(loaded.calibration.predict(s), cal.predict(s))


# --------------------------------------------------------------------------
# Integrity and signing


def rewrite(src, dst, change):
    """Copy a package, letting ``change(entries)`` edit the entry dict."""
    with zipfile.ZipFile(src) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    change(entries)
    with zipfile.ZipFile(dst, "w") as zf:
        for n, data in entries.items():
            zf.writestr(n, data)


@pytest.fixture()
def saved(trained, tmp_path):
    path = tmp_path / "m.pinny"
    trained.package.save(str(path), signing_key=KEY)
    return path


def load_code(path, **kw):
    with pytest.raises(ModelPackageError) as e:
        ModelPackage.load(str(path), **kw)
    return e.value.code


def test_tampered_file_is_refused(saved, tmp_path):
    bad = tmp_path / "bad.pinny"
    rewrite(saved, bad, lambda e: e.__setitem__("templates/000.png", e["templates/000.png"][:-1] + b"x"))
    assert load_code(bad) == "corrupt_package"


def test_unlisted_and_missing_entries_are_refused(saved, tmp_path):
    bad = tmp_path / "extra.pinny"
    rewrite(saved, bad, lambda e: e.__setitem__("templates/999.png", b"x"))
    assert load_code(bad) == "corrupt_package"
    bad2 = tmp_path / "missing.pinny"
    rewrite(saved, bad2, lambda e: e.pop("verifier/labels.npy"))
    assert load_code(bad2) == "corrupt_package"


@pytest.mark.parametrize("name", ["../evil.png", "/etc/passwd", "templates/../../x.png", "run.py"])
def test_unexpected_entry_names_are_refused(saved, tmp_path, name):
    bad = tmp_path / "evil.pinny"
    rewrite(saved, bad, lambda e: e.__setitem__(name, b"x"))
    assert load_code(bad) == "invalid_entry"


def test_newer_format_version_is_refused(saved, tmp_path):
    def bump(e):
        m = json.loads(e["manifest.json"])
        m["format_version"] = 99
        e["manifest.json"] = json.dumps(m).encode()
        e.pop("signature.json")

    bad = tmp_path / "new.pinny"
    rewrite(saved, bad, bump)
    assert load_code(bad) == "unsupported_version"


def test_not_a_package(tmp_path):
    p = tmp_path / "x.pinny"
    p.write_bytes(b"not a zip")
    assert load_code(p) == "corrupt_package"
    assert load_code(tmp_path / "absent.pinny") == "package_not_found"


def test_signature_checks(saved, trained, tmp_path):
    assert ModelPackage.load(str(saved), verify_key=KEY).signed
    assert ModelPackage.load(str(saved)).signed  # readable without the key
    assert load_code(saved, verify_key=b"other-key") == "signature_invalid"
    unsigned = tmp_path / "u.pinny"
    trained.package.save(str(unsigned))
    assert load_code(unsigned, verify_key=KEY) == "signature_missing"


def test_edited_manifest_breaks_signature(saved, tmp_path):
    def edit(e):
        m = json.loads(e["manifest.json"])
        m["decision"]["threshold"] = 0.01
        e["manifest.json"] = json.dumps(m).encode()

    bad = tmp_path / "edited.pinny"
    rewrite(saved, bad, edit)
    assert load_code(bad, verify_key=KEY) == "signature_invalid"


def test_incompatible_feature_version_is_refused(saved, tmp_path, monkeypatch):
    monkeypatch.setattr(KnnVerifier, "FEATURE_VERSION", "hog64-intensity-v2")
    assert load_code(saved) == "incompatible_model"


# --------------------------------------------------------------------------
# Validation


def test_invalid_models_are_refused_before_saving(trained):
    tpl = receptacle()
    with pytest.raises(ModelPackageError) as e:
        ModelPackage("m", "1", "x", [PackageTemplate(tpl)], decision=Decision("verifier", 0.5)).validate()
    assert "fitted verifier" in str(e.value)
    with pytest.raises(ModelPackageError):
        ModelPackage("m", "1", "x", [PackageTemplate(tpl)], decision=Decision("score", 0.5)).validate()
    with pytest.raises(ModelPackageError):
        ModelPackage("m", "1", "x", []).validate()
    with pytest.raises(ModelPackageError):
        ModelPackage("", "1", "x", [PackageTemplate(tpl)]).validate()
    with pytest.raises(ModelPackageError):
        trained.package.detect(np.zeros((10, 10), np.float32))


def test_scan_settings_from_dict_round_trip_and_unknown_keys():
    s = ScanSettings(threshold=0.7, rotations=(0, 90), search_region=BoundingBox(1, 2, 30, 40), blur_sigma=0.5)
    assert ScanSettings.from_dict(s.to_dict()) == s
    with pytest.raises(DetectionError):
        ScanSettings.from_dict({**s.to_dict(), "future_knob": 1})


def test_verifier_state_round_trip_and_validation():
    v = KnnVerifier().fit([receptacle()], [lookalike()])
    feats, labels = v.export_state()
    w = KnnVerifier().load_state(feats, labels)
    crops = [receptacle(), lookalike()]
    assert np.allclose(v.score(crops), w.score(crops))
    with pytest.raises(DetectionError):
        KnnVerifier().load_state(feats, labels[:-1])
    with pytest.raises(DetectionError):
        KnnVerifier().export_state()


def test_onnx_verifier_is_bundled(tmp_path):
    pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    from pinny.detection.verifier import OnnxEmbeddingVerifier

    graph = helper.make_graph(
        [
            helper.make_node("AveragePool", ["x"], ["p"], kernel_shape=[4, 4], strides=[4, 4]),
            helper.make_node("Flatten", ["p"], ["y"], axis=1),
        ],
        "tiny",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [None, 3, 32, 32])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [None, 192])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    model_bytes = model.SerializeToString()
    v = OnnxEmbeddingVerifier(model_bytes, input_size=32, k=1, laplace=1.0, augment_rotations=False)
    v.fit([receptacle()], [lookalike()])
    pkg = ModelPackage("m", "1", "x", [PackageTemplate(receptacle())], verifier=v,
                       verifier_model_bytes=model_bytes, decision=Decision("verifier", 0.5))
    pkg.save(str(tmp_path / "o.pinny"))
    loaded = ModelPackage.load(str(tmp_path / "o.pinny"))
    assert isinstance(loaded.verifier, OnnxEmbeddingVerifier)
    crops = [receptacle(), lookalike()]
    assert np.allclose(loaded.verifier.score(crops), v.score(crops), atol=1e-5)
    assert loaded.manifest()["requires"]["optional"] == ["onnxruntime"]


# --------------------------------------------------------------------------
# Promotion gate


def test_promotion_rules(trained):
    cand = trained.package.manifest()
    assert promotion_check(cand).ok
    worse = {**cand, "evaluation": {"corpus_sha256": "c1", "ap": 0.95, "f1": 0.95, "recall": 0.95}}
    assert promotion_check(cand, worse).reasons[0].startswith("ap dropped")
    slightly = {**cand, "evaluation": {"corpus_sha256": "c1", "ap": 0.905, "f1": 0.88, "recall": 0.92}}
    assert promotion_check(cand, slightly).ok  # within max_drop
    other_corpus = {**cand, "evaluation": {"corpus_sha256": "c2", "ap": 0.1, "f1": 0.1, "recall": 0.1}}
    assert "different corpora" in promotion_check(cand, other_corpus).reasons[0]
    other_class = {**cand, "symbol_class": "gfci"}
    assert not promotion_check(cand, other_class).ok
    no_eval = {**cand, "evaluation": {}}
    assert not promotion_check(no_eval).ok


# --------------------------------------------------------------------------
# CLI


def test_cli(saved, tmp_path, monkeypatch, capsys):
    assert cli(["verify", str(saved)]) == 0
    monkeypatch.setenv("PINNY_MODEL_KEY", KEY.decode())
    assert cli(["verify", str(saved), "--key-env", "PINNY_MODEL_KEY"]) == 0
    monkeypatch.setenv("PINNY_MODEL_KEY", "wrong")
    assert cli(["verify", str(saved), "--key-env", "PINNY_MODEL_KEY"]) == 2
    capsys.readouterr()
    assert cli(["inspect", str(saved)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["name"] == "receptacles" and summary["verifier"] == "knn-hog" and summary["signed"]
    assert cli(["promote-check", str(saved)]) == 0
    assert cli(["promote-check", str(saved), str(saved)]) == 0
    assert cli(["bogus"]) == 64
