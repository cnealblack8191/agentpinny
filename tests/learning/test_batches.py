"""Batch scans in the learning store (contracts section 5a)."""

import sqlite3
import unittest

from pinny.learning import Box, Detection, IdempotencyConflict, InvalidArgument, InvalidTransition, NotFound

from .test_store import DOCV1, DOCV2, StoreTestBase, make_scan

SETTINGS = {"threshold": 0.8}


def page_dets(scores):
    return [Detection(f"det-{n}", Box(100 * n, 100, 40, 40), score=s) for n, s in enumerate(scores, start=1)]


class BatchTests(StoreTestBase):
    def create(self, batch_id="b-1", pages=(0, 1, 2), **kw):
        args = dict(document_id="doc-1", document_version=DOCV1, page_indexes=list(pages),
                    mode="template", settings=SETTINGS, template_page_index=0,
                    template_box={"x": 10, "y": 10, "width": 40, "height": 40}, template_sha256="f" * 64)
        args.update(kw)
        return self.store.create_batch(batch_id, **args)

    def run_page(self, batch_id, scores, scan_prefix="bscan"):
        """Claim the next page, record a scan of it and attach it."""
        i = self.store.next_batch_page(batch_id)
        scan_id = f"{scan_prefix}-{i}"
        self.store.record_scan(make_scan(scan_id, page=i), page_dets(scores))
        self.store.finish_batch_page(batch_id, i, scan_id)
        return i, scan_id

    def test_create_is_idempotent_and_starts_queued(self):
        b = self.create()
        self.assertEqual(b.status, "queued")
        self.assertEqual([p.page_index for p in b.pages], [0, 1, 2])
        self.assertEqual({p.status for p in b.pages}, {"pending"})
        self.assertEqual(b.page_counts["pending"], 3)
        self.assertEqual(self.create().batch_id, "b-1")  # same request: no-op
        with self.assertRaises(IdempotencyConflict):
            self.create(pages=(0, 1))

    def test_create_validates(self):
        for kw in ({"pages": ()}, {"pages": (0, 0)}, {"pages": (-1,)}, {"pages": (True,)},
                   {"template_page_index": -2}, {"template_box": {"x": 1}}):
            with self.subTest(kw=kw), self.assertRaises(InvalidArgument):
                self.create(batch_id="bad", **kw)

    def test_pages_run_in_request_order_and_report_counts(self):
        self.create(pages=(2, 0, 1))
        self.assertEqual(self.run_page("b-1", [0.9, 0.85])[0], 2)
        b = self.store.get_batch("b-1")
        self.assertEqual(b.status, "running")
        self.assertEqual(b.pages[0].counts, {"unreviewed": 2, "approved": 0, "rejected": 0,
                                             "added": 0, "removed": 0, "total": 2})
        self.assertEqual(b.pages[1].counts, {})  # no scan yet
        self.run_page("b-1", [0.95])
        self.run_page("b-1", [])
        self.assertIsNone(self.store.next_batch_page("b-1"))
        b = self.store.get_batch("b-1")
        self.assertEqual(b.status, "complete")
        self.assertEqual(b.pin_counts["unreviewed"], 3)
        self.assertEqual(b.pin_counts["total"], 3)
        self.assertEqual([p.scan_id for p in b.pages], ["bscan-2", "bscan-0", "bscan-1"])

    def test_failure_does_not_stop_the_batch_and_can_be_retried(self):
        self.create()
        i = self.store.next_batch_page("b-1")
        p = self.store.fail_batch_page("b-1", i, "timeout", "took too long")
        self.assertEqual((p.status, p.error_code, p.attempts), ("failed", "timeout", 1))
        self.run_page("b-1", [0.9])
        self.run_page("b-1", [0.9])
        self.assertEqual(self.store.get_batch("b-1").status, "complete")
        b = self.store.requeue_batch("b-1", retry_failed=True)
        self.assertEqual(b.pages[0].status, "pending")
        self.assertEqual(self.store.next_batch_page("b-1"), 0)
        self.assertEqual(self.store.get_batch("b-1").pages[0].attempts, 2)

    def test_finish_checks_state_and_scan(self):
        self.create()
        with self.assertRaises(InvalidTransition):  # page 0 not claimed
            self.store.finish_batch_page("b-1", 0, "scan-1")
        i = self.store.next_batch_page("b-1")
        with self.assertRaises(NotFound):
            self.store.finish_batch_page("b-1", i, "nope")
        self.store.record_scan(make_scan("other-doc", doc=DOCV2, page=0), [])
        with self.assertRaises(InvalidArgument):  # wrong document version
            self.store.finish_batch_page("b-1", i, "other-doc")
        with self.assertRaises(NotFound):
            self.store.finish_batch_page("b-1", 9, "scan-1")
        self.store.finish_batch_page("b-1", i, "scan-1")  # scan-1 is DOCV1 page 0
        self.store.finish_batch_page("b-1", i, "scan-1")  # repeat is a no-op
        with self.assertRaises(InvalidTransition):
            self.store.fail_batch_page("b-1", i, "x", "y")

    def test_interrupted_page_is_requeued(self):
        self.create()
        self.assertEqual(self.store.next_batch_page("b-1"), 0)  # then the process dies
        self.assertEqual(self.store.get_batch("b-1").pages[0].status, "running")
        b = self.store.requeue_batch("b-1")
        self.assertEqual(b.pages[0].status, "pending")
        self.assertEqual(self.store.next_batch_page("b-1"), 0)

    def test_requeue_can_leave_a_live_running_page_alone(self):
        self.create()
        i = self.store.next_batch_page("b-1")
        self.store.fail_batch_page("b-1", i, "x", "y")
        running = self.store.next_batch_page("b-1")
        b = self.store.requeue_batch("b-1", retry_failed=True, interrupted=False)
        self.assertEqual([p.status for p in b.pages], ["pending", "running", "pending"])
        self.store.record_scan(make_scan("live", page=running), [])
        self.store.finish_batch_page("b-1", running, "live")  # still claimable by its job
        self.assertEqual(self.store.requeue_batch("b-1", interrupted=False).pages[1].status, "done")

    def test_cancel_skips_pending_pages_only(self):
        self.create()
        self.run_page("b-1", [0.9])
        running = self.store.next_batch_page("b-1")
        b = self.store.cancel_batch("b-1")
        self.assertEqual([p.status for p in b.pages], ["done", "running", "skipped"])
        self.assertEqual(b.status, "running")  # the running page still finishes
        self.assertIsNone(self.store.next_batch_page("b-1"))
        self.store.record_scan(make_scan("late", page=running), [])
        self.store.finish_batch_page("b-1", running, "late")
        b = self.store.cancel_batch("b-1")  # idempotent
        self.assertEqual(b.status, "cancelled")
        self.assertEqual(self.store.requeue_batch("b-1").pages[2].status, "skipped")  # not resumed

    def test_review_queue_spans_pages_most_uncertain_first(self):
        self.create()
        self.run_page("b-1", [0.99, 0.82])
        self.run_page("b-1", [0.81, 0.95])
        self.run_page("b-1", [0.9])
        q = self.store.batch_review_queue("b-1")
        self.assertEqual([(i.page_index, i.pin_id, i.score) for i in q],
                         [(1, "det-1", 0.81), (0, "det-2", 0.82), (2, "det-1", 0.9),
                          (1, "det-2", 0.95), (0, "det-1", 0.99)])
        self.assertEqual((q[0].scan_id, q[0].x, q[0].y, q[0].version), ("bscan-1", 120.0, 120.0, 1))
        self.assertEqual(q[0].box, Box(100, 100, 40, 40))

        # Reviewed pins leave the queue; the review is a normal event on the page's scan.
        self.store.approve("bscan-1", "det-1", request_id="r-q1", source="test")
        self.store.reject("bscan-0", "det-2", request_id="r-q2", source="test")
        q = self.store.batch_review_queue("b-1", limit=2)
        self.assertEqual([(i.page_index, i.pin_id) for i in q], [(2, "det-1"), (1, "det-2")])
        b = self.store.get_batch("b-1")
        self.assertEqual((b.pin_counts["approved"], b.pin_counts["rejected"]), (1, 1))
        # Batch-reviewed pins label exactly like single-page reviews.
        labels = {e["example_id"]: e["label"] for e in self.store.export()["examples"]}
        self.assertEqual(labels["bscan-1/det-1"], "positive")
        self.assertEqual(labels["bscan-0/det-2"], "negative")
        self.assertIsNone(labels["bscan-2/det-1"])

    def test_review_queue_lowest_score_and_errors(self):
        self.create(pages=(0,))
        self.assertEqual(self.store.batch_review_queue("b-1"), [])
        self.run_page("b-1", [0.9, 0.85])
        self.assertEqual([i.score for i in self.store.batch_review_queue("b-1", "lowest_score")], [0.85, 0.9])
        with self.assertRaises(InvalidArgument):
            self.store.batch_review_queue("b-1", "random")
        with self.assertRaises(NotFound):
            self.store.batch_review_queue("nope")

    def test_page_review_complete_is_reported(self):
        self.create(pages=(0,))
        _, scan_id = self.run_page("b-1", [0.9])
        self.store.approve(scan_id, "det-1", request_id="r-c1", source="test")
        self.store.approve("scan-1", "det-1", request_id="r-c2", source="test")
        self.store.reject("scan-1", "det-2", request_id="r-c3", source="test")
        self.store.reject("scan-1", "det-3", request_id="r-c4", source="test")
        self.store.mark_page_complete(f"{DOCV1}#p0")
        self.assertTrue(self.store.get_batch("b-1").pages[0].review_complete)

    def test_list_batches(self):
        self.create("b-1")
        self.create("b-2", document_version=DOCV2)
        self.assertEqual([b.batch_id for b in self.store.list_batches()], ["b-1", "b-2"])
        self.assertEqual([b.batch_id for b in self.store.list_batches(DOCV2)], ["b-2"])
        self.assertEqual([b.batch_id for b in self.store.list_batches(document_id="doc-1")], ["b-1", "b-2"])

    def test_batches_survive_reopen_and_cannot_be_deleted(self):
        self.create()
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.get_batch("b-1").page_counts["total"], 3)
        db = sqlite3.connect(self.dir / "pinny.sqlite3")
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM batch_pages")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM batches")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
