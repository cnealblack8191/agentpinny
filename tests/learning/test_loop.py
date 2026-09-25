"""Learning-loop data (schema 3) and read-only loop APIs."""

import contextlib
import hashlib
import io
import json
import struct
import unittest
import zlib

from pinny.learning import (
    Box, Detection, IdempotencyConflict, InvalidArgument, InvalidTransition, NotFound,
    ThresholdSuggestion, detector_settings_sha, suggest_threshold,
)
from pinny.learning import loop
from pinny.learning.__main__ import main as cli_main

from .test_store import DETS, DOCV1, DOCV2, H, W, StoreTestBase, make_scan

PAGE0 = f"{DOCV1}#p0"


def tiny_png(w, h):
    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data))
    raw = b"".join(b"\x00" + b"\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PageReviewTests(StoreTestBase):
    def test_mark_complete_requires_reviewed_pins(self):
        self.assertIsNone(self.store.page_review_status(PAGE0))
        with self.assertRaises(InvalidTransition):
            self.store.mark_page_complete(PAGE0)
        for i, det in enumerate(("det-1", "det-2", "det-3")):
            self.store.approve("scan-1", det, request_id=f"r{i}", source="test")
        pr = self.store.mark_page_complete(PAGE0)
        self.assertEqual((pr.status, pr.reviewer), ("complete", "alice"))
        self.assertIsNotNone(pr.completed_at)
        self.assertEqual(self.store.mark_page_complete(PAGE0, reviewer="bob"), pr)  # idempotent
        self.assertEqual(self.store.page_review_status(PAGE0), pr)
        reopened = self.store.mark_page_in_progress(PAGE0)
        self.assertEqual((reopened.status, reopened.completed_at), ("in_progress", None))

    def test_allow_unreviewed_and_unknown_page(self):
        self.assertEqual(self.store.mark_page_complete(PAGE0, allow_unreviewed=True).status, "complete")
        with self.assertRaises(NotFound):
            self.store.mark_page_complete(f"{DOCV2}#p9")


class TemplateTests(StoreTestBase):
    def test_register_and_get(self):
        png = tiny_png(40, 30)
        tid = self.store.register_template(sha256="a" * 64, png=png, class_label="duplex")
        t = self.store.get_template(tid)
        self.assertEqual((t.width, t.height, t.png, t.class_label), (40, 30, png, "duplex"))
        self.assertEqual(self.store.register_template(sha256="a" * 64, png=png, class_label="duplex"), tid)
        with self.assertRaises(IdempotencyConflict):
            self.store.register_template(sha256="b" * 64, width=4, height=4, template_id=tid)
        with self.assertRaises(InvalidArgument):
            self.store.register_template(sha256="a" * 64, png=png, width=41)
        with self.assertRaises(InvalidArgument):
            self.store.register_template(sha256="nothex", width=4, height=4)
        with self.assertRaises(NotFound):
            self.store.get_template("tpl-missing")
        self.assertEqual([x.template_id for x in self.store.list_templates(class_label="duplex")], [tid])

    def test_scan_references_template(self):
        with self.assertRaises(NotFound):
            self.store.record_scan(make_scan("scan-t", template_id="tpl-nope"), DETS)
        tid = self.store.register_template(sha256="f" * 64, width=40, height=40, class_label="gfci")
        self.store.record_scan(make_scan("scan-t", template_id=tid), DETS)
        self.assertEqual(self.store.list_scans()[-1].template_id, tid)
        result = self.store.scan_result("scan-t")
        self.assertEqual(result["template"]["template_id"], tid)
        result["scan_id"] = "scan-t2"
        self.store.record_scan_result(result)
        self.assertEqual(self.store.load_scan("scan-t2").scan.template_id, tid)
        self.store.approve("scan-t", "det-1", request_id="r1", source="test")
        ex = {e["example_id"]: e for e in self.store.export()["examples"]}
        self.assertEqual(ex["scan-t/det-1"]["class_label"], "gfci")  # inherited from template
        self.assertIn("scan-1", {s.scan_id for s in self.store.list_scans()})
        self.assertNotIn("template_id", self.store.scan_result("scan-1")["template"])  # back-compat


class ClassLabelAndManualBoxTests(StoreTestBase):
    def test_manual_pin_with_box_rotation_and_class(self):
        res = self.store.add_manual("scan-1", 620, 620, request_id="m1", source="viewer",
                                    box={"x": 600, "y": 600, "width": 40, "height": 40},
                                    rotation=90, class_label="duplex")
        self.assertEqual(res.pin.box, Box(600, 600, 40, 40))
        self.assertEqual((res.pin.rotation, res.pin.class_label), (90, "duplex"))
        self.assertEqual(res.event.new_state["class_label"], "duplex")
        # Section 6: the crop stays the 128 px square around the point.
        self.assertEqual(self.renderer.calls[-1].box, Box(556, 556, 128, 128))
        again = self.store.add_manual("scan-1", 620, 620, request_id="m1", source="viewer",
                                      box={"x": 600, "y": 600, "width": 40, "height": 40},
                                      rotation=90, class_label="duplex")
        self.assertTrue(again.replayed)
        with self.assertRaises(IdempotencyConflict):
            self.store.add_manual("scan-1", 620, 620, request_id="m1", source="viewer", class_label="gfci")
        with self.assertRaises(InvalidArgument):
            self.store.add_manual("scan-1", 10, 10, request_id="m2", source="viewer", rotation=45)
        with self.assertRaises(InvalidArgument):
            self.store.add_manual("scan-1", 10, 10, request_id="m3", source="viewer",
                                  box={"x": W + 5, "y": 0, "width": 4, "height": 4})
        with self.assertRaises(InvalidArgument):
            self.store.add_manual("scan-1", 10, 10, request_id="m4", source="viewer", class_label="  ")

    def test_approve_sets_class_label(self):
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer", class_label="duplex")
        self.assertEqual(res.pin.class_label, "duplex")
        self.store.reject("scan-1", "det-1", request_id="r2", source="viewer")
        self.assertEqual(self.store.get_pin("scan-1", "det-1").class_label, "duplex")


def _scan_for(doc_id, scan_id, docv=DOCV1, page=0, threshold=0.8, **kw):
    s = make_scan(scan_id, doc=docv, page=page, **kw)
    d = dict(s.__dict__)
    d["document_id"] = doc_id
    d["detector_settings"] = {"threshold": threshold}
    return type(s)(**d)


class SplitTests(StoreTestBase):
    def test_set_get_and_heldout_protection(self):
        self.assertIsNone(self.store.get_split("doc-1"))
        self.assertEqual(self.store.set_split("doc-1", "train"), "train")
        self.store.set_split("doc-1", "eval")
        with self.assertRaises(InvalidTransition):
            self.store.set_split("doc-1", "train")
        self.store.set_split("doc-1", "train", force=True)
        with self.assertRaises(InvalidArgument):
            self.store.set_split("doc-1", "holdout")

    def test_export_excludes_heldout_documents(self):
        self.store.record_scan(_scan_for("doc-eval", "scan-e", docv=DOCV2), DETS)
        self.store.set_split("doc-eval", "eval")
        self.assertEqual({s["scan_id"] for s in self.store.export()["scans"]}, {"scan-1"})
        doc = self.store.export(include_heldout=True)
        self.assertEqual({s["scan_id"] for s in doc["scans"]}, {"scan-1", "scan-e"})
        self.assertEqual(doc["splits"], {"doc-eval": "eval"})


class ReviewStatsTests(StoreTestBase):
    def test_stats_and_filters(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="test")  # 0.9
        self.store.reject("scan-1", "det-2", request_id="r2", source="test")  # 0.7
        self.store.add_manual("scan-1", 10, 10, request_id="r3", source="test")  # not a machine pin
        self.assertEqual(self.store.review_stats(), [(0.9, 1), (0.7, 0)])
        self.store.record_scan(_scan_for("doc-2", "scan-2", docv=DOCV2, threshold=0.5), DETS)
        self.store.approve("scan-2", "det-3", request_id="r4", source="test")
        self.assertEqual(self.store.review_stats(document_id="doc-2"), [(0.6, 1)])
        sha = detector_settings_sha({"threshold": 0.5})
        self.assertEqual(self.store.review_stats(detector_settings_sha=sha), [(0.6, 1)])
        self.assertEqual(len(self.store.review_stats(template_sha256="f" * 64)), 3)
        self.assertEqual(self.store.review_stats(template_sha256="0" * 64), [])
        self.store.set_split("doc-2", "test")
        self.assertEqual(len(self.store.review_stats()), 2)
        self.assertEqual(len(self.store.review_stats(include_heldout=True)), 3)


class SuggestThresholdTests(unittest.TestCase):
    def test_lowest_threshold_meeting_target(self):
        stats = [(0.95, 1)] * 20 + [(0.9, 1)] * 10 + [(0.85, 0)] * 2 + [(0.8, 1)] * 5 + [(0.7, 0)] * 10
        s = suggest_threshold(stats, target_precision=0.9, min_labels=30)
        self.assertIsInstance(s, ThresholdSuggestion)
        self.assertEqual(s.reason, "ok")
        self.assertEqual(s.value, 0.8)  # 35/37 = 0.946
        self.assertAlmostEqual(s.precision, 35 / 37)
        self.assertEqual(s.recall_proxy, 1.0)
        self.assertEqual(s.n, 47)
        self.assertEqual(suggest_threshold(stats, target_precision=0.99).value, 0.9)

    def test_no_suggestion_reasons(self):
        self.assertEqual(suggest_threshold([(0.9, 1)] * 5).reason, "insufficient_labels")
        self.assertIsNone(suggest_threshold([(0.9, 1)] * 5).value)
        self.assertEqual(suggest_threshold([(0.9, 0)] * 40).reason, "no_positives")
        self.assertEqual(suggest_threshold([(0.9, 0)] * 20 + [(0.5, 1)] * 20).reason, "target_not_reached")
        with self.assertRaises(ValueError):
            suggest_threshold([(0.9, 1)] * 40, target_precision=0)


class ReviewQueueTests(StoreTestBase):
    def test_margin_order_and_filters(self):
        # scan threshold 0.8: det-1 0.9 (0.1), det-2 0.7 (0.1), det-3 0.6 (0.2)
        self.assertEqual(self.store.review_queue("scan-1"), ["det-1", "det-2", "det-3"])
        self.assertEqual(self.store.review_queue("scan-1", threshold=0.65), ["det-2", "det-3", "det-1"])
        self.assertEqual(self.store.review_queue("scan-1", strategy="lowest_score"),
                         ["det-3", "det-2", "det-1"])
        self.assertEqual(self.store.review_queue("scan-1", limit=1), ["det-1"])
        self.store.approve("scan-1", "det-1", request_id="r1", source="test")
        self.assertEqual(self.store.review_queue("scan-1"), ["det-2", "det-3"])
        with self.assertRaises(InvalidArgument):
            self.store.review_queue("scan-1", strategy="random")
        with self.assertRaises(NotFound):
            self.store.review_queue("scan-x")

    def test_default_threshold_without_setting(self):
        s = make_scan("scan-n")
        d = dict(s.__dict__, detector_settings={})
        self.store.record_scan(type(s)(**d), DETS)
        self.assertEqual(self.store.review_queue("scan-n"), ["det-3", "det-2", "det-1"])


class TemplateBankTests(StoreTestBase):
    def test_positive_and_negative_crops(self):
        a = self.store.approve("scan-1", "det-1", request_id="r1", source="test", class_label="duplex")
        self.store.reject("scan-1", "det-2", request_id="r2", source="test")
        m = self.store.add_manual("scan-1", 620, 620, request_id="r3", source="test",
                                  box={"x": 600, "y": 600, "width": 40, "height": 40})
        gone = self.store.add_manual("scan-1", 900, 900, request_id="r4", source="test")
        self.store.remove_manual("scan-1", gone.pin.pin_id, request_id="r5", source="test")

        pos = self.store.template_bank_crops()
        self.assertEqual([(c.pin_id, c.label) for c in pos], [("det-1", "positive"), (m.pin.pin_id, "positive")])
        c = pos[0]
        self.assertEqual(c.crop_key, a.event.crop_key)
        self.assertEqual(c.class_label, "duplex")
        self.assertEqual(c.crop_box, {"x": 76, "y": 76, "width": 88, "height": 88})
        self.assertEqual(c.box, {"x": 100, "y": 100, "width": 40, "height": 40})
        self.assertEqual(c.score, 0.9)
        with open(c.path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), c.sha256)
        self.assertEqual([c.pin_id for c in self.store.template_bank_crops(class_label="duplex")], ["det-1"])
        neg = self.store.negative_crops()
        self.assertEqual([(c.pin_id, c.label) for c in neg], [("det-2", "negative")])

    def test_unwritten_heldout_and_conflicting_crops_are_excluded(self):
        self.renderer.fail = True
        self.store.approve("scan-1", "det-1", request_id="r1", source="test")
        self.assertEqual(self.store.template_bank_crops(), [])
        self.assertEqual(self.store.template_bank_crops(written_only=False)[0].status, "failed")
        self.renderer.fail = False
        self.store.process_pending_crops()
        self.assertEqual(len(self.store.template_bank_crops()), 1)
        self.store.record_scan(make_scan("scan-2"), DETS)
        self.store.reject("scan-2", "det-1", request_id="r2", source="test")
        self.assertEqual(self.store.template_bank_crops(), [])
        self.assertEqual(self.store.negative_crops(), [])
        self.store.set_split("doc-1", "eval")
        self.assertEqual(self.store.template_bank_crops(include_heldout=True, written_only=False), [])


class ExportDatasetTests(StoreTestBase):
    def review_page(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="test", class_label="duplex")
        self.store.reject("scan-1", "det-2", request_id="r2", source="test")
        self.store.approve("scan-1", "det-3", request_id="r3", source="test")
        self.store.add_manual("scan-1", 820, 820, request_id="r4", source="test")  # template-size box
        self.store.add_manual("scan-1", 1020, 1020, request_id="r5", source="test",
                              box={"x": 1000, "y": 1000, "width": 30, "height": 50}, class_label="gfci")

    def test_coco_export_of_complete_pages(self):
        self.review_page()
        out = self.dir / "ds"
        m0 = self.store.export_dataset(out)
        self.assertEqual((m0["counts"]["pages"], m0["excluded_incomplete_pages"]), (0, [PAGE0]))
        self.store.mark_page_complete(PAGE0)
        m = self.store.export_dataset(out)
        coco = json.loads((out / "train.coco.json").read_text())
        self.assertEqual(hashlib.sha256((out / "train.coco.json").read_bytes()).hexdigest(), m["sha256"])
        self.assertEqual(json.loads((out / "manifest.json").read_text()), m)
        self.assertEqual(m["pages"], [PAGE0])
        self.assertEqual(m["counts"]["annotations"], 4)
        self.assertEqual(m["counts"]["per_category"], {"duplex": 1, "gfci": 1, "receptacle": 2})
        self.assertEqual(coco["images"][0]["canonical_page_id"], PAGE0)
        self.assertEqual((coco["images"][0]["width"], coco["images"][0]["height"]), (W, H))
        self.assertEqual({c["name"] for c in coco["categories"]}, {"duplex", "gfci", "receptacle"})
        bboxes = sorted(a["bbox"] for a in coco["annotations"])
        self.assertEqual(bboxes, [[100, 100, 40, 40], [500, 500, 40, 40], [800, 800, 40, 40],
                                  [1000, 1000, 30, 50]])
        self.assertFalse(m["images"]["included"])
        self.assertEqual([x["export_id"] for x in self.store.list_dataset_exports()],
                         [m0["export_id"], m["export_id"]])
        # Deterministic content.
        self.assertEqual(self.store.export_dataset(self.dir / "ds2")["sha256"], m["sha256"])

    def test_duplicates_across_scans_are_merged(self):
        self.review_page()
        self.store.record_scan(make_scan("scan-2"), DETS)
        for i, det in enumerate(("det-1", "det-2", "det-3")):
            self.store.approve("scan-2", det, request_id=f"s2-{i}", source="test")
        self.store.mark_page_complete(PAGE0)
        m = self.store.export_dataset(self.dir / "ds")
        # det-1 and det-3 merge with scan-1 (first occurrence and its class win);
        # det-2 was rejected in scan-1 but approved in scan-2, so it is added.
        self.assertEqual(m["counts"]["annotations"], 5)
        self.assertEqual(m["counts"]["per_category"], {"duplex": 1, "gfci": 1, "receptacle": 3})

    def test_splits_and_heldout(self):
        self.review_page()
        self.store.mark_page_complete(PAGE0)
        self.store.set_split("doc-1", "eval")
        self.assertEqual(self.store.export_dataset(self.dir / "t")["counts"]["pages"], 0)
        m = self.store.export_dataset(self.dir / "e", split="eval")
        self.assertEqual(m["pages"], [PAGE0])
        self.assertTrue((self.dir / "e" / "eval.coco.json").exists())
        self.assertEqual(self.store.export_dataset(self.dir / "v", split="val")["counts"]["pages"], 0)

    def test_incomplete_pages_allowed_on_request_and_tiling(self):
        self.review_page()
        m = self.store.export_dataset(self.dir / "ds", require_page_complete=False, tile=(1024, 128))
        coco = json.loads((self.dir / "ds" / "train.coco.json").read_text())
        self.assertEqual(m["tile"], {"size": 1024, "overlap": 128})
        self.assertEqual(len(coco["images"]), 2 * 3)  # 1700 x 2200 page
        self.assertTrue(all(i["width"] <= 1024 and i["height"] <= 1024 for i in coco["images"]))
        first = [a for a in coco["annotations"] if a["image_id"] == 1]
        self.assertIn([100, 100, 40, 40], [a["bbox"] for a in first])

    def test_bad_arguments(self):
        with self.assertRaises(InvalidArgument):
            self.store.export_dataset(self.dir / "x", fmt="yolo")
        with self.assertRaises(InvalidArgument):
            self.store.export_dataset(self.dir / "x", split="holdout")
        with self.assertRaises(InvalidArgument):
            self.store.export_dataset(self.dir / "x", tile=(100, 100))

    def test_manual_pin_without_size_is_reported(self):
        s = make_scan("scan-nt")
        self.store.record_scan(type(s)(**dict(s.__dict__, template_box=None, page_index=3)),
                               [Detection("d", Box(10, 10, 5, 5), 0.9)])
        self.store.approve("scan-nt", "d", request_id="a", source="test")
        self.store.add_manual("scan-nt", 500, 500, request_id="b", source="test")
        self.store.mark_page_complete(f"{DOCV1}#p3")
        m = self.store.export_dataset(self.dir / "ds")
        self.assertEqual(len(m["warnings"]), 1)
        m = self.store.export_dataset(self.dir / "ds", manual_box_px=(20, 20))
        self.assertEqual(m["warnings"], [])


class LoopHelperTests(unittest.TestCase):
    def test_tile_windows_cover_page(self):
        tiles = loop.tile_windows(1700, 2200, 1024, 128)
        self.assertEqual(tiles[0], (0, 0, 1024, 1024))
        self.assertEqual(tiles[-1], (676, 1176, 1700, 2200))
        self.assertEqual(loop.tile_windows(500, 400, 1024), [(0, 0, 500, 400)])

    def test_clip_to_tile(self):
        self.assertEqual(loop.clip_to_tile((10, 10, 50, 50), (0, 0, 40, 100)), (10, 10, 40, 50))
        self.assertIsNone(loop.clip_to_tile((10, 10, 50, 50), (0, 0, 20, 100)))

    def test_iou(self):
        self.assertEqual(loop.iou((0, 0, 10, 10), (0, 0, 10, 10)), 1.0)
        self.assertEqual(loop.iou((0, 0, 10, 10), (20, 20, 30, 30)), 0.0)


class CliTests(StoreTestBase):
    def run_cli(self, *args):
        self.store.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli_main(["--data-dir", str(self.dir), *args]), 0)
        self.store = self.open()
        return buf.getvalue().strip()

    def test_commands(self):
        self.assertIn("store schema 3", self.run_cli("migrate"))
        self.assertEqual(self.run_cli("split", "doc-1", "val"), "val")
        self.assertEqual(self.run_cli("split", "doc-1"), "val")
        self.assertEqual(json.loads(self.run_cli("suggest-threshold"))["reason"], "insufficient_labels")
        self.assertEqual(json.loads(self.run_cli("page-complete", PAGE0, "--allow-unreviewed"))["status"],
                         "complete")
        out = json.loads(self.run_cli("export-dataset", str(self.dir / "ds"), "--split", "val"))
        self.assertEqual(out["counts"]["pages"], 1)
        self.assertIn("0 label conflicts", self.run_cli("export", "--out", str(self.dir / "e.json")))


if __name__ == "__main__":
    unittest.main()
