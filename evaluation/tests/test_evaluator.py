"""Evaluator self-tests. These validate the evaluator, not the detector."""

import io
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, EVAL_ROOT)

from pinny_eval.cli import EXIT_REJECTED, EXIT_USAGE, main  # noqa: E402
from pinny_eval.matching import Point, distance, match  # noqa: E402

FIXTURES = os.path.join(EVAL_ROOT, "fixtures", "synthetic")
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


def score_fixture(name, tol=None, extra=None):
    case = os.path.join(FIXTURES, name)
    with open(os.path.join(case, "expected.json")) as fh:
        expected = json.load(fh)
    with tempfile.TemporaryDirectory() as tmp:
        out_json = os.path.join(tmp, "report.json")
        argv = [
            "score",
            "--detections", os.path.join(case, "detections.json"),
            "--ground-truth", os.path.join(case, "ground_truth.json"),
            "--tolerance-px", str(tol if tol is not None else expected["tolerance_px"]),
            *IDENTITY_ARGS, *(extra or []),
            "--json-out", out_json,
        ]
        code, out, err = run_cli(argv)
        report = None
        if code == 0:
            with open(out_json) as fh:
                report = json.load(fh)
    return expected, code, report, err


def fixture_names(kind):
    names = sorted(os.listdir(FIXTURES))
    out = []
    for n in names:
        with open(os.path.join(FIXTURES, n, "expected.json")) as fh:
            exp = json.load(fh)
        if ("reject" in exp) == (kind == "reject"):
            out.append(n)
    return out


class FixtureScoring(unittest.TestCase):
    def test_score_fixtures(self):
        names = fixture_names("score")
        self.assertGreaterEqual(len(names), 10)
        for name in names:
            with self.subTest(fixture=name):
                exp, code, rep, err = score_fixture(name)
                self.assertEqual(code, 0, err)
                c = rep["counts"]
                self.assertEqual(
                    (c["true_positives"], c["false_positives"], c["false_negatives"]),
                    (exp["counts"]["tp"], exp["counts"]["fp"], exp["counts"]["fn"]),
                )
                for metric in ("precision", "recall"):
                    got = rep["metrics"][metric]
                    if exp[metric] is None:
                        self.assertIsNone(got["value"])
                        self.assertTrue(got["undefined_reason"])
                    else:
                        self.assertAlmostEqual(got["value"], exp[metric])
                        self.assertIsNone(got["undefined_reason"])
                self.assertEqual(
                    sorted([m["prediction"]["id"], m["reference"]["id"]] for m in rep["matched"]),
                    sorted(exp["matched"]),
                )
                self.assertEqual([f["id"] for f in rep["false_positives"]], exp["false_positives"])
                self.assertEqual([f["id"] for f in rep["false_negatives"]], exp["false_negatives"])
                # Synthetic data must never be presented as real-drawing accuracy.
                self.assertEqual(rep["status"]["real_drawing_accuracy"], "Not measured — verified reference pending")
                self.assertEqual(rep["status"]["scope"], "synthetic_fixture")
                self.assertEqual(rep["matching"]["tolerance_px"], exp["tolerance_px"])
                self.assertEqual(rep["dataset"]["dataset_id"], f"synthetic-{name}")
                self.assertEqual(rep["detector_provenance"]["scan_id"], f"synthetic-scan-{name}")

    def test_reject_fixtures(self):
        names = fixture_names("reject")
        self.assertGreaterEqual(len(names), 5)
        for name in names:
            with self.subTest(fixture=name):
                exp, code, rep, err = score_fixture(name)
                self.assertEqual(code, EXIT_REJECTED)
                self.assertIsNone(rep)
                self.assertIn(exp["reject"], err)

    def test_duplicate_reason_is_reported(self):
        _, _, rep, _ = score_fixture("duplicates")
        for fp in rep["false_positives"]:
            self.assertTrue(fp["reason"].startswith("duplicate_or_crowded"))
            self.assertEqual(fp["references_within_tolerance"][0]["matched_to"], "d1")

    def test_tolerance_changes_result_and_is_recorded(self):
        _, code, rep, _ = score_fixture("misses", tol=9.99)
        self.assertEqual(code, 0)
        self.assertEqual(rep["counts"]["true_positives"], 1)  # the exact-10px pair drops out
        self.assertEqual(rep["matching"]["tolerance_px"], 9.99)


class CliGuards(unittest.TestCase):
    def _case(self, name):
        return os.path.join(FIXTURES, name)

    def test_tolerance_is_required(self):
        c = self._case("perfect")
        code, _, _ = run_cli(["score", "--detections", f"{c}/detections.json",
                              "--ground-truth", f"{c}/ground_truth.json", *IDENTITY_ARGS])
        self.assertEqual(code, EXIT_USAGE)

    def test_negative_tolerance_rejected(self):
        c = self._case("perfect")
        code, _, _ = run_cli(["score", "--detections", f"{c}/detections.json",
                              "--ground-truth", f"{c}/ground_truth.json", "--tolerance-px", "-1", *IDENTITY_ARGS])
        self.assertEqual(code, EXIT_USAGE)

    def test_expected_identity_mismatch_rejected(self):
        c = self._case("perfect")
        args = list(IDENTITY_ARGS)
        args[args.index("--width") + 1] = "999"
        code, _, err = run_cli(["score", "--detections", f"{c}/detections.json",
                                "--ground-truth", f"{c}/ground_truth.json", "--tolerance-px", "10", *args])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("expected 999x800", err)

    def test_pending_reports_not_measured(self):
        c = self._case("perfect")
        code, out, _ = run_cli(["pending", "--detections", f"{c}/detections.json", *IDENTITY_ARGS])
        self.assertEqual(code, 0)
        self.assertIn("Not measured — verified reference pending", out)
        self.assertNotIn("Precision", out)

    def test_validate(self):
        c = self._case("perfect")
        code, out, _ = run_cli(["validate", "--ground-truth", f"{c}/ground_truth.json"])
        self.assertEqual(code, 0)
        self.assertIn("verification 'synthetic'", out)

    def _write(self, tmp, obj):
        path = os.path.join(tmp, "f.json")
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def test_malformed_inputs_rejected(self):
        with open(os.path.join(self._case("perfect"), "detections.json")) as fh:
            base = json.load(fh)
        mutations = {
            "outside the": lambda d: d["detections"][0].update(x=1000.5),
            "duplicate id": lambda d: d["detections"][1].update(id="d1"),
            "finite number": lambda d: d["detections"][0].update(y="12"),
            "only 'canonical_raster_px'": lambda d: d["coordinate_frame"].update(space="pdf_points"),
            "origin 'top-left'": lambda d: d["coordinate_frame"].update(y_axis="up"),
            "missing required field 'settings'": lambda d: d["detector"].pop("settings"),
            "unknown source": lambda d: d["detections"][0].update(source="import"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for needle, mutate in mutations.items():
                with self.subTest(needle=needle):
                    d = json.loads(json.dumps(base))
                    mutate(d)
                    path = self._write(tmp, d)
                    code, _, err = run_cli(["pending", "--detections", path, *IDENTITY_ARGS])
                    self.assertEqual(code, EXIT_REJECTED)
                    self.assertIn(needle, err)

    def test_verified_reference_is_labelled_measured(self):
        with open(os.path.join(self._case("perfect"), "ground_truth.json")) as fh:
            g = json.load(fh)
        g["dataset"]["verification"]["status"] = "verified"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, g)
            c = self._case("perfect")
            code, out, _ = run_cli(["score", "--detections", f"{c}/detections.json", "--ground-truth", path,
                                    "--tolerance-px", "10", *IDENTITY_ARGS])
        self.assertEqual(code, 0)
        self.assertIn("Measured against verified reference", out)

    def test_module_entrypoint(self):
        proc = subprocess.run([sys.executable, "-m", "pinny_eval", "--help"], cwd=EVAL_ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("score", proc.stdout)


def brute_force(preds, refs, tol):
    """Best (cardinality, -total distance) over all one-to-one matchings."""
    best = (0, 0.0)
    n = len(preds)
    for k in range(min(n, len(refs)), 0, -1):
        found = False
        for ps in itertools.combinations(range(n), k):
            for rs in itertools.permutations(range(len(refs)), k):
                ds = [distance(preds[a], refs[b]) for a, b in zip(ps, rs)]
                if all(d <= tol for d in ds):
                    total = sum(ds)
                    if not found or total < best[1]:
                        best = (k, total)
                        found = True
        if found:
            return best
    return best


class MatcherProperties(unittest.TestCase):
    def test_against_brute_force(self):
        rng = random.Random(20260923)
        for trial in range(400):
            np_, nr = rng.randint(0, 5), rng.randint(0, 5)
            preds = [Point(f"p{i}", rng.uniform(0, 40), rng.uniform(0, 40)) for i in range(np_)]
            refs = [Point(f"r{i}", rng.uniform(0, 40), rng.uniform(0, 40)) for i in range(nr)]
            tol = rng.choice([3.0, 8.0, 15.0])
            pairs = match(preds, refs, tol)
            with self.subTest(trial=trial):
                self.assertEqual(len({p.prediction_id for p in pairs}), len(pairs))
                self.assertEqual(len({p.reference_id for p in pairs}), len(pairs))
                self.assertTrue(all(p.distance <= tol for p in pairs))
                k, total = brute_force(preds, refs, tol)
                self.assertEqual(len(pairs), k)
                self.assertAlmostEqual(sum(p.distance for p in pairs), total, places=9)

    def test_order_independent(self):
        rng = random.Random(7)
        preds = [Point(f"p{i}", rng.uniform(0, 200), rng.uniform(0, 200)) for i in range(60)]
        refs = [Point(f"r{i}", rng.uniform(0, 200), rng.uniform(0, 200)) for i in range(60)]
        a = match(preds, refs, 12.0)
        b = match(list(reversed(preds)), list(reversed(refs)), 12.0)
        self.assertEqual(a, b)

    def test_scales_to_a_dense_page(self):
        rng = random.Random(11)
        preds = [Point(f"p{i}", rng.uniform(0, 7000), rng.uniform(0, 5000)) for i in range(1500)]
        refs = [Point(f"r{i}", rng.uniform(0, 7000), rng.uniform(0, 5000)) for i in range(1500)]
        pairs = match(preds, refs, 25.0)
        self.assertEqual(len({p.reference_id for p in pairs}), len(pairs))


if __name__ == "__main__":
    unittest.main()
