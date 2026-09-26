// Batch scans in the browser (contracts §5a): pure helpers, no DOM.
//
// Pages are shown to people numbered from 1 and sent to the API as
// page_index, numbered from 0.

// "1-3, 7" -> [0, 1, 2, 6]. Blank means every page (null). Throws an Error
// with a message fit to show the reviewer.
export function parsePages(text, pageCount) {
  const s = String(text || '').trim();
  if (!s || s.toLowerCase() === 'all') return null;
  const out = [];
  const seen = new Set();
  for (const part of s.split(',')) {
    const t = part.trim();
    if (!t) continue;
    const m = t.match(/^(\d+)\s*(?:-\s*(\d+))?$/);
    if (!m) throw new Error(`"${t}" is not a page number or range (for example 1-5, 8).`);
    const a = Number(m[1]);
    const b = m[2] === undefined ? a : Number(m[2]);
    if (a < 1 || b < 1) throw new Error('Pages are numbered from 1.');
    if (b < a) throw new Error(`"${t}" runs backwards.`);
    if (b > pageCount) throw new Error(`This document has ${pageCount} page(s); "${t}" is past the end.`);
    for (let p = a; p <= b; p++) {
      if (!seen.has(p)) {
        seen.add(p);
        out.push(p - 1);
      }
    }
  }
  if (!out.length) throw new Error('No pages listed.');
  return out;
}

export function isActive(batch) {
  return !!batch && (batch.status === 'queued' || batch.status === 'running');
}

// One line for the panel, e.g. "Scanning: 12 of 60 pages done, 1 failed."
export function progressText(batch) {
  if (!batch) return '';
  const c = batch.page_counts;
  const finished = c.done + c.failed + c.skipped;
  const bits = [`${c.done} of ${c.total} page(s) scanned`];
  if (c.failed) bits.push(`${c.failed} failed`);
  if (c.skipped) bits.push(`${c.skipped} skipped`);
  const lead = { queued: 'Waiting to start', running: 'Scanning', complete: 'Finished',
    cancelled: 'Cancelled' }[batch.status] || batch.status;
  const p = batch.pin_counts;
  const pins = p.total ? ` ${p.unreviewed} of ${p.total} mark(s) still to review.` : '';
  return `${lead}: ${bits.join(', ')}${finished < c.total && batch.status !== 'queued' ? '...' : '.'}${pins}`;
}

// Status of one page for its page button: "" when not in the batch.
//   scanning | queued | failed | skipped | review (has unreviewed marks) | reviewed
export function pageBadge(batch, pageIndex) {
  const p = batch && batch.pages.find((x) => x.page_index === pageIndex);
  if (!p) return '';
  if (p.status === 'running') return 'scanning';
  if (p.status === 'pending') return 'queued';
  if (p.status === 'failed' || p.status === 'skipped') return p.status;
  // A page with no marks still needs a look for missed symbols.
  return p.counts.unreviewed || (p.counts.total === 0 && !p.review_complete) ? 'review' : 'reviewed';
}

// The first queue item the reviewer has not already acted on locally.
// ``busy(scanId, pinId)`` is true while an edit to that pin is unsaved;
// ``skip`` is the pin being looked at now, so "next" moves on from it.
export function nextQueueItem(items, busy, skip = null) {
  return items.find((i) => !busy(i.scan_id, i.pin_id)
    && !(skip && skip.scan_id === i.scan_id && skip.pin_id === i.pin_id)) || null;
}
