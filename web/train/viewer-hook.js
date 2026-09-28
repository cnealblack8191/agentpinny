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
    await training.markReviewed(cur.version, cur.page);
    status.textContent = `Page ${cur.page + 1} is marked fully reviewed.`;
    status.className = 'status ok';
  } catch (err) {
    status.textContent = err.message;
    status.className = 'status error';
  } finally {
    btn.disabled = false;
  }
};
