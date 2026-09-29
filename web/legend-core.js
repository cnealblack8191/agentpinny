// Legend workflow in the browser: pure helpers, no DOM (node tests drive them).
//
// Sheets are shown to people numbered from 1; the API uses page_index from 0.

// One colour per tag. The same order is in legend.css as .tag-c0 ... .tag-c9,
// so a tag's chip in a table matches its label on the canvas.
export const PALETTE = [
  { bg: '#1f5fbf', fg: '#ffffff' }, // blue
  { bg: '#c62828', fg: '#ffffff' }, // red
  { bg: '#6a3d9a', fg: '#ffffff' }, // purple
  { bg: '#8c564b', fg: '#ffffff' }, // brown
  { bg: '#00838f', fg: '#ffffff' }, // teal
  { bg: '#9e9d24', fg: '#111111' }, // olive
  { bg: '#d81b60', fg: '#ffffff' }, // pink
  { bg: '#283593', fg: '#ffffff' }, // navy
  { bg: '#ef6c00', fg: '#111111' }, // orange
  { bg: '#37474f', fg: '#ffffff' }, // slate
];

// Colour index of a tag: its place in the legend, else a stable hash.
export function tagColorIndex(tags, tag) {
  const i = (tags || []).indexOf(tag);
  if (i >= 0) return i % PALETTE.length;
  let h = 0;
  for (const ch of String(tag || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % PALETTE.length;
}

export const REVIEW_TEXT = { reviewed: 'reviewed', to_review: 'to review', ready_to_mark: 'ready to mark reviewed' };

export function sheetStatusText(row) {
  if (row.error) return `not saved: ${row.error.message}`;
  return REVIEW_TEXT[row.review] || '';
}

// The next sheet that still needs work, after the one on screen (wrapping).
export function nextSheet(sheets, currentPage) {
  const open = (sheets || []).filter((s) => s.scan_id && s.review !== 'reviewed');
  if (!open.length) return null;
  return open.find((s) => s.page_index > (currentPage ?? -1)) || open[0];
}

// Rows of the counts table for the whole set (scope "") or one sheet
// (scope = its page_index as a string): [{tag, name, counted, n}], plus the total.
export function countsRows(counts, scope = '') {
  if (!counts || !counts.run_id) return { rows: [], total: null };
  let byTag;
  let total;
  if (scope === '') {
    byTag = counts.totals.by_tag;
    total = counts.totals.all;
  } else {
    const s = counts.sheets.find((x) => String(x.page_index) === String(scope));
    byTag = s ? s.by_tag : {};
    total = s ? s.all : null;
  }
  const rows = counts.tags.filter((t) => byTag[t.tag] || t.counted)
    .map((t) => ({ tag: t.tag, name: t.name, counted: t.counted, n: byTag[t.tag] || emptyTally() }));
  return { rows, total };
}

export function emptyTally() {
  return { found: 0, approved: 0, rejected: 0, added: 0, confirmed: 0, unreviewed: 0 };
}

export function isActiveRun(run) {
  return !!run && ['queued', 'running', 'recording'].includes(run.status);
}

// One line about a whole-set scan, for the panel.
export function runText(run) {
  if (!run) return '';
  const n = run.sheets_total;
  if (run.status === 'queued') return `Waiting to start: ${n} sheet(s) to scan...`;
  if (run.status === 'running') return `Scanning: ${run.sheets_done} of ${n} sheet(s) searched...`;
  if (run.status === 'recording') return 'Saving the pins...';
  if (run.status === 'failed') return `The scan failed: ${run.error ? run.error.message : 'unknown error'}`;
  const found = run.sheets.reduce((a, s) => a + Object.values(s.counts || {}).reduce((x, y) => x + y, 0), 0);
  const bad = run.sheets.filter((s) => s.error).length;
  return `Scanned ${run.sheets.length} sheet(s) and found ${found} symbol(s)`
    + `${run.elapsed_seconds != null ? ` in ${Math.max(1, Math.round(run.elapsed_seconds))} s` : ''}.`
    + (bad ? ` ${bad} sheet(s) could not be saved.` : ' Review each sheet, then mark it fully reviewed.')
    + learnedText(run);
}

// What the learned symbol types did in a finished run ('' when none were used).
export function learnedText(run) {
  const tags = (run && run.learned_tags) || [];
  if (!tags.length) return '';
  const threw = new Map();
  const added = new Map();
  for (const s of run.sheets || []) {
    for (const [t, n] of Object.entries((s.learned && s.learned.rejected) || {})) threw.set(t, (threw.get(t) || 0) + n);
    for (const [t, n] of Object.entries((s.learned && s.learned.added) || {})) added.set(t, (added.get(t) || 0) + n);
  }
  const used = tags.filter((t) => !run.learned || !run.learned[t] || run.learned[t].used);
  const skipped = tags.filter((t) => run.learned && run.learned[t] && !run.learned[t].used);
  const parts = used.map((t) => {
    const bits = [];
    if (threw.get(t)) bits.push(`threw out ${threw.get(t)} look-alike(s)`);
    if (added.get(t)) bits.push(`added ${added.get(t)}`);
    return `${t} ${bits.length ? bits.join(', ') : 'changed nothing'}`;
  });
  return (parts.length ? ` Learned from your reviews: ${parts.join('; ')}.` : '')
    + (skipped.length ? ` Not used (different legend drawing): ${skipped.join(', ')}.` : '');
}

// The tag a new pin gets: the last one chosen, if still in the legend;
// else the counted tag found most often on this sheet; else the first counted tag.
export function defaultPinTag(countedTags, pins, lastUsed) {
  if (lastUsed && countedTags.includes(lastUsed)) return lastUsed;
  const n = new Map();
  for (const p of pins || []) {
    if (p.class_label && countedTags.includes(p.class_label) && p.state !== 'rejected' && p.state !== 'removed') {
      n.set(p.class_label, (n.get(p.class_label) || 0) + 1);
    }
  }
  let best = null;
  for (const t of countedTags) if (n.has(t) && (best === null || n.get(t) > n.get(best))) best = t;
  return best || countedTags[0] || '';
}

export function legendSummary(legend) {
  if (!legend) return '';
  const counted = legend.entries.filter((e) => e.count).length;
  const check = legend.entries.filter((e) => e.status === 'check').length;
  let s = `Legend on page ${legend.page_index + 1}: ${legend.entries.length} symbol(s), ${counted} counted.`;
  if (check) s += ` ${check} need a look (marked !).`;
  if (legend.confirmed) s += ` Confirmed${legend.confirmed_by ? ` by ${legend.confirmed_by}` : ''}.`;
  return s;
}

export function comparisonText(c) {
  if (!c) return '';
  const list = (xs) => xs.map((d) => d.tag).join(', ');
  const bits = [`${c.match} match`];
  if (c.new.length) bits.push(`${c.new.length} new (${list(c.new)})`);
  if (c.changed.length) bits.push(`${c.changed.length} changed (${c.changed.map((d) => `${d.tag}: ${d.notes.join(' ')}`).join('; ')})`);
  if (c.missing.length) bits.push(`${c.missing.length} missing from this set (${list(c.missing)})`);
  return `Compared with ${c.engineer}'s standard legend: ${bits.join(', ')}.`;
}
