import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pinny_learning import (
    Detection, IdempotencyConflict, InvalidTransition, LearningStore, Scan, StaleVersion,
    contract,
)
from pinny_learning.contract import CropSpec


class FakeRenderer:
    """Deterministic stand-in for the page renderer; can be told to fail."""

    def __init__(self):
        self.fail = False
        self.calls = []

    def __call__(self, spec: CropSpec) -> bytes:
        self.calls.append(spec)
        if self.fail:
            raise OSError("disk full")
        return b"PNG:" + json.dumps(spec.to_dict(), sort_keys=True).encode()


def make_scan(scan_id="scan-1", doc="docv-1", page="page-1", **kw):
    return Scan(scan_id=scan_id, document_version_id=doc, canonical_page_id=page,
                page_width=612.0, page_height=792.0, detector_version="det-0.1",
                matching_settings={"threshold": 0.8, "scales": [1.0]},
                template_id="tmpl-duplex", page_index=0, **kw)


DETS = [
    Detection("d1", (100.0, 100.0, 120.0, 120.0), score=0.9, label="duplex"),
    Detection("d2", (300.0, 300.0, 320.0, 320.0), score=0.7, label="duplex"),
    Detection("d3", (500.0, 500.0, 520.0, 520.0), score=0.6, label="duplex"),
]


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.renderer = FakeRenderer()
        self.store = self.open()
        self.store.record_scan(make_scan(), DETS)

    def open(self):
        return LearningStore(self.dir, crop_renderer=self.renderer, default_reviewer="alice")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def labels(self, store=None, **kw):
        doc = (store or self.store).export(**kw)
        return {e["pin_id"]: (e["label"], e["label_status"]) for e in doc["examples"]}


class ReviewActionTests(StoreTestBase):
    def test_approve_reject_add_and_labels(self):
        a = self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        r = self.store.reject("scan-1", "d2", request_id="r2", source="viewer", reviewer="bob")
        m = self.store.add_manual("scan-1", 50, 60, request_id="r3", source="viewer")

        self.assertEqual(a.pin.state, "approved")
        self.assertEqual(a.event.prior_state["state"], "unreviewed")
        self.assertEqual(a.event.new_state["state"], "approved")
        self.assertEqual(a.event.reviewer, "alice")
        self.assertEqual(r.event.reviewer, "bob")
        self.assertIsNone(m.event.prior_state)
        self.assertEqual(m.pin.origin, "manual")

        self.assertEqual(self.labels(), {
            "d1": ("positive", "reviewed"),
            "d2": ("negative", "reviewed"),
            "d3": (None, "unlabeled"),
            m.pin.pin_id: ("positive", "manual_added"),
        })
        self.assertEqual(self.store.crop_status_counts(), {"written": 3})

    def test_removing_manual_pin_is_not_a_negative(self):
        m = self.store.add_manual("scan-1", 50, 60, request_id="r1", source="viewer")
        rm = self.store.delete_pin("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        self.assertEqual(rm.event.action, "remove_manual")
        self.assertIsNone(rm.event.crop_key)
        self.assertEqual(self.labels()[m.pin.pin_id], (None, "manual_removed"))
        # Raw add event is still there for reinterpretation.
        actions = [e["action"] for e in self.store.export()["events"]]
        self.assertEqual(actions, ["add", "remove_manual"])

    def test_delete_pin_rejects_machine_detection(self):
        res = self.store.delete_pin("scan-1", "d1", request_id="r1", source="viewer")
        self.assertEqual(res.event.action, "reject")
        self.assertEqual(self.labels()["d1"], ("negative", "reviewed"))

    def test_invalid_transitions(self):
        m = self.store.add_manual("scan-1", 50, 60, request_id="r1", source="viewer")
        with self.assertRaises(InvalidTransition):
            self.store.approve("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        with self.assertRaises(InvalidTransition):
            self.store.remove_manual("scan-1", "d1", request_id="r3", source="viewer")
        # Failed requests leave no trace and their ids stay usable.
        self.assertEqual(len(self.store.load_scan("scan-1").events), 1)

    def test_re_review_latest_wins_and_history_kept(self):
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        self.store.reject("scan-1", "d1", request_id="r2", source="viewer")
        self.assertEqual(self.labels()["d1"], ("negative", "re-reviewed"))
        evs = [e for e in self.store.load_scan("scan-1").events if e.pin_id == "d1"]
        self.assertEqual([e.prior_state["state"] for e in evs], ["unreviewed", "approved"])
        # Both events point at the same crop; only one crop row/file exists.
        self.assertEqual(evs[0].crop_key, evs[1].crop_key)
        self.assertEqual(self.store.crop_status_counts(), {"written": 1})

    def test_expected_version(self):
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer", expected_version=1)
        with self.assertRaises(StaleVersion):
            self.store.reject("scan-1", "d1", request_id="r2", source="viewer", expected_version=1)

    def test_state_and_event_are_atomic(self):
        def boom(*a, **k):
            raise RuntimeError("crash mid-transaction")
        self.store._ensure_crop = boom
        with self.assertRaises(RuntimeError):
            self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        del self.store._ensure_crop
        self.assertEqual(self.store.get_pin("scan-1", "d1").state, "unreviewed")
        self.assertEqual(self.store.load_scan("scan-1").events, [])
        # The failed request can be retried with the same id.
        self.assertFalse(self.store.approve("scan-1", "d1", request_id="r1", source="viewer").replayed)

    def test_events_are_append_only(self):
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE review_events SET action='reject'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM review_events")
        db.close()


class IdempotencyTests(StoreTestBase):
    def test_repeated_requests_do_not_duplicate(self):
        first = self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        again = self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(first.event, again.event)
        self.assertEqual(again.pin.version, 2)

        m1 = self.store.add_manual("scan-1", 10, 10, request_id="r2", source="viewer")
        m2 = self.store.add_manual("scan-1", 10, 10, request_id="r2", source="viewer")
        self.assertEqual(m1.pin.pin_id, m2.pin.pin_id)

        state = self.store.load_scan("scan-1")
        self.assertEqual(len(state.events), 2)
        self.assertEqual(len(state.pins), 4)
        self.assertEqual(len(self.store.export(include_unlabeled=False)["examples"]), 2)

    def test_retry_after_restart_is_idempotent(self):
        self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.store.close()
        self.store = self.open()
        res = self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.assertTrue(res.replayed)
        self.assertEqual(len(self.store.load_scan("scan-1").pins), 4)

    def test_request_id_reuse_with_different_payload_conflicts(self):
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        with self.assertRaises(IdempotencyConflict):
            self.store.reject("scan-1", "d1", request_id="r1", source="viewer")
        with self.assertRaises(IdempotencyConflict):
            self.store.approve("scan-1", "d2", request_id="r1", source="viewer")

    def test_delete_pin_replay(self):
        m = self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.store.delete_pin("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        again = self.store.delete_pin("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        self.assertTrue(again.replayed)

    def test_record_scan_idempotent(self):
        self.store.record_scan(make_scan(), DETS)  # identical: no-op
        with self.assertRaises(IdempotencyConflict):
            self.store.record_scan(make_scan(), DETS[:1])


class CropRecoveryTests(StoreTestBase):
    def test_crop_write_failure_is_recoverable(self):
        self.renderer.fail = True
        res = self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        self.assertEqual(res.pin.state, "approved")  # review committed regardless
        self.assertEqual(self.store.crop_status_counts(), {"failed": 1})
        ex = self.store.export(include_unlabeled=False)
        self.assertEqual(ex["examples"][0]["label"], "positive")
        self.assertEqual(ex["crops"][res.event.crop_key]["status"], "failed")

        self.renderer.fail = False
        self.assertEqual(self.store.process_pending_crops()["written"], 1)
        self.assertEqual(self.store.crop_status_counts(), {"written": 1})

    def test_no_renderer_leaves_pending_then_regenerates(self):
        self.store.close()
        self.store = LearningStore(self.dir)
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        self.assertEqual(self.store.crop_status_counts(), {"pending": 1})
        self.store.close()
        self.store = self.open()
        self.store.process_pending_crops()
        self.assertEqual(self.store.crop_status_counts(), {"written": 1})

    def test_missing_file_is_regenerated_identically(self):
        res = self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        path = self.store.crop_path(res.event.crop_key)
        original = path.read_bytes()
        path.unlink()
        self.store.process_pending_crops()
        self.assertEqual(path.read_bytes(), original)


class PersistenceTests(StoreTestBase):
    def test_restart_reload(self):
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        m = self.store.add_manual("scan-1", 50, 60, request_id="r2", source="viewer")
        before = self.store.load_scan("scan-1")
        before_export = self.store.export()
        self.store.close()

        self.store = self.open()
        after = self.store.load_scan("scan-1")
        self.assertEqual(before, after)
        self.assertEqual(after.scan.matching_settings, {"threshold": 0.8, "scales": [1.0]})
        self.assertEqual(after.detections[0].score, 0.9)
        self.assertEqual({p.pin_id: p.state for p in after.pins},
                         {"d1": "approved", "d2": "unreviewed", "d3": "unreviewed",
                          m.pin.pin_id: "added"})
        after_export = self.store.export()
        before_export.pop("exported_at"), after_export.pop("exported_at")
        self.assertEqual(before_export, after_export)

    def test_document_and_page_separation(self):
        self.store.record_scan(make_scan("scan-2", doc="docv-1", page="page-2"), DETS)
        self.store.record_scan(make_scan("scan-3", doc="docv-2", page="page-1"), DETS)
        # Same detection ids on different pages/docs are independent pins.
        self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        self.store.reject("scan-2", "d1", request_id="r2", source="viewer")

        self.assertEqual(self.store.get_pin("scan-3", "d1").state, "unreviewed")
        self.assertEqual([s.scan_id for s in self.store.list_scans(document_version_id="docv-1")],
                         ["scan-1", "scan-2"])
        self.assertEqual([s.scan_id for s in self.store.list_scans("docv-1", "page-2")], ["scan-2"])

        e1 = self.store.load_scan("scan-1").events[0]
        e2 = self.store.load_scan("scan-2").events[0]
        # Identical geometry on different pages must not share a crop.
        self.assertNotEqual(e1.crop_key, e2.crop_key)
        ex = self.store.export(document_version_id="docv-2")
        self.assertEqual({e["scan_id"] for e in ex["examples"]}, {"scan-3"})
        self.assertEqual([s["document_version_id"] for s in ex["scans"]], ["docv-2"])

    def test_export_contents(self):
        a = self.store.approve("scan-1", "d1", request_id="r1", source="viewer")
        out = self.dir / "exports" / "e.json"
        doc = self.store.export(include_unlabeled=False, out_path=out)
        self.assertEqual(json.loads(out.read_text()), json.loads(json.dumps(doc)))
        self.assertEqual(doc["schema"], "pinny.learning.export")
        self.assertEqual(doc["schema_version"], 1)
        s = doc["scans"][0]
        for k in ("document_version_id", "canonical_page_id", "template_id", "detector_version",
                  "matching_settings", "matching_settings_sha256"):
            self.assertIn(k, s)
        crop = doc["crops"][a.event.crop_key]
        self.assertEqual(crop["status"], "written")
        self.assertTrue((self.dir / crop["path"]).exists())
        self.assertEqual(crop["spec"]["document_version_id"], "docv-1")
        self.assertEqual(crop["spec"]["canonical_page_id"], "page-1")


class CropBoundsTests(unittest.TestCase):
    def test_manual_crop_interior(self):
        s = contract.manual_pin_crop("dv", "p", 612, 792, 100, 200, size=48, dpi=72)
        self.assertEqual(s.box, (76.0, 176.0, 124.0, 224.0))
        self.assertEqual(s.unclipped_box, s.box)
        self.assertFalse(s.clipped)
        self.assertEqual(s.pixel_box, (76, 176, 124, 224))

    def test_manual_crop_clipped_at_corner(self):
        s = contract.manual_pin_crop("dv", "p", 612, 792, 5, 790, size=48, dpi=144)
        self.assertEqual(s.unclipped_box, (-19.0, 766.0, 29.0, 814.0))
        self.assertEqual(s.box, (0.0, 766.0, 29.0, 792.0))
        self.assertTrue(s.clipped_left and s.clipped_bottom)
        self.assertFalse(s.clipped_top or s.clipped_right)
        self.assertEqual(s.pixel_box, (0, 1532, 58, 1584))

    def test_detection_crop_margin_and_clip(self):
        s = contract.detection_crop("dv", "p", 612, 792, (600, 10, 611, 20), margin=8, dpi=72)
        self.assertEqual(s.box, (592.0, 2.0, 612.0, 28.0))
        self.assertTrue(s.clipped_right)
        self.assertFalse(s.clipped_top)

    def test_deterministic_and_float_noise_stable(self):
        a = contract.manual_pin_crop("dv", "p", 612, 792, 100.0, 200.0)
        b = contract.manual_pin_crop("dv", "p", 612, 792, 100.0000001, 199.9999999)
        self.assertEqual(a, b)
        self.assertEqual(CropSpec.from_dict(json.loads(json.dumps(a.to_dict()))), a)

    def test_pixel_box_never_exceeds_page(self):
        s = contract.manual_pin_crop("dv", "p", 612.3, 792.7, 612, 792, dpi=200)
        px_w, px_h = -(-612.3 * 200 // 72), -(-792.7 * 200 // 72)
        self.assertLessEqual(s.pixel_box[2], px_w)
        self.assertLessEqual(s.pixel_box[3], px_h)

    def test_outside_page_rejected(self):
        with self.assertRaises(ValueError):
            contract.manual_pin_crop("dv", "p", 612, 792, -100, -100)


if __name__ == "__main__":
    unittest.main()
