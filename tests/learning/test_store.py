import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from pinny.learning import (
    Box, Detection, IdempotencyConflict, InvalidArgument, InvalidTransition, LearningStore,
    NotFound, Scan, SchemaMismatch, StaleVersion, contract, local_reviewer_identity,
)
from pinny.learning.contract import CropSpec

DOCV1 = "sha256:" + "a" * 64
DOCV2 = "sha256:" + "b" * 64
W, H = 1700, 2200  # letter page at 200 DPI


class FakeRenderer:
    """Deterministic stand-in for the render service's crop_renderer."""

    def __init__(self):
        self.fail = False
        self.calls = []

    def __call__(self, spec: CropSpec) -> bytes:
        self.calls.append(spec)
        if self.fail:
            raise OSError("disk full")
        return b"PNG:" + json.dumps(spec.to_dict(), sort_keys=True).encode()


def make_scan(scan_id="scan-1", doc=DOCV1, page=0, **kw):
    return Scan(scan_id=scan_id, document_id="doc-1", document_version=doc, page_index=page,
                frame_width=W, frame_height=H, detector_name="opencv-template",
                detector_version="git:abc123", detector_settings={"threshold": 0.8, "rotations": [0, 90]},
                template_box={"x": 10, "y": 10, "width": 40, "height": 40}, template_sha256="f" * 64,
                created_at="2026-09-23T12:00:00.000Z", **kw)


DETS = [
    Detection("det-1", Box(100, 100, 40, 40), score=0.9),
    Detection("det-2", {"x": 300, "y": 300, "width": 40, "height": 40}, score=0.7, rotation=90),
    Detection("det-3", Box(500, 500, 40, 40), score=0.6),
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
        a = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer", expected_version=1)
        r = self.store.reject("scan-1", "det-2", request_id="r2", source="viewer", reviewer="bob")
        m = self.store.add_manual("scan-1", 50.5, 60.25, request_id="r3", source="viewer")

        self.assertEqual(a.pin.state, "approved")
        self.assertEqual(a.pin.box, Box(100, 100, 40, 40))
        self.assertEqual((a.pin.x, a.pin.y), (120.0, 120.0))  # detection centre
        self.assertEqual(a.event.prior_state["state"], "unreviewed")
        self.assertEqual(a.event.new_state["state"], "approved")
        self.assertEqual(a.event.new_state["box"], {"x": 100, "y": 100, "width": 40, "height": 40})
        self.assertEqual(a.event.reviewer, "alice")
        self.assertEqual(r.event.reviewer, "bob")
        self.assertEqual(m.event.action, "add_manual")
        self.assertIsNone(m.event.prior_state)
        self.assertEqual(m.pin.origin, "manual")
        self.assertEqual(m.event.new_state["point"], {"x": 50.5, "y": 60.25})

        self.assertEqual(self.labels(), {
            "det-1": ("positive", "reviewed"),
            "det-2": ("negative", "reviewed"),
            "det-3": (None, "unlabeled"),
            m.pin.pin_id: ("positive", "manual_added"),
        })
        self.assertEqual(self.store.crop_status_counts(), {"written": 3})

    def test_removing_manual_pin_is_not_a_negative(self):
        m = self.store.add_manual("scan-1", 50, 60, request_id="r1", source="viewer")
        rm = self.store.delete_pin("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        self.assertEqual(rm.event.action, "remove_manual")
        self.assertIsNone(rm.event.crop_key)
        self.assertEqual(self.labels()[m.pin.pin_id], (None, "manual_removed"))
        actions = [e["action"] for e in self.store.export()["events"]]
        self.assertEqual(actions, ["add_manual", "remove_manual"])

    def test_delete_pin_rejects_machine_detection(self):
        res = self.store.delete_pin("scan-1", "det-1", request_id="r1", source="viewer")
        self.assertEqual(res.event.action, "reject")
        self.assertEqual(self.labels()["det-1"], ("negative", "reviewed"))

    def test_invalid_transitions_and_arguments(self):
        m = self.store.add_manual("scan-1", 50, 60, request_id="r1", source="viewer")
        with self.assertRaises(InvalidTransition):
            self.store.approve("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        with self.assertRaises(InvalidTransition):
            self.store.remove_manual("scan-1", "det-1", request_id="r3", source="viewer")
        with self.assertRaises(InvalidArgument):
            self.store.approve("scan-1", "det-1", request_id="r4", source="browser")
        with self.assertRaises(InvalidArgument):
            self.store.add_manual("scan-1", W, 10, request_id="r5", source="viewer")
        with self.assertRaises(NotFound):
            self.store.approve("scan-1", "nope", request_id="r6", source="viewer")
        with self.assertRaises(NotFound):
            self.store.approve("scan-x", "det-1", request_id="r7", source="viewer")
        # Failed requests leave no trace and their ids stay usable.
        self.assertEqual(len(self.store.load_scan("scan-1").events), 1)
        self.store.approve("scan-1", "det-1", request_id="r4", source="viewer")

    def test_errors_carry_stable_codes(self):
        with self.assertRaises(NotFound) as cm:
            self.store.approve("scan-1", "nope", request_id="r1", source="viewer")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("nope", str(cm.exception))

    def test_re_review_latest_wins_and_history_kept(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.reject("scan-1", "det-1", request_id="r2", source="viewer")
        self.assertEqual(self.labels()["det-1"], ("negative", "re-reviewed"))
        evs = [e for e in self.store.load_scan("scan-1").events if e.pin_id == "det-1"]
        self.assertEqual([e.prior_state["state"] for e in evs], ["unreviewed", "approved"])
        self.assertEqual(evs[0].crop_key, evs[1].crop_key)
        self.assertEqual(self.store.crop_status_counts(), {"written": 1})

    def test_expected_version(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer", expected_version=1)
        with self.assertRaises(StaleVersion) as cm:
            self.store.reject("scan-1", "det-1", request_id="r2", source="viewer", expected_version=1)
        self.assertEqual(cm.exception.code, "stale_version")

    def test_state_and_event_are_atomic(self):
        def boom(*a, **k):
            raise RuntimeError("crash mid-transaction")
        self.store._ensure_crop = boom
        with self.assertRaises(RuntimeError):
            self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        del self.store._ensure_crop
        self.assertEqual(self.store.get_pin("scan-1", "det-1").state, "unreviewed")
        self.assertEqual(self.store.load_scan("scan-1").events, [])
        self.assertFalse(self.store.approve("scan-1", "det-1", request_id="r1", source="viewer").replayed)

    def test_events_are_append_only(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE review_events SET action='reject'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM review_events")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE detections SET score=0")
        db.close()


class ReviewerTests(unittest.TestCase):
    def test_local_reviewer_identity(self):
        with mock.patch.dict(os.environ, {"PINNY_REVIEWER": "carol"}):
            self.assertEqual(local_reviewer_identity(), "carol")
        with mock.patch.dict(os.environ, {"PINNY_REVIEWER": ""}), \
                mock.patch("getpass.getuser", return_value="osuser"):
            self.assertEqual(local_reviewer_identity(), "osuser")
        with mock.patch.dict(os.environ, {"PINNY_REVIEWER": ""}), \
                mock.patch("getpass.getuser", side_effect=OSError):
            self.assertIsNone(local_reviewer_identity())

    def test_store_defaults_to_local_reviewer(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PINNY_REVIEWER": "carol"}):
            with LearningStore(d) as s:
                s.record_scan(make_scan(), DETS)
                self.assertEqual(s.approve("scan-1", "det-1", request_id="r", source="test").event.reviewer,
                                 "carol")


class IdempotencyTests(StoreTestBase):
    def test_repeated_requests_do_not_duplicate(self):
        rid = str(uuid.uuid4())
        first = self.store.approve("scan-1", "det-1", request_id=rid, source="viewer", expected_version=1)
        again = self.store.approve("scan-1", "det-1", request_id=rid, source="viewer", expected_version=1)
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)  # stale expected_version is irrelevant on replay
        self.assertEqual(first.event, again.event)
        self.assertEqual(again.pin.version, 2)

        rid2 = str(uuid.uuid4())
        m1 = self.store.add_manual("scan-1", 10, 10, request_id=rid2, source="viewer")
        m2 = self.store.add_manual("scan-1", 10, 10, request_id=rid2, source="viewer")
        self.assertEqual(m1.pin.pin_id, m2.pin.pin_id)

        state = self.store.load_scan("scan-1")
        self.assertEqual(len(state.events), 2)
        self.assertEqual(len(state.pins), 4)
        self.assertEqual(len(self.store.export(include_unlabeled=False)["examples"]), 2)
        self.assertEqual(len(self.renderer.calls), 2)

    def test_retry_after_restart_is_idempotent(self):
        self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.store.close()
        self.store = self.open()
        res = self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.assertTrue(res.replayed)
        self.assertEqual(len(self.store.load_scan("scan-1").pins), 4)

    def test_request_id_reuse_with_different_payload_conflicts(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        with self.assertRaises(IdempotencyConflict):
            self.store.reject("scan-1", "det-1", request_id="r1", source="viewer")
        with self.assertRaises(IdempotencyConflict) as cm:
            self.store.approve("scan-1", "det-2", request_id="r1", source="viewer")
        self.assertEqual(cm.exception.code, "idempotency_conflict")

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
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.assertEqual(res.pin.state, "approved")
        self.assertEqual(self.store.crop_status_counts(), {"failed": 1})
        ex = self.store.export(include_unlabeled=False)
        self.assertEqual(ex["examples"][0]["label"], "positive")
        self.assertEqual(ex["crops"][res.event.crop_key]["status"], "failed")

        self.renderer.fail = False
        self.assertEqual(self.store.process_pending_crops()["written"], 1)
        self.assertEqual(self.store.crop_status_counts(), {"written": 1})

    def test_no_renderer_leaves_pending_then_regenerates(self):
        self.store.close()
        self.store = LearningStore(self.dir, default_reviewer=None)
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.assertEqual(self.store.crop_status_counts(), {"pending": 1})
        self.store.close()
        self.store = self.open()
        self.store.process_pending_crops()
        self.assertEqual(self.store.crop_status_counts(), {"written": 1})

    def test_missing_file_is_regenerated_identically(self):
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        path = self.store.crop_path(res.event.crop_key)
        original = path.read_bytes()
        path.unlink()
        self.store.process_pending_crops()
        self.assertEqual(path.read_bytes(), original)

    def test_renderer_receives_exact_page_reference(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        spec = self.renderer.calls[0]
        self.assertEqual((spec.document_version, spec.page_index), (DOCV1, 0))
        self.assertEqual(spec.canonical_page_id, f"{DOCV1}#p0")
        self.assertEqual(spec.box, Box(76, 76, 88, 88))  # 40 px box + 24 px margin


class PersistenceTests(StoreTestBase):
    def test_restart_reload(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        m = self.store.add_manual("scan-1", 50, 60, request_id="r2", source="viewer")
        before = self.store.load_scan("scan-1")
        before_export = self.store.export()
        self.store.close()

        self.store = self.open()
        after = self.store.load_scan("scan-1")
        self.assertEqual(before, after)
        self.assertEqual(after.scan, make_scan())
        self.assertEqual(after.detections[1].rotation, 90)
        self.assertEqual({p.pin_id: p.state for p in after.pins},
                         {"det-1": "approved", "det-2": "unreviewed", "det-3": "unreviewed",
                          m.pin.pin_id: "added"})
        after_export = self.store.export()
        before_export.pop("exported_at"), after_export.pop("exported_at")
        self.assertEqual(before_export, after_export)

    def test_document_and_page_separation(self):
        self.store.record_scan(make_scan("scan-2", doc=DOCV1, page=1), DETS)
        self.store.record_scan(make_scan("scan-3", doc=DOCV2, page=0), DETS)
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.reject("scan-2", "det-1", request_id="r2", source="viewer")

        self.assertEqual(self.store.get_pin("scan-3", "det-1").state, "unreviewed")
        self.assertEqual([s.scan_id for s in self.store.list_scans(DOCV1)], ["scan-1", "scan-2"])
        self.assertEqual([s.scan_id for s in self.store.list_scans(DOCV1, 1)], ["scan-2"])
        self.assertEqual(self.store.list_scans(DOCV1, 1)[0].canonical_page_id, f"{DOCV1}#p1")

        e1 = self.store.load_scan("scan-1").events[0]
        e2 = self.store.load_scan("scan-2").events[0]
        self.assertNotEqual(e1.crop_key, e2.crop_key)
        ex = self.store.export(document_version=DOCV2)
        self.assertEqual({e["scan_id"] for e in ex["examples"]}, {"scan-3"})
        self.assertEqual([s["document"]["document_version"] for s in ex["scans"]], [DOCV2])

    def test_scan_result_round_trip(self):
        result = self.store.scan_result("scan-1")
        self.assertEqual(result["coordinate_frame"], contract.frame_descriptor(W, H))
        self.assertEqual(result["detections"][0],
                         {"id": "det-1", "box": {"x": 100, "y": 100, "width": 40, "height": 40},
                          "x": 120.0, "y": 120.0, "score": 0.9, "rotation": 0, "source": "detector"})
        result["scan_id"] = "scan-copy"
        self.assertEqual(self.store.record_scan_result(result), "scan-copy")
        copy = self.store.scan_result("scan-copy")
        copy["scan_id"] = "scan-1"
        self.assertEqual(copy, self.store.scan_result("scan-1"))

    def test_scan_result_rejects_wrong_frame(self):
        bad = self.store.scan_result("scan-1")
        bad["scan_id"] = "scan-bad"
        bad["coordinate_frame"]["dpi"] = 72
        with self.assertRaises(InvalidArgument):
            self.store.record_scan_result(bad)

    def test_old_schema_is_refused(self):
        # Updated for schema migrations: the old version of this test restored
        # schema_version to '2' and expected a plain reopen, which encoded the
        # "refuse any version bump" behaviour. Schema 1 (pre-contract) is still
        # refused; the store is restored to the current version afterwards.
        self.store.close()
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        db.execute("UPDATE store_meta SET value='1' WHERE key='schema_version'")
        db.commit()
        db.close()
        with self.assertRaises(SchemaMismatch) as cm:
            self.open()
        self.assertEqual(cm.exception.code, "schema_mismatch")
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        db.execute("UPDATE store_meta SET value=? WHERE key='schema_version'",
                   (str(contract.STORE_SCHEMA_VERSION),))
        db.commit()
        db.close()
        self.store = self.open()

    def test_export_contents(self):
        a = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        out = self.dir / "exports" / "e.json"
        doc = self.store.export(include_unlabeled=False, out_path=out)
        self.assertEqual(json.loads(out.read_text()), json.loads(json.dumps(doc)))
        self.assertEqual((doc["schema"], doc["schema_version"]), ("pinny.learning.export", 2))
        s = doc["scans"][0]
        self.assertEqual(s["canonical_page_id"], f"{DOCV1}#p0")
        self.assertEqual(s["coordinate_frame"]["width"], W)
        self.assertEqual(s["detector"]["version"], "git:abc123")
        self.assertIn("settings_sha256", s["detector"])
        self.assertEqual(s["template"]["sha256"], "f" * 64)
        ex = doc["examples"][0]
        self.assertEqual(ex["box"], {"x": 100, "y": 100, "width": 40, "height": 40})
        self.assertEqual(ex["point"], {"x": 120.0, "y": 120.0})
        crop = doc["crops"][a.event.crop_key]
        self.assertEqual(crop["status"], "written")
        self.assertTrue((self.dir / crop["path"]).exists())
        self.assertEqual(crop["spec"]["spec_version"], 2)
        self.assertEqual(crop["spec"]["box"], {"x": 76, "y": 76, "width": 88, "height": 88})


class CropBoundsTests(unittest.TestCase):
    def test_manual_crop_interior(self):
        s = contract.manual_pin_crop(DOCV1, 0, W, H, 500, 700)
        self.assertEqual(s.box, Box(436, 636, 128, 128))
        self.assertEqual(s.unclipped_box, s.box)
        self.assertFalse(s.clipped)
        self.assertEqual(s.spec_version, 2)
        self.assertEqual(s.dpi, 200)

    def test_manual_crop_fractional_point_rounds_half_up(self):
        self.assertEqual(contract.manual_pin_crop(DOCV1, 0, W, H, 500.5, 700.49).box,
                         Box(437, 636, 128, 128))

    def test_manual_crop_clipped_at_corner(self):
        s = contract.manual_pin_crop(DOCV1, 0, W, H, 5, 2195)
        self.assertEqual(s.unclipped_box, Box(-59, 2131, 128, 128))
        self.assertEqual(s.box, Box(0, 2131, 69, 69))
        self.assertTrue(s.clipped_left and s.clipped_bottom)
        self.assertFalse(s.clipped_top or s.clipped_right)

    def test_detection_crop_margin_and_clip(self):
        s = contract.detection_crop(DOCV1, 0, W, H, {"x": 1680, "y": 10, "width": 15, "height": 20})
        self.assertEqual(s.unclipped_box, Box(1656, -14, 63, 68))
        self.assertEqual(s.box, Box(1656, 0, 44, 54))
        self.assertTrue(s.clipped_right and s.clipped_top)
        self.assertFalse(s.clipped_left or s.clipped_bottom)

    def test_detection_crop_float_box_expands_to_whole_pixels(self):
        s = contract.detection_crop(DOCV1, 0, W, H, Box(100.2, 100.7, 10.1, 10.0))
        self.assertEqual(s.box, Box(76, 76, 59, 59))  # floor(100.2)-24 .. ceil(110.3)+24

    def test_deterministic_and_float_noise_stable(self):
        a = contract.manual_pin_crop(DOCV1, 0, W, H, 100.0, 200.0)
        b = contract.manual_pin_crop(DOCV1, 0, W, H, 100.0000001, 199.9999999)
        self.assertEqual(a, b)
        self.assertEqual(CropSpec.from_dict(json.loads(json.dumps(a.to_dict()))), a)

    def test_old_spec_version_refused(self):
        d = contract.manual_pin_crop(DOCV1, 0, W, H, 100, 200).to_dict()
        d["spec_version"] = 1
        with self.assertRaises(ValueError):
            CropSpec.from_dict(d)

    def test_outside_raster_rejected(self):
        with self.assertRaises(ValueError):
            contract.manual_pin_crop(DOCV1, 0, W, H, -100, -100)

    def test_canonical_page_id(self):
        self.assertEqual(contract.canonical_page_id(DOCV1, 3), f"{DOCV1}#p3")


if __name__ == "__main__":
    unittest.main()
