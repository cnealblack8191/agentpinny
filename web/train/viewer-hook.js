// "Mark page fully reviewed" in the viewer (docs/training-site.md section 3).
// Kept out of app.js: it reads the open document and page from the URL
// fragment the viewer keeps up to date (#v=<version>&p=<page index>).
import { training } from './api.js';

const btn = document.getElementById('mark-reviewed-btn');
const status = document.getElementById('mark-reviewed-status');

function current() {
  const q = new URLSearchParams(location.hash.slice(1));
  const p = Number(q.get('p'));
  return q.get('v') && q.has('p') && Number.isInteger(p) ? { version: q.get('v'), page: p } : null;
}

btn.onclick = async () => {
  const cur = current();
  status.className = 'status';
  if (!cur) {
    status.textContent = 'Open a page first.';
    return;
  }
  btn.disabled = true;
  try {
    // Approvals and rejections are saved in the background: wait for them,
    // or the server still sees those pins as unreviewed.
    const outbox = () => (window.__pinny ? window.__pinny.queue() : []);
    const deadline = Date.now() + 30000;
    while (outbox().some((e) => e.status === 'pending' || e.status === 'sending') && Date.now() < deadline) {
      status.textContent = 'Saving your last changes first…';
      await new Promise((r) => setTimeout(r, 250));
    }
    if (outbox().some((e) => e.status === 'failed')) {
      throw new Error('Some changes could not be saved. Retry or discard them (left panel), then mark the page again.');
    }
    if (outbox().some((e) => e.status === 'pending' || e.status === 'sending')) {
      throw new Error('Your last changes are still being saved. Try again in a moment.');
    }
    await training.markReviewed(cur.version, cur.page);
    status.textContent = `Page ${cur.page + 1} is marked fully reviewed.`;
    status.className = 'status ok';
    document.dispatchEvent(new CustomEvent('pinny:page-reviewed', { detail: cur }));
  } catch (err) {
    status.textContent = err.message;
    status.className = 'status error';
  } finally {
    btn.disabled = false;
  }
};
