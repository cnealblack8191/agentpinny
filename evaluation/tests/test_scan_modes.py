"""Phase 2 scan modes (docs/phase2-contracts.md P7) in detections files.

The evaluator must accept the P7 extras unchanged, record the mode, and never
score the candidates a verifier suppressed.
"""

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
from pinny_eval.scan_adapter import scan_result_to_detections  # noqa: E402

CASE = os.path.join(EVAL_ROOT, "fixtures", "synthetic", "misses")
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


class ScanModes(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(CASE, "detections.json")) as fh:
            self.base = json.load(fh)
        with open(os.path.join(CASE, "expected.json")) as fh:
            self.expected = json.load(fh)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _score(self, detections):
        path = os.path.join(self.tmp.name, "det.json")
        out_json = os.path.join(self.tmp.name, "report.json")
        with open(path, "w") as fh:
            json.dump(detections, fh)
        code, _, err = run_cli([
            "score", "--detections", path,
            "--ground-truth", os.path.join(CASE, "ground_truth.json"),
            "--tolerance-px", str(self.expected["tolerance_px"]),
            *IDENTITY_ARGS, "--json-out", out_json,
        ])
        report = None
        if code == 0:
            with open(out_json) as fh:
                report = json.load(fh)
        return code, report, err

    def _verifier_scan(self):
        det = copy.deepcopy(self.base)
        det["mode"] = "template+verifier"
        det["detector"]["name"] = "opencv-template+verifier"
        for item in det["detections"]:
            item["template_score"] = 0.91
            item["verifier_score"] = 0.8
            item["score"] = 0.8
            item["confidence"] = 0.8
        # A suppressed candidate sitting exactly on a missed reference: if it
        # were scored, it would turn a false negative into a true positive.
        gt_path = os.path.join(CASE, "ground_truth.json")
        with open(gt_path) as fh:
            gt = json.load(fh)
        matched = {(d["x"], d["y"]) for d in det["detections"]}
        missed = [r for r in gt["receptacles"] if (r["x"], r["y"]) not in matched]
        det["suppressed"] = [
            {"id": f"sup-{i}", "x": r["x"], "y": r["y"], "template_score": 0.85,
             "verifier_score": 0.1, "source": "detector"}
            for i, r in enumerate(missed)
        ]
        return det

    def test_verifier_mode_extras_accepted_and_counts_unchanged(self):
        code, report, err = self._score(self._verifier_scan())
        self.assertEqual(code, 0, err)
        want = self.expected["counts"]
        got = report["counts"]
        self.assertEqual(
            (got["true_positives"], got["false_positives"], got["false_negatives"]),
            (want["tp"], want["fp"], want["fn"]),
        )

    def test_mode_and_suppressed_count_recorded(self):
        det = self._verifier_scan()
        code, report, err = self._score(det)
        self.assertEqual(code, 0, err)
        prov = report["detector_provenance"]
        self.assertEqual(prov["mode"], "template+verifier")
        self.assertEqual(prov["suppressed_count"], len(det["suppressed"]))
        self.assertGreater(prov["suppressed_count"], 0)

    def test_model_mode_via_adapter(self):
        scan = {k: v for k, v in self.base.items() if k not in ("format", "format_version", "provenance")}
        scan["mode"] = "model"
        scan["detector"] = {"name": "pinny-point-detector", "version": "detector-x", "settings": {}}
        scan["detections"] = [
            {"id": d["id"], "box": {"x": d["x"] - 20, "y": d["y"] - 20, "width": 40, "height": 40},
             "x": d["x"], "y": d["y"], "score": 0.7, "rotation": 0, "source": "detector"}
            for d in self.base["detections"]
        ]
        code, report, err = self._score(scan_result_to_detections(scan))
        self.assertEqual(code, 0, err)
        self.assertEqual(report["detector_provenance"]["mode"], "model")
        self.assertEqual(report["counts"]["true_positives"], self.expected["counts"]["tp"])

    def test_malformed_suppressed_rejected(self):
        det = self._verifier_scan()
        det["suppressed"] = {"not": "a list"}
        code, _, err = self._score(det)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("suppressed", err)

    def test_empty_mode_rejected(self):
        det = self._verifier_scan()
        det["mode"] = ""
        code, _, err = self._score(det)
        self.assertEqual(code, EXIT_REJECTED)
        self.assertIn("mode", err)


if __name__ == "__main__":
    unittest.main()
