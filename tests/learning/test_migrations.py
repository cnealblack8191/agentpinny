"""Schema migrations (pinny/learning/schema.py)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pinny.learning import LearningStore, SchemaMismatch, contract, schema

from .test_store import DOCV1, FakeRenderer

FIXTURE = Path(__file__).parent / "fixtures" / "store_v2.sql"


def _columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def _schema_shape(path):
    db = sqlite3.connect(path)
    try:
        objs = sorted(r for r in db.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        return objs, {name: _columns(db, name) for typ, name in objs if typ == "table"}
    finally:
        db.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        db.executescript(FIXTURE.read_text())  # written by the schema-2 code
        db.close()

    def tearDown(self):
        self._tmp.cleanup()

    def open(self, **kw):
        return LearningStore(self.dir, crop_renderer=FakeRenderer(), default_reviewer="dave", **kw)

    def test_upgrades_schema_2_in_place(self):
        with self.open() as s:
            self.assertEqual(s.migrated_from, [2])
            self.assertEqual(s.schema_version, contract.STORE_SCHEMA_VERSION)
            st = s.load_scan("scan-1")
            self.assertEqual({p.pin_id: p.state for p in st.pins if p.origin == "machine"},
                             {"det-1": "approved", "det-2": "rejected", "det-3": "unreviewed"})
            self.assertEqual(s.get_pin("scan-1", "det-2").rotation, 90)  # backfilled from detections
            self.assertEqual([e.reviewer for e in st.events], ["alice", "bob", "alice"])
            labels = {e["pin_id"]: e["label"] for e in s.export()["examples"]}
            self.assertEqual(labels["det-1"], "positive")
            self.assertEqual(labels["det-2"], "negative")
        with self.open() as s:  # second open: nothing to do
            self.assertEqual(s.migrated_from, [])

    def test_legacy_requests_replay_even_under_a_new_reviewer(self):
        with self.open() as s:
            res = s.approve("scan-1", "det-1", request_id="legacy-r1", source="viewer", reviewer="zed")
            self.assertTrue(res.replayed)
            self.assertEqual(res.event.reviewer, "alice")
            res = s.add_manual("scan-1", 50.5, 60.25, request_id="legacy-r3", source="viewer")
            self.assertTrue(res.replayed)
            self.assertEqual(len(s.load_scan("scan-1").events), 3)

    def test_legacy_crop_keys_stay_valid(self):
        with self.open() as s:
            ev = s.load_scan("scan-1").events[0]
            pin = s.get_pin("scan-1", "det-1")
            spec = contract.detection_crop(DOCV1, 0, 1700, 2200, pin.box)
            self.assertEqual(spec.key(), ev.crop_key)
            counts = s.process_pending_crops()  # files are not in the fixture: regenerate
            self.assertEqual(counts["written"], 3)
            self.assertEqual(s.crop_status_counts(), {"written": 3})

    def test_new_features_work_after_upgrade(self):
        with self.open() as s:
            s.set_split("doc-1", "val")
            self.assertEqual(s.get_split("doc-1"), "val")
            s.reject("scan-1", "det-3", request_id="new-r", source="test")
            self.assertEqual(s.mark_page_complete(f"{DOCV1}#p0").status, "complete")
            tid = s.register_template(sha256="0" * 64, width=40, height=40, class_label="duplex")
            self.assertEqual(s.get_template(tid).class_label, "duplex")
            db = sqlite3.connect(self.dir / "pinny.sqlite3")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE scans SET document_id='x'")
            finally:
                db.close()

    def test_upgraded_schema_matches_fresh_schema(self):
        self.open().close()
        with tempfile.TemporaryDirectory() as d:
            LearningStore(d).close()
            self.assertEqual(_schema_shape(self.dir / "pinny.sqlite3"),
                             _schema_shape(Path(d) / "pinny.sqlite3"))

    def test_failed_migration_rolls_back(self):
        def boom(db):
            db.execute("CREATE TABLE half_done (x)")
            raise RuntimeError("migration bug")
        db = sqlite3.connect(self.dir / "pinny.sqlite3", isolation_level=None)
        try:
            with self.assertRaises(RuntimeError):
                schema.migrate(db, 3, {2: boom})
            self.assertEqual(schema.read_version(db), 2)
            self.assertNotIn("half_done", [r[0] for r in db.execute("SELECT name FROM sqlite_master")])
            self.assertNotIn("class_label", _columns(db, "pins"))
            self.assertFalse(db.in_transaction)
        finally:
            db.close()
        with self.open() as s:  # the real migration still applies afterwards
            self.assertEqual(s.migrated_from, [2])

    def test_newer_and_prototype_schemas_are_refused(self):
        for version in ("99", "1"):
            db = sqlite3.connect(self.dir / "pinny.sqlite3")
            db.execute("UPDATE store_meta SET value=? WHERE key='schema_version'", (version,))
            db.commit()
            db.close()
            with self.assertRaises(SchemaMismatch) as cm:
                self.open()
            self.assertEqual(cm.exception.code, "schema_mismatch")

    def test_missing_step_is_refused(self):
        db = sqlite3.connect(self.dir / "pinny.sqlite3", isolation_level=None)
        try:
            with self.assertRaises(ValueError):
                schema.migrate(db, 4)
            self.assertEqual(schema.read_version(db), 2)
        finally:
            db.close()


class StatementSplitTests(unittest.TestCase):
    def test_trigger_bodies_stay_whole(self):
        stmts = schema.statements(schema.BASELINE_V2)
        triggers = [s for s in stmts if s.startswith("CREATE TRIGGER")]
        self.assertEqual(len(triggers), 3)
        self.assertTrue(all(s.endswith("END;") for s in triggers))


if __name__ == "__main__":
    unittest.main()
