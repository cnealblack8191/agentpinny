// Unit tests for web/legend-core.js (the legend workflow's pure helpers).
//   node --test tests/viewer/test_legend.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import * as C from '../../web/legend-core.js';
import { readFileSync } from 'node:fs';

test('tag colours follow the legend order and match legend.css', () => {
  const tags = ['D', 'G', 'DD'];
  assert.equal(C.tagColorIndex(tags, 'D'), 0);
  assert.equal(C.tagColorIndex(tags, 'DD'), 2);
  const other = C.tagColorIndex(tags, 'ZZ');
  assert.ok(other >= 0 && other < C.PALETTE.length);
  assert.equal(C.tagColorIndex(tags, 'ZZ'), other); // stable
  const css = readFileSync(new URL('../../web/legend.css', import.meta.url), 'utf8');
  C.PALETTE.forEach((c, i) => {
    assert.ok(css.includes(`.tag-c${i} { background: ${c.bg}; color: ${c.fg}; }`), `tag-c${i}`);
  });
});

test('next sheet to review skips reviewed and failed sheets and wraps', () => {
  const sheets = [
    { page_index: 1, scan_id: 'a', review: 'reviewed' },
    { page_index: 2, scan_id: 'b', review: 'to_review' },
    { page_index: 3, scan_id: null, review: null, error: { message: 'x' } },
    { page_index: 4, scan_id: 'd', review: 'ready_to_mark' },
  ];
  assert.equal(C.nextSheet(sheets, 0).page_index, 2);
  assert.equal(C.nextSheet(sheets, 2).page_index, 4);
  assert.equal(C.nextSheet(sheets, 4).page_index, 2); // wraps
  assert.equal(C.nextSheet([sheets[0]], 1), null);
  assert.equal(C.sheetStatusText(sheets[2]), 'not saved: x');
  assert.equal(C.sheetStatusText(sheets[3]), 'ready to mark reviewed');
});

test('counts rows for the whole set and for one sheet', () => {
  const t = (found, approved) => ({ ...C.emptyTally(), found, approved, confirmed: approved });
  const counts = {
    run_id: 'r', tags: [{ tag: 'D', name: 'Duplex', counted: true }, { tag: 'Q', name: 'Quad', counted: true },
      { tag: '', name: 'No tag', counted: false }],
    sheets: [{ page_index: 1, by_tag: { D: t(3, 1) }, all: t(3, 1) }],
    totals: { by_tag: { D: t(3, 1) }, all: t(3, 1) },
  };
  const whole = C.countsRows(counts, '');
  assert.deepEqual(whole.rows.map((r) => r.tag), ['D', 'Q']); // counted tags always shown
  assert.equal(whole.rows[1].n.found, 0);
  assert.equal(whole.total.found, 3);
  assert.equal(C.countsRows(counts, '1').rows[0].n.approved, 1);
  assert.deepEqual(C.countsRows(null), { rows: [], total: null });
});

test('the default tag for a new pin', () => {
  const pins = [{ class_label: 'G', state: 'approved' }, { class_label: 'G', state: 'unreviewed' },
    { class_label: 'D', state: 'approved' }, { class_label: 'D', state: 'rejected' },
    { class_label: 'D', state: 'rejected' }];
  assert.equal(C.defaultPinTag(['D', 'G'], pins, ''), 'G'); // rejected pins do not count
  assert.equal(C.defaultPinTag(['D', 'G'], pins, 'D'), 'D'); // last used wins
  assert.equal(C.defaultPinTag(['D', 'G'], pins, 'X'), 'G'); // unless it left the legend
  assert.equal(C.defaultPinTag(['D', 'G'], [], ''), 'D');
  assert.equal(C.defaultPinTag([], [], ''), '');
});

test('run and legend summaries', () => {
  assert.equal(C.runText({ status: 'running', sheets_done: 3, sheets_total: 40 }),
    'Scanning: 3 of 40 sheet(s) searched...');
  assert.ok(C.isActiveRun({ status: 'recording' }) && !C.isActiveRun({ status: 'done' }));
  const done = { status: 'done', sheets_total: 2, elapsed_seconds: 1.2,
    sheets: [{ counts: { D: 2, G: 1 } }, { counts: { D: 1 }, error: null }] };
  assert.match(C.runText(done), /found 4 symbol\(s\) in 1 s/);
  const lg = { page_index: 0, confirmed: true, confirmed_by: 'a@b.c',
    entries: [{ count: true, status: 'ok' }, { count: false, status: 'check' }] };
  assert.equal(C.legendSummary(lg),
    'Legend on page 1: 2 symbol(s), 1 counted. 1 need a look (marked !). Confirmed by a@b.c.');
  assert.match(C.comparisonText({ engineer: 'HR', match: 3, new: [{ tag: 'X' }], changed: [], missing: [] }),
    /3 match, 1 new \(X\)/);
});

test('runText says what the learned symbol types did', () => {
  const run = { status: 'done', sheets_total: 2, elapsed_seconds: 3, learned_tags: ['D', 'Q', 'G'],
    learned: { D: { used: true }, Q: { used: true }, G: { used: false } },
    sheets: [{ counts: { D: 5 }, learned: { rejected: { D: 2 } } },
      { counts: { D: 5 }, learned: { rejected: { D: 1 }, added: { D: 1 } } }] };
  const text = C.runText(run);
  assert.match(text, /Learned from your reviews: D threw out 3 look-alike\(s\), added 1; Q changed nothing\./);
  assert.match(text, /Not used \(different legend drawing\): G\./);
  assert.equal(C.learnedText({ status: 'done', sheets: [] }), '');
});
