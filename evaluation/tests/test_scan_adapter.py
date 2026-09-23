"""Tests for the contracts section 4 scan adapter and the frame `dpi` field."""

import ast
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, EVAL_ROOT)

from pinny_eval.cli import EXIT_REJECTED, main  # noqa: E402
from pinny_eval.inputs import InputError  # noqa: E402
from pinny_eval.scan_adapter import scan_result_to_detections  # noqa: E402

CASE = os.path.join(EVAL_ROOT, "fixtures", "contracts", "scan_result")
IDENTITY_ARGS = [
    "--document-id", "0f8e2c1a-5b7d-4e3a-9c61-2d4b8f0a7e15",
    "--document-version", "sha256:5d41402abc4b2a76b9719d911017c592aaf1e1b2c3d4e5f60718293a4b5c6d7e",
    "--page", "2", "--width", "1000", "--height", "800",
]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def load(name):
    with open(os.path.join(CASE, name)) as fh:
        return json.load(fh)


class ScanAdapter(unittest.TestCase):
    def setUp(self):
        self.scan = load("scan_result.json")
        self.expected = load("expected.json")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def _write(self, name, obj):
        path = self._path(name)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def test_mapping(self):
        out = scan_result_to_detections(self.scan)
        self.assertEqual(out["format"], "pinny.detections")
        self.assertEqual(out["format_version"], 1)
        self.assertEqual(out["provenance"], "original_detector_output")
        for key in ("scan_id", "document", "coordinate_frame", "template", "detector", "created_at"):
            self.assertEqual(out[key], self.scan[key])
        for got, orig in zip(out["detections"], self.scan["detections"]):
            self.assertEqual([got["x"], got["y"]], self.expected["points"][got["id"]])
            self.assertEqual(got["confidence"], self.expected["confidence"][got["id"]])
            self.assertEqual(got["score"], orig["score"])  # raw score kept as well
            self.assertEqual(got["box"], orig["box"])
            self.assertEqual(got["rotation"], orig["rotation"])
            self.assertEqual(got["source"], "detector")

    def test_does_not_mutate_input(self):
        before = copy.deepcopy(self.scan)
        scan_result_to_detections(self.scan)
        self.assertEqual(self.scan, before)

    def test_adapt_then_score(self):
        out = self._path("detections.json")
        code, stdout, err = run_cli(["adapt-scan", "--scan", os.path.join(CASE, "scan_result.json"), "--out", out])
        self.assertEqual(code, 0, err)
        self.assertIn("3 detections", stdout)
        self.assertIn("at 200 DPI", stdout)

        rep_path = self._path("report.json")
        md_path = self._path("report.md")
        code, _, err = run_cli([
            "score", "--detections", out, "--ground-truth", os.path.join(CASE, "ground_truth.json"),
            "--tolerance-px", str(self.expected["tolerance_px"]), *IDENTITY_ARGS,
            "--json-out", rep_path, "--md-out", md_path,
        ])
        self.assertEqual(code, 0, err)  # scan frame has dpi 200, reference omits it: same frame
        with open(rep_path) as fh:
            rep = json.load(fh)
        c = rep["counts"]
        self.assertEqual(
            [c["true_positives"], c["false_positives"], c["false_negatives"]],
            [self.expected["counts"][k] for k in ("tp", "fp", "fn")],
        )
        self.assertEqual(
            sorted([m["prediction"]["id"], m["reference"]["id"]] for m in rep["matched"]),
            self.expected["matched"],
        )
        self.assertEqual([f["id"] for f in rep["false_positives"]], self.expected["false_positives"])
        self.assertEqual([f["id"] for f in rep["false_negatives"]], self.expected["false_negatives"])
        self.assertEqual(rep["coordinate_frame"]["dpi"], 200)
        prov = rep["detector_provenance"]
        self.assertEqual(prov["template"], self.scan["template"])
        self.assertEqual(prov["created_at"], self.scan["created_at"])
        self.assertEqual(prov["detector"], self.scan["detector"])
        self.assertEqual(rep["status"]["real_drawing_accuracy"], "Not measured — verified reference pending")
        with open(md_path) as fh:
            md = fh.read()
        self.assertIn("at 200 DPI", md)
        self.assertIn("Scan created: 2026-09-23T12:00:00Z", md)

    def test_rejections(self):
        def mutate(fn):
            s = copy.deepcopy(self.scan)
            fn(s)
            return s

        cases = {
            "does not equal the box center": mutate(lambda s: s["detections"][0].update(x=121.0)),
            "source is 'manual'": mutate(lambda s: s["detections"][1].update(source="manual")),
            "source is None": mutate(lambda s: s["detections"][1].pop("source")),
            "missing 'score'": mutate(lambda s: s["detections"][2].pop("score")),
            "box width and height must be > 0": mutate(lambda s: s["detections"][2]["box"].update(width=0)),
            "missing 'box'": mutate(lambda s: s["detections"][2].pop("box")),
            "missing required field 'detector'": mutate(lambda s: s.pop("detector")),
            "already has 'format'/'provenance'": mutate(lambda s: s.update(provenance="corrected_pins")),
        }
        for needle, scan in cases.items():
            with self.subTest(needle=needle):
                with self.assertRaises(InputError) as ctx:
                    scan_result_to_detections(scan)
                self.assertIn(needle, str(ctx.exception))

    def test_existing_detections_file_is_not_re_adapted(self):
        path = os.path.join(EVAL_ROOT, "fixtures", "synthetic", "perfect", "detections.json")
        out = self._path("out.json")
        code, _, err = run_cli(["adapt-scan", "--scan", path, "--out", out])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("already has 'format'", err)
        self.assertFalse(os.path.exists(out))

    def test_invalid_output_leaves_no_file(self):
        bad = copy.deepcopy(self.scan)
        bad["coordinate_frame"]["dpi"] = 300  # adapts fine, fails the detections loader
        scan = self._write("scan.json", bad)
        out = self._path("out.json")
        code, _, err = run_cli(["adapt-scan", "--scan", scan, "--out", out])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("dpi is 300", err)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["scan.json"])

    def test_refuses_to_overwrite_without_flag(self):
        out = self._write("out.json", {"keep": True})
        scan = os.path.join(CASE, "scan_result.json")
        code, _, err = run_cli(["adapt-scan", "--scan", scan, "--out", out])
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("already exists", err)
        with open(out) as fh:
            self.assertEqual(json.load(fh), {"keep": True})
        code, _, err = run_cli(["adapt-scan", "--scan", scan, "--out", out, "--overwrite"])
        self.assertEqual(code, 0, err)

    def test_dpi_values(self):
        base = scan_result_to_detections(self.scan)
        for dpi, ok in ((200, True), (200.0, True), (None, True), (300, False), (72, False), (True, False), ("200", False)):
            with self.subTest(dpi=dpi):
                d = copy.deepcopy(base)
                if dpi is None:
                    d["coordinate_frame"].pop("dpi")
                else:
                    d["coordinate_frame"]["dpi"] = dpi
                path = self._write("d.json", d)
                code, _, err = run_cli(["pending", "--detections", path, *IDENTITY_ARGS])
                self.assertEqual(code, 0 if ok else EXIT_REJECTED, err)
                if not ok:
                    self.assertIn("canonical raster is 200 DPI", err)


class Standalone(unittest.TestCase):
    def test_evaluation_does_not_import_pinny(self):
        pkg = os.path.join(EVAL_ROOT, "pinny_eval")
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name)) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [node.module or ""]
                for mod in mods:
                    with self.subTest(file=name, module=mod):
                        top = mod.split(".")[0]
                        self.assertNotIn(top, ("pinny", "pinny_learning"))
                        stdlib = getattr(sys, "stdlib_module_names", None)  # Python 3.10+
                        if stdlib is not None:
                            self.assertIn(top, stdlib)


if __name__ == "__main__":
    unittest.main()
