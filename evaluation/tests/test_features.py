"""Tests for F1/localization, tolerance modes, score curves, dpi/runtime, detector-assisted
references, score-corpus, the baseline gate and the from-points converter."""

import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, EVAL_ROOT)

from pinny_eval.cli import EXIT_REGRESSION, EXIT_REJECTED, EXIT_USAGE, main  # noqa: E402
from pinny_eval.curves import CurveInput, pr_curve  # noqa: E402
from pinny_eval.inputs import InputError  # noqa: E402
from pinny_eval.matching import Point, distance, eligible_pairs, match  # noqa: E402
from pinny_eval.tolerance import Tolerance  # noqa: E402

SYN = os.path.join(EVAL_ROOT, "fixtures", "synthetic")
MINI = os.path.join(EVAL_ROOT, "fixtures", "corpus_mini")
IDENTITY_ARGS = [
    "--document-id", "synthetic-doc-001",
    "--document-version", "sha256:synthetic-v1",
    "--page", "0", "--width", "1000", "--height", "800",
]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def load(path):
    with open(path) as fh:
        return json.load(fh)


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, name):
        return os.path.join(self.tmp, name)

    def score(self, case, *tol, det=None, gt=None, extra=()):
        det = det or os.path.join(SYN, case, "detections.json")
        gt = gt or os.path.join(SYN, case, "ground_truth.json")
        out = self.p("report.json")
        if os.path.exists(out):
            os.remove(out)
        code, stdout, err = run_cli(["score", "--detections", det, "--ground-truth", gt, *tol,
                                     *IDENTITY_ARGS, "--json-out", out, "--md-out", self.p("r.md"), *extra])
        rep = load(out) if os.path.exists(out) else None
        md = read_text(self.p("r.md")) if rep else None
        return code, rep, md, stdout, err


class PageReport(Tmp):
    def test_curve_fixture_matches_hand_computation(self):
        exp = load(os.path.join(SYN, "scored_curve", "expected.json"))
        code, rep, md, _, err = self.score("scored_curve", "--tolerance-px", "10")
        self.assertEqual(code, 0, err)
        cur = rep["curve"]
        self.assertTrue(cur["available"])
        got = [[p["threshold"], p["tp"], p["precision"], p["recall"]] for p in cur["points"]]
        self.assertEqual(len(got), len(exp["curve"]["points"]))
        for g, e in zip(got, exp["curve"]["points"]):
            self.assertEqual(g[:2], e[:2])
            self.assertAlmostEqual(g[2], e[2])
            self.assertAlmostEqual(g[3], e[3])
        self.assertAlmostEqual(cur["ap"], exp["curve"]["ap"])
        self.assertAlmostEqual(cur["ap"], 0.5416666666666666)
        self.assertEqual(cur["best_f1"]["threshold"], exp["curve"]["best_f1_threshold"])
        self.assertAlmostEqual(cur["best_f1"]["f1"], exp["curve"]["best_f1"])
        self.assertIsNone(cur["precision_at_recall_0_95"])
        r = cur["recall_at_precision_0_95"]
        self.assertEqual([r["threshold"], r["recall"]], exp["curve"]["recall_at_precision_0_95"])
        self.assertIn("all-point interpolated", cur["ap_method"])
        # The lowest-threshold point equals the full-set Hungarian result.
        self.assertEqual(cur["points"][-1]["tp"], rep["counts"]["true_positives"])
        self.assertIn("AP: **0.5417**", md)

    def test_localization_and_f1(self):
        exp = load(os.path.join(SYN, "scored_curve", "expected.json"))
        _, rep, md, _, _ = self.score("scored_curve", "--tolerance-px", "10")
        for k, v in exp["localization"].items():
            self.assertAlmostEqual(rep["localization"][k], v, msg=k)
        self.assertAlmostEqual(rep["metrics"]["f1"]["value"], 0.6)
        self.assertIn("| F1 | 0.6000 (6/10) |", md)
        self.assertIn("p95 distance px | 3.800", md)

    def test_f1_zero_denominator(self):
        code, rep, _, _, err = self.score("both_empty", "--tolerance-px", "10")
        self.assertEqual(code, 0, err)
        self.assertIsNone(rep["metrics"]["f1"]["value"])
        self.assertTrue(rep["metrics"]["f1"]["undefined_reason"])
        _, rep, _, _, _ = self.score("empty_predictions", "--tolerance-px", "10")
        self.assertEqual(rep["metrics"]["f1"]["value"], 0.0)

    def test_runtime_and_dpi_reported(self):
        _, rep, md, _, _ = self.score("scored_curve", "--tolerance-px", "10")
        self.assertEqual(rep["runtime"], {"elapsed_seconds": 1.25, "source_field": "elapsed_seconds"})
        self.assertEqual(rep["coordinate_frame"]["dpi"], 200)
        self.assertEqual(rep["dpi_check"]["note"], "all inputs declare 200 DPI")
        self.assertIn("Detector runtime: 1.25 s", md)

    def test_dpi_rules(self):
        d = load(os.path.join(SYN, "scored_curve", "detections.json"))
        del d["coordinate_frame"]["dpi"]
        det = write(self.p("d.json"), d)
        code, rep, _, _, err = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertEqual(code, 0, err)
        self.assertIn("not declared by detections", rep["dpi_check"]["note"])
        d["coordinate_frame"]["dpi"] = "200"
        write(det, d)
        code, _, _, _, err = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("dpi", err)

    def test_runtime_variants(self):
        d = load(os.path.join(SYN, "scored_curve", "detections.json"))
        del d["elapsed_seconds"]
        d["runtime"] = {"elapsed_seconds": 2.5, "peak_rss_mb": 300}
        det = write(self.p("d.json"), d)
        _, rep, _, _, _ = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertEqual(rep["runtime"]["elapsed_seconds"], 2.5)
        d["elapsed_seconds"] = 3
        write(det, d)
        code, _, _, _, err = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("disagree", err)
        d["runtime"] = {"elapsed_seconds": -1}
        del d["elapsed_seconds"]
        write(det, d)
        code, _, _, _, err = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertEqual(code, EXIT_REJECTED)

    def test_curve_omitted_without_confidence(self):
        d = load(os.path.join(SYN, "scored_curve", "detections.json"))
        del d["detections"][2]["confidence"]
        det = write(self.p("d.json"), d)
        code, rep, md, _, _ = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertEqual(code, 0)
        self.assertFalse(rep["curve"]["available"])
        self.assertIsNone(rep["curve"]["ap"])
        self.assertIn("1 of 6 detections have no confidence", md)
        self.assertEqual(rep["counts"]["true_positives"], 3)  # counts unaffected

    def test_score_field_accepted_as_confidence(self):
        d = load(os.path.join(SYN, "scored_curve", "detections.json"))
        for item in d["detections"]:
            item["score"] = item.pop("confidence")
        det = write(self.p("d.json"), d)
        _, rep, _, _, _ = self.score("scored_curve", "--tolerance-px", "10", det=det)
        self.assertAlmostEqual(rep["curve"]["ap"], 0.5416666666666666)


class ToleranceModes(Tmp):
    def test_relative_only_requires_boxes(self):
        code, _, _, _, err = self.score("scored_curve", "--tolerance-rel", "0.5")
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("needs a detection box", err)

    def test_px_fallback_with_relative(self):
        d = load(os.path.join(SYN, "relative_tolerance", "detections.json"))
        del d["detections"][1]["box"]  # d2 now falls back to 10 px and matches g2 (7 px)
        det = write(self.p("d.json"), d)
        code, rep, md, _, err = self.score("relative_tolerance", "--tolerance-px", "10", "--tolerance-rel", "0.5",
                                           det=det)
        self.assertEqual(code, 0, err)
        self.assertEqual(rep["counts"]["true_positives"], 3)
        tols = {m["prediction"]["id"]: m["tolerance_px"] for m in rep["matched"]}
        self.assertEqual(tols, {"d1": 5.0, "d2": 10.0, "d3": 20.0})
        self.assertEqual(rep["matching"]["tolerance_mode"], "rel_with_px_fallback")
        self.assertIn("10 px for detections without a box", md)

    def test_neither_tolerance_is_usage_error(self):
        code, _, _, _, _ = self.score("scored_curve")
        self.assertEqual(code, EXIT_USAGE)

    def test_bad_box_rejected(self):
        d = load(os.path.join(SYN, "relative_tolerance", "detections.json"))
        d["detections"][0]["box"]["width"] = 0
        det = write(self.p("d.json"), d)
        code, _, _, _, err = self.score("relative_tolerance", "--tolerance-rel", "0.5", det=det)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("width and height must be > 0", err)

    def test_tolerance_object(self):
        with self.assertRaises(ValueError):
            Tolerance()
        t = Tolerance(rel=0.5)
        with self.assertRaises(InputError):
            t.per_prediction([Point("a", 0, 0)], {})


class CurveCorrectness(unittest.TestCase):
    def test_incremental_matches_rematching_at_every_threshold(self):
        rng = random.Random(424242)
        for trial in range(150):
            pages, per_page = [], []
            for _ in range(rng.randint(1, 3)):
                preds = [Point(f"p{i}", rng.uniform(0, 60), rng.uniform(0, 60)) for i in range(rng.randint(0, 12))]
                refs = [Point(f"r{i}", rng.uniform(0, 60), rng.uniform(0, 60)) for i in range(rng.randint(0, 10))]
                conf = {p.id: rng.choice([0.1, 0.2, 0.3, 0.5, 0.7, 0.9, rng.random()]) for p in preds}
                tol = rng.choice([4.0, 9.0, 15.0])
                pages.append(CurveInput(conf, len(refs), eligible_pairs(preds, refs, tol)))
                per_page.append((preds, refs, conf, tol))
            curve = pr_curve(pages)
            n_refs = sum(len(r) for _, r, _, _ in per_page)
            with self.subTest(trial=trial):
                if n_refs == 0:
                    self.assertFalse(curve["available"])
                    continue
                for pt in curve["points"]:
                    t = pt["threshold"]
                    tp = sum(len(match([p for p in preds if conf[p.id] >= t], refs, tol))
                             for preds, refs, conf, tol in per_page)
                    n = sum(1 for _, _, conf, _ in per_page for c in conf.values() if c >= t)
                    self.assertEqual(pt["tp"], tp)
                    self.assertEqual(pt["detections"], n)
                # AP never exceeds the max recall and is in [0, 1].
                if curve["points"]:
                    self.assertLessEqual(curve["ap"], curve["points"][-1]["recall"] + 1e-12)

    def test_no_detections(self):
        c = pr_curve([CurveInput({}, 3, {})])
        self.assertTrue(c["available"])
        self.assertEqual(c["ap"], 0.0)
        self.assertEqual(c["points"], [])

    def test_perfect_detector_ap_is_one(self):
        refs = [Point(f"r{i}", i * 50.0, 10.0) for i in range(20)]
        preds = [Point(f"p{i}", i * 50.0 + 1, 10.0) for i in range(20)]
        conf = {p.id: 1.0 - i / 100 for i, p in enumerate(preds)}
        c = pr_curve([CurveInput(conf, len(refs), eligible_pairs(preds, refs, 5.0))])
        self.assertAlmostEqual(c["ap"], 1.0)
        self.assertAlmostEqual(c["precision_at_recall_0_95"]["precision"], 1.0)

    def test_scales_to_5k_detections(self):
        rng = random.Random(5)
        refs = [Point(f"r{i}", rng.uniform(0, 7200), rng.uniform(0, 4800)) for i in range(4000)]
        preds = [Point(f"p{i}", r.x + rng.uniform(-4, 4), r.y + rng.uniform(-4, 4))
                 for i, r in enumerate(refs[:3800])]
        preds += [Point(f"f{i}", rng.uniform(0, 7200), rng.uniform(0, 4800)) for i in range(1200)]
        conf = {p.id: rng.random() for p in preds}
        t0 = time.perf_counter()
        edges = eligible_pairs(preds, refs, 10.0)
        pairs = match(preds, refs, 10.0, edges_by_id=edges)
        c = pr_curve([CurveInput(conf, len(refs), edges)])
        elapsed = time.perf_counter() - t0
        self.assertEqual(c["points"][-1]["tp"], len(pairs))
        self.assertEqual(c["thresholds"], len(set(conf.values())))
        self.assertLess(elapsed, 30.0)


class GridEdges(unittest.TestCase):
    def test_grid_equals_brute_force(self):
        rng = random.Random(99)
        for trial in range(200):
            preds = [Point(f"p{i}", rng.uniform(-5, 80), rng.uniform(0, 80)) for i in range(rng.randint(0, 15))]
            refs = [Point(f"r{i}", rng.uniform(0, 80), rng.uniform(0, 80)) for i in range(rng.randint(0, 15))]
            tol = {p.id: rng.choice([0.0, 2.5, 7.0, 20.0]) for p in preds}
            got = eligible_pairs(preds, refs, tol)
            want = {(p.id, r.id): distance(p, r) for p in preds for r in refs if distance(p, r) <= tol[p.id]}
            with self.subTest(trial=trial):
                self.assertEqual(got, want)

    def test_zero_tolerance_exact_hits(self):
        preds = [Point("p", 10.0, 10.0)]
        refs = [Point("r", 10.0, 10.0), Point("s", 10.0, 10.5)]
        self.assertEqual(eligible_pairs(preds, refs, 0.0), {("p", "r"): 0.0})


class DetectorAssisted(Tmp):
    def _gt(self, **changes):
        g = load(os.path.join(SYN, "detector_assisted", "ground_truth.json"))
        g["dataset"]["verification"].update(changes)
        for k, v in list(g["dataset"]["verification"].items()):
            if v is ...:
                del g["dataset"]["verification"][k]
        return write(self.p("gt.json"), g)

    def test_caveat_is_prominent(self):
        code, rep, md, stdout, err = self.score("detector_assisted", "--tolerance-px", "10")
        self.assertEqual(code, 0, err)
        self.assertIn("recall may be overstated", rep["status"]["real_drawing_accuracy"])
        self.assertEqual(len(rep["status"]["caveats"]), 2)  # generic + seeded-from-this-scan
        head = md.split("## Identity")[0]
        self.assertIn("> **Caveat:** RECALL MAY BE OVERSTATED", head)
        self.assertIn("CAVEAT: RECALL MAY BE OVERSTATED", stdout)

    def test_other_scan_has_one_caveat(self):
        gt = self._gt(source_scan_id="some-other-scan")
        _, rep, _, _, _ = self.score("detector_assisted", "--tolerance-px", "10", gt=gt)
        self.assertEqual(len(rep["status"]["caveats"]), 1)

    def test_requirements(self):
        cases = {
            "must be false": dict(independent_of_detector=True),
            "exhaustive_miss_check must be true": dict(exhaustive_miss_check=...),
            "reviewed_by must name": dict(reviewed_by=None),
        }
        for needle, change in cases.items():
            with self.subTest(needle=needle):
                gt = self._gt(**change)
                code, _, _, _, err = self.score("detector_assisted", "--tolerance-px", "10", gt=gt)
                self.assertEqual(code, EXIT_REJECTED)
                self.assertIn(needle, err)

    def test_corrected_pins_still_rejected(self):
        d = load(os.path.join(SYN, "detector_assisted", "detections.json"))
        d["detections"][0]["source"] = "user_moved"
        det = write(self.p("d.json"), d)
        code, _, _, _, err = self.score("detector_assisted", "--tolerance-px", "10", det=det)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("manually corrected pins", err)
        d["detections"][0]["source"] = "detector"
        d["provenance"] = "corrected_pin_set"
        write(det, d)
        code, _, _, _, err = self.score("detector_assisted", "--tolerance-px", "10", det=det)
        self.assertEqual(code, EXIT_REJECTED)

    def test_other_statuses_still_need_independence(self):
        gt = self._gt(status="verified")
        code, _, _, _, err = self.score("detector_assisted", "--tolerance-px", "10", gt=gt)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("independent_of_detector must be true", err)

    def test_template_mentions_new_fields(self):
        t = load(os.path.join(EVAL_ROOT, "templates", "ground_truth.template.json"))
        v = t["dataset"]["verification"]
        self.assertIn("exhaustive_miss_check", v)
        self.assertEqual(t["coordinate_frame"]["dpi"], 200)
        code, _, err = run_cli(["validate", "--ground-truth",
                                os.path.join(EVAL_ROOT, "templates", "ground_truth.template.json")])
        self.assertEqual(code, EXIT_REJECTED)  # deliberately invalid until filled in


class Corpus(Tmp):
    MANIFEST = os.path.join(MINI, "manifest.json")
    BASELINE = os.path.join(MINI, "baseline.json")

    def corpus(self, *extra, manifest=None):
        out = self.p("corpus.json")
        if os.path.exists(out):
            os.remove(out)
        code, stdout, err = run_cli(["score-corpus", "--manifest", manifest or self.MANIFEST,
                                     "--json-out", out, "--md-out", self.p("corpus.md"), *extra])
        rep = load(out) if os.path.exists(out) else None
        md = read_text(self.p("corpus.md")) if rep else None
        return code, rep, md, err

    def test_pooled_equals_sum_of_pages(self):
        code, rep, md, err = self.corpus()
        self.assertEqual(code, 0, err)
        man = load(self.MANIFEST)
        self.assertEqual(rep["counts"]["pages"], len(man["pages"]))
        tp = fp = fn = 0
        for entry in man["pages"]:
            det = os.path.join(MINI, entry["detections"])
            gt = os.path.join(MINI, entry["ground_truth"])
            g = load(gt)
            doc = g["document"]
            out = self.p("page.json")
            code, _, e = run_cli(["score", "--detections", det, "--ground-truth", gt, "--tolerance-px", "10",
                                  "--document-id", doc["document_id"], "--document-version",
                                  doc["document_version"], "--page", str(doc["page_index"]),
                                  "--width", str(g["coordinate_frame"]["width"]),
                                  "--height", str(g["coordinate_frame"]["height"]), "--json-out", out])
            self.assertEqual(code, 0, e)
            c = load(out)["counts"]
            tp, fp, fn = tp + c["true_positives"], fp + c["false_positives"], fn + c["false_negatives"]
        c = rep["counts"]
        self.assertEqual((c["true_positives"], c["false_positives"], c["false_negatives"]), (tp, fp, fn))
        self.assertAlmostEqual(rep["micro"]["recall"]["value"], tp / (tp + fn))
        self.assertAlmostEqual(rep["micro"]["f1"]["value"], 2 * tp / (2 * tp + fp + fn))
        self.assertEqual(rep["curve"]["points"][-1]["tp"], tp)
        # Status: synthetic corpus is never real-drawing accuracy.
        self.assertEqual(rep["status"]["scope"], "synthetic_fixture")
        for section in ("## Pooled results", "## Pooled score curve", "## Per page", "## Per document",
                        "## Worst pages", "## Runtime", "## Localization", "## Per tag"):
            self.assertIn(section, md)

    def test_macro_worst_runtime_groups(self):
        _, rep, _, _ = self.corpus("--worst", "2")
        pages = rep["per_page"]
        f1s = [p["metrics"]["f1"]["value"] for p in pages if p["metrics"]["f1"]["value"] is not None]
        self.assertAlmostEqual(rep["macro"]["f1"]["value"], sum(f1s) / len(f1s))
        # The page with no references and no detections-that-match has undefined recall.
        self.assertGreaterEqual(rep["macro"]["recall"]["pages_excluded_undefined"], 1)
        self.assertEqual(len(rep["worst_pages"]), 2)
        self.assertLessEqual(rep["worst_pages"][0]["f1"], rep["worst_pages"][1]["f1"])
        rts = sorted(p["runtime_seconds"] for p in pages)
        self.assertEqual(rep["runtime"]["pages_with_runtime"], len(pages))
        self.assertAlmostEqual(rep["runtime"]["p50_seconds"], (rts[1] + rts[2]) / 2)
        docs = {r["key"]: r for r in rep["per_document"]}
        self.assertEqual(set(docs), {"doc-alpha", "doc-beta"})
        self.assertEqual(sum(r["true_positives"] for r in docs.values()), rep["counts"]["true_positives"])
        self.assertIn("scanned", {r["key"] for r in rep["per_tag"]})

    def test_committed_baseline_gate_passes(self):
        code, rep, md, err = self.corpus("--baseline", self.BASELINE, "--max-drop", "0.01")
        self.assertEqual(code, 0, err)
        self.assertTrue(rep["baseline_check"]["passed"])
        self.assertIn("**PASSED**", md)

    def test_regression_exit_code(self):
        b = load(self.BASELINE)
        b["metrics"]["recall"] = min(1.0, b["metrics"]["recall"] + 0.05)
        base = write(self.p("b.json"), b)
        code, rep, md, err = self.corpus("--baseline", base)
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIsNotNone(rep)  # reports are still written
        self.assertIn("REGRESSION: recall", err)
        self.assertIn("FAILED", md)
        # Within the margin passes.
        code, _, _, _ = self.corpus("--baseline", base, "--max-drop", "0.06")
        self.assertEqual(code, 0)

    def test_baseline_tolerance_mismatch_rejected(self):
        code, _, _, err = self.corpus("--baseline", self.BASELINE, "--tolerance-px", "10")
        self.assertEqual(code, 0, err)  # same value as the manifest: fine
        code, _, _, err = self.corpus("--tolerance-px", "12")
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("differs from the manifest", err)

    def test_write_baseline_roundtrip_and_report_as_baseline(self):
        wb = self.p("new_baseline.json")
        code, rep, _, _ = self.corpus("--write-baseline", wb)
        self.assertEqual(code, 0)
        self.assertEqual(load(wb), load(self.BASELINE))  # committed baseline is current
        shutil.copy(self.p("corpus.json"), self.p("prev_report.json"))
        code, _, _, _ = self.corpus("--baseline", self.p("prev_report.json"))  # a full report also works
        self.assertEqual(code, 0)

    def _manifest(self, mutate):
        m = load(self.MANIFEST)
        for e in m["pages"]:
            e["detections"] = os.path.join(MINI, e["detections"])
            e["ground_truth"] = os.path.join(MINI, e["ground_truth"])
        mutate(m)
        return write(self.p("m.json"), m)

    def test_manifest_rejections(self):
        cases = {
            "repeats pages[0]": lambda m: m["pages"].append(dict(m["pages"][0])),
            "do not describe the same page": lambda m: m["pages"][0].update(ground_truth=m["pages"][1]["ground_truth"]),
            "non-empty list": lambda m: m.update(pages=[]),
            "pinny.eval_manifest": lambda m: m.update(format="other"),
            "tolerance": lambda m: m.pop("tolerance_px"),
        }
        for needle, mutate in cases.items():
            with self.subTest(needle=needle):
                code, _, _, err = self.corpus(manifest=self._manifest(mutate))
                self.assertEqual(code, EXIT_REJECTED)
                self.assertIn(needle, err)

    def test_mixed_statuses_with_assisted_page(self):
        def add(m):
            m["pages"].append({"detections": os.path.join(SYN, "detector_assisted", "detections.json"),
                               "ground_truth": os.path.join(SYN, "detector_assisted", "ground_truth.json"),
                               "tags": ["assisted"]})
        code, rep, md, err = self.corpus(manifest=self._manifest(add))
        self.assertEqual(code, 0, err)
        self.assertEqual(rep["status"]["scope"], "mixed_reference")
        self.assertTrue(rep["status"]["caveats"][0].startswith("RECALL MAY BE OVERSTATED"))
        self.assertIn("> **Caveat:** RECALL MAY BE OVERSTATED", md.split("## Pooled")[0])


class CiGate(unittest.TestCase):
    def test_ci_command_from_repo_root(self):
        """The exact command documented for CI (README, "CI regression gate")."""
        import subprocess
        repo = os.path.dirname(EVAL_ROOT)
        env = dict(os.environ, PYTHONPATH="evaluation")
        proc = subprocess.run(
            [sys.executable, "-m", "pinny_eval", "score-corpus",
             "--manifest", "evaluation/fixtures/corpus_mini/manifest.json",
             "--baseline", "evaluation/fixtures/corpus_mini/baseline.json", "--max-drop", "0.01"],
            cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("**PASSED**", proc.stdout)


class ScoreBaseline(Tmp):
    def test_score_baseline(self):
        wb = self.p("b.json")
        code, rep, _, _, _ = self.score("scored_curve", "--tolerance-px", "10", extra=["--write-baseline", wb])
        self.assertEqual(code, 0)
        b = load(wb)
        self.assertAlmostEqual(b["metrics"]["ap"], 0.5416666666666666)
        self.assertAlmostEqual(b["metrics"]["recall"], 0.75)
        code, rep, _, _, _ = self.score("scored_curve", "--tolerance-px", "10", extra=["--baseline", wb])
        self.assertEqual(code, 0)
        b["metrics"]["f1"] = 0.62
        write(wb, b)
        code, rep, _, _, _ = self.score("scored_curve", "--tolerance-px", "10", extra=["--baseline", wb])
        self.assertEqual(code, EXIT_REGRESSION)
        failed = [c["metric"] for c in rep["baseline_check"]["checks"] if not c["passed"]]
        self.assertEqual(failed, ["f1"])
        code, _, _, _, err = self.score("scored_curve", "--tolerance-px", "9", extra=["--baseline", wb])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("tolerance", err)

    def test_ap_unavailable_fails_gate(self):
        wb = self.p("b.json")
        self.score("scored_curve", "--tolerance-px", "10", extra=["--write-baseline", wb])
        d = load(os.path.join(SYN, "scored_curve", "detections.json"))
        del d["detections"][0]["confidence"]
        det = write(self.p("d.json"), d)
        code, rep, _, _, _ = self.score("scored_curve", "--tolerance-px", "10", det=det, extra=["--baseline", wb])
        self.assertEqual(code, EXIT_REGRESSION)

    def test_bad_baseline_file(self):
        bad = write(self.p("bad.json"), {"format": "nope"})
        code, _, _, _, err = self.score("scored_curve", "--tolerance-px", "10", extra=["--baseline", bad])
        self.assertEqual(code, EXIT_REJECTED)


class FromPoints(Tmp):
    def test_convert_and_validate(self):
        csv_path = self.p("pts.csv")
        with open(csv_path, "w") as fh:
            fh.write("x,y,page,document,id\n10,20,0,plan-A,\n30,40,0,plan-A,s7\n5,5,1,plan-A,\n50,60,0,plan B,\n")
        out = self.p("out")
        code, stdout, err = run_cli(["from-points", "--csv", csv_path, "--out-dir", out, "--dataset-id", "fpcad",
                                     "--labeled-by", "FloorPlanCAD", "--document-version", "sha256:x",
                                     "--width", "200", "--height", "200", "--scale", "2"])
        self.assertEqual(code, 0, err)
        files = sorted(os.listdir(out))
        self.assertEqual(files, ["plan-A_p0.ground_truth.json", "plan-A_p1.ground_truth.json",
                                 "plan_B_p0.ground_truth.json"])
        g = load(os.path.join(out, "plan-A_p0.ground_truth.json"))
        self.assertEqual([(r["id"], r["x"], r["y"]) for r in g["receptacles"]],
                         [("r0001", 20.0, 40.0), ("s7", 60.0, 80.0)])
        self.assertTrue(g["dataset"]["verification"]["independent_of_detector"])
        self.assertEqual(g["dataset"]["verification"]["status"], "unverified")
        code, stdout, _ = run_cli(["validate", "--ground-truth", os.path.join(out, "plan_B_p0.ground_truth.json")])
        self.assertEqual(code, 0)

    def test_convert_rejections(self):
        csv_path = self.p("pts.csv")
        with open(csv_path, "w") as fh:
            fh.write("x,y,document\n1,2,a\n")
        code, _, err = run_cli(["from-points", "--csv", csv_path, "--out-dir", self.p("o"), "--dataset-id", "d",
                                "--labeled-by", "x", "--document-version", "v", "--width", "10", "--height", "10"])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("missing CSV column", err)
        with open(csv_path, "w") as fh:
            fh.write("x,y,page,document\n1,2,0,a\n")
        code, _, err = run_cli(["from-points", "--csv", csv_path, "--out-dir", self.p("o"), "--dataset-id", "d",
                                "--labeled-by", "x", "--width", "10", "--height", "10"])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("document_version", err)
        with open(csv_path, "w") as fh:
            fh.write("x,y,page,document\n11,2,0,a\n")
        code, _, err = run_cli(["from-points", "--csv", csv_path, "--out-dir", self.p("o"), "--dataset-id", "d",
                                "--labeled-by", "x", "--document-version", "v", "--width", "10", "--height", "10"])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("outside the", err)


if __name__ == "__main__":
    unittest.main()
