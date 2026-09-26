// node --test tests/viewer/test_batch.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { parsePages, isActive, progressText, pageBadge, nextQueueItem } from '../../web/batch.js';

test('parsePages: blank or "all" means every page', () => {
  assert.equal(parsePages('', 5), null);
  assert.equal(parsePages('  All ', 5), null);
});

test('parsePages: numbers and ranges, 1-based in, 0-based out, in order given', () => {
  assert.deepEqual(parsePages('1-3, 7', 10), [0, 1, 2, 6]);
  assert.deepEqual(parsePages('8, 2 - 3', 10), [7, 1, 2]);
  assert.deepEqual(parsePages('2,2,1-2,', 3), [1, 0]); // repeats dropped
});

test('parsePages: clear errors', () => {
  assert.throws(() => parsePages('0', 5), /numbered from 1/);
  assert.throws(() => parsePages('6', 5), /5 page\(s\)/);
  assert.throws(() => parsePages('4-2', 5), /backwards/);
  assert.throws(() => parsePages('a', 5), /not a page number/);
  assert.throws(() => parsePages(',', 5), /No pages/);
});

const batch = (status, pages, pins = { unreviewed: 0, total: 0 }) => {
  const c = { pending: 0, running: 0, done: 0, failed: 0, skipped: 0, total: pages.length };
  for (const p of pages) c[p.status] += 1;
  return { status, pages, page_counts: c, pin_counts: pins };
};
const page = (i, status, counts = {}, extra = {}) => ({ page_index: i, status,
  counts: status === 'done' ? { unreviewed: 0, total: 0, ...counts } : {}, review_complete: false, ...extra });

test('isActive and progressText', () => {
  const b = batch('running', [page(0, 'done', { unreviewed: 2, total: 3 }), page(1, 'running'),
    page(2, 'failed')], { unreviewed: 2, total: 3 });
  assert.ok(isActive(b));
  assert.equal(progressText(b), 'Scanning: 1 of 3 page(s) scanned, 1 failed... 2 of 3 mark(s) still to review.');
  const done = batch('complete', [page(0, 'done')]);
  assert.ok(!isActive(done));
  assert.equal(progressText(done), 'Finished: 1 of 1 page(s) scanned.');
  assert.equal(progressText(null), '');
});

test('pageBadge', () => {
  const b = batch('running', [page(0, 'running'), page(1, 'pending'), page(2, 'failed'),
    page(3, 'done', { unreviewed: 1, total: 2 }), page(4, 'done', { total: 2 }), page(5, 'done'),
    page(6, 'done', {}, { review_complete: true }), page(7, 'skipped')]);
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => pageBadge(b, i)),
    ['scanning', 'queued', 'failed', 'review', 'reviewed', 'review', 'reviewed', 'skipped', '']);
  assert.equal(pageBadge(null, 0), '');
});

test('nextQueueItem skips pins with unsaved edits and the pin on screen', () => {
  const items = [{ scan_id: 's1', pin_id: 'a' }, { scan_id: 's2', pin_id: 'a' }, { scan_id: 's2', pin_id: 'b' }];
  const busy = (s, p) => s === 's1' && p === 'a';
  assert.deepEqual(nextQueueItem(items, busy), items[1]);
  assert.deepEqual(nextQueueItem(items, busy, { scan_id: 's2', pin_id: 'a' }), items[2]);
  assert.equal(nextQueueItem([], busy), null);
});
