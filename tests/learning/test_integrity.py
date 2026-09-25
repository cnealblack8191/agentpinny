"""Integrity fixes from docs/improvement-review.md ("Learning store integrity issues")."""

import hashlib
import json
import sqlite3
import threading

from pinny.learning import Box, Detection, LearningStore, WrongThread, contract
from pinny.learning.contract import CropSpec

from .test_store import DETS, DOCV1, H, W, StoreTestBase, make_scan


class _FailingCommit:
    """Connection proxy whose next COMMIT fails (disk full, I/O error ...)."""

    def __init__(self, db):
        self._db = db
        self.fail = True

    def execute(self, sql, *args):
        if sql == "COMMIT" and self.fail:
            self.fail = False
            raise sqlite3.OperationalError("disk I/O error")
        return self._db.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._db, name)


class ExportSnapshotTests(StoreTestBase):
    def test_export_reads_one_snapshot(self):
        """A review committed by another connection mid-export must not pair
        an old pin state with a new event list."""
        other = LearningStore(self.dir, crop_renderer=self.renderer, default_reviewer="bob")
        fired = []

        def trace(sql):
            if "FROM review_events" in sql and not fired:
                fired.append(sql)
                other.approve("scan-1", "det-1", request_id="concurrent", source="test")

        self.store._conn.set_trace_callback(trace)
        try:
            doc = self.store.export()
        finally:
            self.store._conn.set_trace_callback(None)
            other.close()
        self.assertTrue(fired)
        ex = {e["pin_id"]: e for e in doc["examples"]}
        self.assertEqual(ex["det-1"]["pin_state"], "unreviewed")
        self.assertEqual(doc["events"], [])
        self.assertEqual(self.store.get_pin("scan-1", "det-1").state, "approved")  # committed

    def test_export_query_count_does_not_grow_with_scans(self):
        def count():
            stmts = []
            self.store._conn.set_trace_callback(stmts.append)
            try:
                self.store.export()
            finally:
                self.store._conn.set_trace_callback(None)
            return [s for s in stmts if s.lstrip().upper().startswith(("SELECT", "WITH"))]

        self.store.approve("scan-1", "det-1", request_id="r1", source="test")
        one = count()
        for i in range(2, 7):
            self.store.record_scan(make_scan(f"scan-{i}", page=i), DETS)
            self.store.approve(f"scan-{i}", "det-1", request_id=f"r{i}", source="test")
        self.assertEqual(len(count()), len(one))
        self.assertLessEqual(len(one), 8)

    def test_export_file_is_written_atomically(self):
        out = self.dir / "exports" / "e.json"
        self.store.export(out_path=out)
        self.assertEqual(json.loads(out.read_text())["schema"], "pinny.learning.export")
        self.assertEqual([p.name for p in out.parent.iterdir()], ["e.json"])  # no tmp left


class ReplayTests(StoreTestBase):
    def test_replay_returns_result_as_recorded(self):
        first = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.reject("scan-1", "det-1", request_id="r2", source="viewer")
        again = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.assertTrue(again.replayed)
        self.assertEqual(again.pin, first.pin)  # approved at version 2, not the current row
        self.assertEqual(again.pin.state, "approved")
        self.assertEqual(self.store.get_pin("scan-1", "det-1").state, "rejected")

    def test_manual_replay_after_removal_returns_added_pin(self):
        m = self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.store.remove_manual("scan-1", m.pin.pin_id, request_id="r2", source="viewer")
        again = self.store.add_manual("scan-1", 10, 10, request_id="r1", source="viewer")
        self.assertEqual(again.pin, m.pin)
        self.assertEqual(again.pin.state, "added")

    def test_reviewer_change_replays_instead_of_conflicting(self):
        first = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer", reviewer="alice")
        again = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer", reviewer="carol")
        self.assertTrue(again.replayed)
        self.assertEqual(again.event.reviewer, "alice")  # recorded identity kept
        self.assertEqual(again.event, first.event)


class TransactionTests(StoreTestBase):
    def test_failed_commit_rolls_back_and_connection_stays_usable(self):
        real = self.store._conn
        self.store._conn = _FailingCommit(real)
        with self.assertRaises(sqlite3.OperationalError):
            self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store._conn = real
        self.assertFalse(real.in_transaction)
        self.assertEqual(self.store.get_pin("scan-1", "det-1").state, "unreviewed")
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.assertFalse(res.replayed)
        self.assertEqual(res.pin.version, 2)


class ThreadTests(StoreTestBase):
    def test_use_from_other_thread_raises_clear_error(self):
        errors = []

        def worker():
            try:
                self.store.get_pin("scan-1", "det-1")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], WrongThread)
        self.assertEqual(errors[0].code, "wrong_thread")
        self.assertIn("one LearningStore per thread", str(errors[0]))

    def test_one_store_per_thread_works_concurrently(self):
        errors = []

        def worker(i):
            try:
                with LearningStore(self.dir, default_reviewer=f"t{i}") as s:
                    s.approve("scan-1", f"det-{i}", request_id=f"thread-{i}", source="test")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual({p.state for p in self.store.load_scan("scan-1").pins}, {"approved"})


class CropIntegrityTests(StoreTestBase):
    def test_renderer_version_is_part_of_the_crop_key(self):
        a = contract.detection_crop(DOCV1, 0, W, H, Box(100, 100, 40, 40))
        b = contract.detection_crop(DOCV1, 0, W, H, Box(100, 100, 40, 40), renderer_version="pdfium-6721")
        self.assertEqual(a.renderer_version, "unknown")
        self.assertNotEqual(a.key(), b.key())
        self.assertEqual(CropSpec.from_dict(json.loads(json.dumps(b.to_dict()))), b)
        # Back-compat: "unknown" hashes like a spec written before the field existed.
        legacy = a.to_dict()
        del legacy["renderer_version"]
        legacy_key = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":"))
                                    .encode()).hexdigest()
        self.assertEqual(a.key(), legacy_key)
        self.assertEqual(CropSpec.from_dict(legacy), a)

    def test_store_uses_renderer_version(self):
        self.store.close()
        self.renderer.renderer_version = "pdfium-6721"
        self.store = self.open()
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.assertEqual(self.renderer.calls[-1].renderer_version, "pdfium-6721")
        crop = self.store.export()["crops"][res.event.crop_key]
        self.assertEqual(crop["spec"]["renderer_version"], "pdfium-6721")

    def test_corrupted_crop_file_is_detected_and_regenerated(self):
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        path = self.store.crop_path(res.event.crop_key)
        good = path.read_bytes()
        path.write_bytes(b"garbage")
        self.assertEqual(self.store.process_pending_crops(), {"written": 1, "failed": 0, "pending": 0})
        self.assertEqual(path.read_bytes(), good)

    def test_corrupted_crop_without_renderer_goes_pending(self):
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.crop_path(res.event.crop_key).write_bytes(b"garbage")
        self.store.close()
        self.store = LearningStore(self.dir, default_reviewer=None)
        self.store.process_pending_crops()
        crop = self.store.export()["crops"][res.event.crop_key]
        self.assertEqual(crop["status"], "pending")
        self.assertIn("sha256", crop["last_error"])

    def test_nondeterministic_rerender_is_flagged(self):
        res = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.crop_path(res.event.crop_key).unlink()
        self.store.crop_renderer = lambda spec: b"different pixels"
        self.store.process_pending_crops()
        crop = self.store.export()["crops"][res.event.crop_key]
        self.assertEqual(crop["status"], "written")
        self.assertIn("differs from recorded sha256", crop["last_error"])

    def test_conflicting_labels_for_same_crop_are_reported(self):
        self.store.record_scan(make_scan("scan-2"), DETS)  # same page, same boxes
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.reject("scan-2", "det-1", request_id="r2", source="viewer")
        doc = self.store.export()
        self.assertEqual(len(doc["label_conflicts"]), 1)
        c = doc["label_conflicts"][0]
        self.assertEqual(c["labels"], ["negative", "positive"])
        self.assertEqual(sorted(c["example_ids"]["positive"] + c["example_ids"]["negative"]),
                         ["scan-1/det-1", "scan-2/det-1"])


class ImmutabilityTests(StoreTestBase):
    def test_scans_and_detections_are_immutable(self):
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE scans SET detector_version='x'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM detections")
        finally:
            db.close()

    def test_reapprove_is_a_noop_without_event(self):
        first = self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        again = self.store.approve("scan-1", "det-1", request_id="r2", source="viewer")
        self.assertTrue(again.noop)
        self.assertFalse(again.replayed)
        self.assertEqual(again.pin, first.pin)
        self.assertEqual(again.event, first.event)
        self.assertEqual(len(self.store.load_scan("scan-1").events), 1)
        self.assertEqual(self.store.get_pin("scan-1", "det-1").version, 2)

    def test_noop_request_stays_idempotent(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        self.store.approve("scan-1", "det-1", request_id="r2", source="viewer")  # no-op
        self.store.reject("scan-1", "det-1", request_id="r3", source="viewer")
        retry = self.store.approve("scan-1", "det-1", request_id="r2", source="viewer")
        self.assertTrue(retry.replayed and retry.noop)
        self.assertEqual(retry.pin.state, "approved")  # as recorded
        self.assertEqual(self.store.get_pin("scan-1", "det-1").state, "rejected")  # not re-applied
        self.assertEqual(len(self.store.load_scan("scan-1").events), 2)

    def test_reject_twice_and_delete_pin_noop(self):
        self.store.reject("scan-1", "det-1", request_id="r1", source="viewer")
        res = self.store.delete_pin("scan-1", "det-1", request_id="r2", source="viewer")
        self.assertTrue(res.noop)
        self.assertTrue(self.store.delete_pin("scan-1", "det-1", request_id="r2", source="viewer").replayed)

    def test_reapprove_with_new_class_label_is_an_event(self):
        self.store.approve("scan-1", "det-1", request_id="r1", source="viewer")
        res = self.store.approve("scan-1", "det-1", request_id="r2", source="viewer", class_label="duplex")
        self.assertFalse(res.noop)
        self.assertEqual(res.pin.class_label, "duplex")
        self.assertEqual(res.pin.version, 3)
        same = self.store.approve("scan-1", "det-1", request_id="r3", source="viewer", class_label="duplex")
        self.assertTrue(same.noop)


class DetectionRoundTripTests(StoreTestBase):
    def test_machine_pin_carries_detection_rotation(self):
        self.assertEqual(self.store.get_pin("scan-1", "det-2").rotation, 90)
        self.store.record_scan(make_scan("scan-r"), [Detection("d", Box(1, 1, 5, 5), 0.5, rotation=270)])
        self.assertEqual(self.store.get_pin("scan-r", "d").rotation, 270)

