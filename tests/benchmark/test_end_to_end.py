"""End to end: tiny synthetic dataset, fake P6 models, CLI run and promote-report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pinny.benchmark import cli
from pinny.benchmark.dataset import load_test_split

from .helpers import (FakePointDetector, FakeVerifier, build_dataset, page_rgb, raster_key,
                      write_fake_model)

SPURIOUS = {"x": 20.0, "y": 20.0, "score": 0.6}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ds = tmp_path / "dataset"
    manifest = build_dataset(ds)
    test_entries = [e for e in manifest["detector"] if e["split"] == "test"]
    all_points = [[p["x"], p["y"]] for e in test_entries for p in e["points"]]
    verifier_dir = write_fake_model(tmp_path / "verifier", "verifier",
                                    "verifier-20260924T000000Z-0badc0de", 0.5, {"points": all_points})
    pages = {}
    for e in test_entries:
        pts = [{"x": p["x"] + 1.5, "y": p["y"] - 1.0, "score": 0.9} for p in e["points"]]
        if e["page_key"] == "doc-b-p0":
            pts = pts[1:]           # one miss
        if e["page_key"] == "doc-a-p0":
            pts.append(SPURIOUS)    # one false positive
        pages[raster_key(page_rgb(ds, e))] = pts
    detector_dir = write_fake_model(tmp_path / "detector", "detector",
                                    "detector-20260924T000000Z-feedf00d", 0.5, {"pages": pages})
    monkeypatch.setattr(cli, "load_verifier", FakeVerifier.load)
    monkeypatch.setattr(cli, "load_detector", FakePointDetector.load)
    return tmp_path, ds, verifier_dir, detector_dir, test_entries


def _run(tmp_path, ds, verifier_dir, detector_dir, out_name="out", extra=()):
    out = tmp_path / out_name
    code = cli.main(["run", "--dataset", str(ds), "--modes", "template,template+verifier,model",
                     "--verifier", str(verifier_dir), "--detector", str(detector_dir),
                     "--out", str(out), "--template-threshold", "0.3", *extra])
    return code, out


def test_run_writes_scored_detections_and_summary(setup):
    tmp_path, ds, verifier_dir, detector_dir, test_entries = setup
    code, out = _run(tmp_path, ds, verifier_dir, detector_dir)
    assert code == 0
    summary = json.loads((out / "summary.json").read_text())
    md = (out / "summary.md").read_text()

    # Test split only: the train document is never scored.
    assert summary["split"] == "test"
    assert summary["page_count"] == len(test_entries) == 4
    assert summary["document_count"] == 3
    assert summary["reference_points"] == sum(len(e["points"]) for e in test_entries) == 10
    assert summary["tolerance_px"] == 12
    assert summary["dataset"]["dataset_id"] == "synthetic-test-dataset"
    assert summary["dataset"]["synthetic_only"] is True
    assert summary["label"] == "synthetic — not real-drawing accuracy"
    assert "synthetic — not real-drawing accuracy" in md
    assert "synthetic-test-dataset" in md and "12 px" in md

    # The template rule is recorded, with the box for every page.
    tc = summary["template_choice"]
    assert "first test point" in tc["rule"] and tc["size_px"] == 40
    assert tc["per_page"]["doc-c-p0"] == {"x": 220, "y": 140, "width": 40, "height": 40}
    assert "first test point" in md

    modes = summary["modes"]
    assert set(modes) == {"template", "template+verifier", "model"}
    assert modes["template+verifier"]["model_ids"] == {"verifier": "verifier-20260924T000000Z-0badc0de"}
    assert modes["model"]["model_ids"] == {"detector": "detector-20260924T000000Z-feedf00d"}

    for mode, res in modes.items():
        assert [p["page_key"] for p in res["pages"]] == ["doc-a-p0", "doc-a-p1", "doc-b-p0", "doc-c-p0"]
        # Summed over the per-page evaluator reports.
        for key in ("true_positives", "false_positives", "false_negatives"):
            total = 0
            for p in res["pages"]:
                report = json.loads((out / p["report_file"]).read_text())
                assert report["format"] == "pinny.evaluation_report"
                assert report["matching"]["tolerance_px"] == 12
                assert report["detector_provenance"]["mode"] == mode
                total += report["counts"][key]
            assert res["counts"][key] == total
        c = res["counts"]
        tp, fp, fn = c["true_positives"], c["false_positives"], c["false_negatives"]
        assert tp + fn == 10
        if tp + fp:
            assert res["metrics"]["precision"]["value"] == tp / (tp + fp)
        assert res["metrics"]["recall"]["value"] == tp / 10

    # One pinny.detections v1 file per page and mode.
    for mode in modes:
        files = sorted((out / mode).glob("*.detections.json"))
        assert len(files) == 4
        for f in files:
            d = json.loads(f.read_text())
            assert d["format"] == "pinny.detections" and d["format_version"] == 1
            assert d["provenance"] == "original_detector_output"
            assert d["mode"] == mode

    # The low template threshold produces false positives that the verifier removes.
    t, tv, m = modes["template"]["counts"], modes["template+verifier"]["counts"], modes["model"]["counts"]
    assert t["false_positives"] > 0
    assert tv["false_positives"] < t["false_positives"]
    assert tv["true_positives"] == t["true_positives"]
    # Every template candidate is either kept or recorded as suppressed.
    assert tv["predictions"] + modes["template+verifier"]["suppressed"] == t["predictions"]
    tv_file = json.loads((out / "template+verifier" / "doc-a-p0.detections.json").read_text())
    assert tv_file["detector"]["name"] == "opencv-template+verifier"
    assert tv_file["detector"]["version"] == "verifier-20260924T000000Z-0badc0de"
    for d in tv_file["detections"]:
        assert d["score"] == d["verifier_score"] and "template_score" in d
    # Model mode: one miss, one false positive, 40 px boxes centred on the points.
    assert (m["true_positives"], m["false_positives"], m["false_negatives"]) == (9, 1, 1)
    m_file = json.loads((out / "model" / "doc-a-p0.detections.json").read_text())
    assert m_file["detector"]["name"] == "pinny-point-detector"
    for d in m_file["detections"]:
        assert d["box"]["width"] == d["box"]["height"] == 40 and d["rotation"] == 0
        assert d["box"]["x"] + 20 == d["x"] and d["box"]["y"] + 20 == d["y"]


def test_promote_report_on_synthetic_never_promotes(setup, capsys):
    tmp_path, ds, verifier_dir, detector_dir, _ = setup
    code, out = _run(tmp_path, ds, verifier_dir, detector_dir)
    assert code == 0
    for candidate in ("template+verifier", "model"):
        report_path = tmp_path / f"promotion-{candidate}.json"
        md_path = tmp_path / f"promotion-{candidate}.md"
        code = cli.main(["promote-report", "--benchmark", str(out), "--candidate", candidate,
                         "--out", str(report_path), "--md-out", str(md_path)])
        assert code == 0
        r = json.loads(report_path.read_text())
        assert r["format"] == "pinny.promotion" and r["promote"] is False
        assert "not_synthetic_only" in r["failed_conditions"]
        assert "min_reference_points" in r["failed_conditions"]
        assert "min_test_documents" not in r["failed_conditions"]
        assert r["dataset_id"] == "synthetic-test-dataset" and r["tolerance_px"] == 12
        assert r["benchmark"]["sha256"]
        assert "synthetic — not real-drawing accuracy" in md_path.read_text()
    # The verifier drops the template's false positives with no recall loss, so on
    # a large enough real test split this candidate would pass the metric conditions.
    r = json.loads((tmp_path / "promotion-template+verifier.json").read_text())
    by = {c["name"]: c for c in r["conditions"]}
    assert by["recall_not_below_baseline"]["passed"] and by["precision_within_margin"]["passed"]


def test_real_dataset_is_unverified_by_default(tmp_path, monkeypatch):
    ds = tmp_path / "dataset"
    build_dataset(ds, synthetic=False)
    out = tmp_path / "out"
    assert cli.main(["run", "--dataset", str(ds), "--modes", "template", "--out", str(out)]) == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["reference_status"] == "unverified"
    assert summary["dataset"]["synthetic_only"] is False
    gt = json.loads(next((out / "reference").glob("*.json")).read_text())
    assert gt["dataset"]["verification"]["status"] == "unverified"


def test_run_rejects_missing_model_and_non_empty_out(setup, capsys):
    tmp_path, ds, _, _, _ = setup
    code = cli.main(["run", "--dataset", str(ds), "--modes", "template+verifier",
                     "--out", str(tmp_path / "o1")])
    assert code == cli.EXIT_REJECTED
    assert "missing_model" in capsys.readouterr().err
    occupied = tmp_path / "o2"
    occupied.mkdir()
    (occupied / "x").write_text("keep")
    code = cli.main(["run", "--dataset", str(ds), "--modes", "template", "--out", str(occupied)])
    assert code == cli.EXIT_REJECTED
    assert (occupied / "x").read_text() == "keep"
    assert cli.main(["run", "--dataset", str(ds), "--modes", "bogus", "--out", "x"]) == cli.EXIT_USAGE


def test_split_loader_ignores_other_splits(tmp_path):
    ds = tmp_path / "dataset"
    build_dataset(ds)
    split = load_test_split(ds)
    assert all(p.document_id != "doc-t" for p in split.pages)
    rgb = split.pages[0].load_rgb()
    assert rgb.shape == (split.pages[0].height, split.pages[0].width, 3)


def test_benchmark_import_does_not_need_torch():
    import subprocess

    code = ("import sys, pinny.benchmark, pinny.benchmark.cli; "
            "sys.exit(1 if 'torch' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2]).returncode == 0
