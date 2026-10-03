// node --test tests/viewer/test_training.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { JOB_NAMES, ago, canPromote, compareColumns, dailyActivity, dayKeys, jobOutcome, jobSummary, latestBenchmark, level, localDay, pct, viewerLink } from '../../web/train/format.js';

const side = (p, r) => ({ counts: { true_positives: 1, false_positives: 0, false_negatives: 0 },
  metrics: { precision: p, recall: r } });
const bench = (id, promote, dataset = 'd'.repeat(64)) => ({ job_id: id, promote, dataset_id: dataset,
  baseline: side(0.8, 0.9), candidate: side(0.95, 0.92) });

test('viewerLink opens the document, page, scan and pin', () => {
  const href = viewerLink({ document_version: 'sha256:' + 'a'.repeat(64), page_index: 2, scan_id: 's1', pin_id: 'p 1' });
  assert.ok(href.startsWith('index.html#'));
  const q = new URLSearchParams(href.split('#')[1]);
  assert.equal(q.get('v'), 'sha256:' + 'a'.repeat(64));
  assert.equal(q.get('p'), '2');
  assert.equal(q.get('s'), 's1');
  assert.equal(q.get('pin'), 'p 1');
  assert.equal(new URLSearchParams(viewerLink({ document_version: 'v', page_index: 0 }).split('#')[1]).has('pin'), false);
});

test('Promote needs an admin, a recommending gate and a model that is not active yet', () => {
  const m = { model_id: 'verifier-1', active: false, benchmarks: [bench('j2', true), bench('j1', false)] };
  assert.equal(canPromote(m, 'admin'), true);
  assert.equal(canPromote(m, 'reviewer'), false);
  assert.equal(canPromote({ ...m, active: true }, 'admin'), false);
  assert.equal(canPromote({ ...m, benchmarks: [bench('j3', false), bench('j2', true)] }, 'admin'), false); // newest wins
  assert.equal(canPromote({ ...m, benchmarks: [] }, 'admin'), false);
  assert.equal(latestBenchmark(m).job_id, 'j2');
});

test('compareColumns: baseline, candidate, and the active model on the same dataset', () => {
  const cand = { model_id: 'verifier-new', benchmarks: [bench('j2', true, 'x'.repeat(64))] };
  const active = { model_id: 'verifier-old', benchmarks: [bench('j0', true, 'y'.repeat(64)), bench('j1', true, 'x'.repeat(64))] };
  const cols = compareColumns(cand, active);
  assert.deepEqual(cols.map((c) => c.label.split(' ')[0]), ['Template', 'Candidate', 'Active']);
  assert.equal(cols[2].dataset, 'x'.repeat(64));
  assert.equal(compareColumns(cand, cand).length, 2); // the active model is not compared with itself
  const none = compareColumns(cand, { model_id: 'verifier-old', benchmarks: [] });
  assert.equal(none[2].missing, true);
  assert.deepEqual(compareColumns({ model_id: 'm', benchmarks: [] }, null), []);
});

test('job text', () => {
  assert.equal(jobSummary({ kind: 'train_verifier', payload: { dataset_id: 'f'.repeat(64), epochs: 5 } }),
    'Train verifier: dataset ffffffffffff, 5 epochs');
  assert.equal(jobOutcome({ kind: 'benchmark', status: 'done', result: { promote: true } }), 'gate: promote');
  assert.equal(jobOutcome({ kind: 'build_dataset', status: 'failed', error: { code: 'x', message: 'Nothing.' } }), 'Nothing.');
  assert.equal(pct(0.5), '50.0 %');
  assert.equal(pct(null), '–');
});

test('a symbol-type training job has a name and an outcome', () => {
  assert.equal(JOB_NAMES.train_symbol, 'Train symbol type');
  assert.equal(jobOutcome({ kind: 'train_symbol', status: 'done', result: { tag: 'D', active: true } }), 'D: switched on');
  assert.equal(jobOutcome({ kind: 'train_symbol', status: 'done', result: { tag: 'Q', active: false } }), 'Q: saved, off');
});

test('dayKeys lists the last n local days, oldest first', () => {
  const now = new Date(2026, 9, 3, 15, 0);
  assert.deepEqual(dayKeys(3, now), ['2026-10-01', '2026-10-02', '2026-10-03']);
  assert.deepEqual(dayKeys(2, new Date(2026, 2, 1, 9)), ['2026-02-28', '2026-03-01']);
});

test('dailyActivity buckets UTC hours into local days per person', () => {
  const hourUtc = (y, m, d, h) => new Date(Date.UTC(y, m, d, h)).toISOString().slice(0, 13);
  const at = new Date(Date.UTC(2026, 9, 2, 12));
  const keys = [localDay(new Date(at.getTime() - 86400000)), localDay(at)];
  const out = dailyActivity([
    { reviewer: 'a@x', hour: hourUtc(2026, 9, 2, 12), actions: 3 },
    { reviewer: 'a@x', hour: hourUtc(2026, 9, 2, 12), actions: 2 },
    { reviewer: null, hour: hourUtc(2026, 9, 1, 12), actions: 1 },
    { reviewer: 'b@x', hour: '2020-01-01T00', actions: 9 },
  ], keys);
  assert.deepEqual(out.get('a@x'), [0, 5]);
  assert.deepEqual(out.get(null), [1, 0]);
  assert.equal(out.has('b@x'), false);
});

test('level shades relative to the busiest cell', () => {
  assert.equal(level(0, 10), 0);
  assert.equal(level(1, 10), 1);
  assert.equal(level(10, 10), 4);
  assert.equal(level(5, 0), 0);
});

test('ago says how long ago', () => {
  const now = Date.parse('2026-10-03T12:00:00Z');
  assert.equal(ago(null, now), '');
  assert.equal(ago('2026-10-03T11:59:30Z', now), 'just now');
  assert.equal(ago('2026-10-03T11:30:00Z', now), '30 min ago');
  assert.equal(ago('2026-10-03T07:00:00Z', now), '5 h ago');
  assert.equal(ago('2026-10-02T10:00:00Z', now), 'yesterday');
  assert.equal(ago(now / 1000 - 3 * 86400, now), '3 days ago');
});
