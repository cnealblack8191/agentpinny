// Pure helpers for the training pages (unit-tested in tests/viewer/test_training.mjs).

export const JOB_NAMES = {
  build_dataset: 'Build dataset', train_verifier: 'Train verifier', train_detector: 'Train detector',
  benchmark: 'Benchmark', train_symbol: 'Train symbol type',
};
export const ACTIVE_STATES = new Set(['queued', 'running']);

export function pct(v, digits = 1) {
  return typeof v === 'number' && isFinite(v) ? `${(v * 100).toFixed(digits)} %` : '–';
}

export function num(v, digits = 3) {
  return typeof v === 'number' && isFinite(v) ? v.toFixed(digits) : '–';
}

export function shortId(id, n = 12) {
  return typeof id === 'string' ? id.slice(0, n) : '';
}

export function when(t) {
  // Job times are Unix seconds; dataset and model times are RFC 3339 strings.
  if (t == null || t === '') return '';
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t);
  return isNaN(d.getTime()) ? String(t) : d.toLocaleString();
}

// The viewer opens a document, page and scan from its URL fragment
// (web/app.js readHash); `pin` selects one pin once the scan is shown.
export function viewerLink({ document_version, page_index, scan_id, pin_id }) {
  const q = new URLSearchParams();
  q.set('v', document_version);
  q.set('p', String(page_index));
  if (scan_id) q.set('s', scan_id);
  if (pin_id) q.set('pin', pin_id);
  return `index.html#${q.toString()}`;
}

export function isActiveJob(job) {
  return !!job && ACTIVE_STATES.has(job.status);
}

export function jobSummary(job) {
  const name = JOB_NAMES[job.kind] || job.kind;
  const p = job.payload || {};
  const what = [];
  if (p.dataset_id) what.push(`dataset ${shortId(p.dataset_id)}`);
  if (p.model_id) what.push(p.model_id);
  if (p.epochs) what.push(`${p.epochs} epochs`);
  return what.length ? `${name}: ${what.join(', ')}` : name;
}

export function jobOutcome(job) {
  if (job.status === 'failed' || job.status === 'cancelled') {
    return job.error ? job.error.message : job.status;
  }
  const r = job.result;
  if (job.status !== 'done' || !r) return '';
  if (job.kind === 'build_dataset') return `dataset ${shortId(r.dataset_id)}${r.reused ? ' (unchanged)' : ''}`;
  if (job.kind === 'benchmark') return r.promote ? 'gate: promote' : 'gate: do not promote';
  if (job.kind === 'train_symbol') return `${r.tag}: ${r.active ? 'switched on' : 'saved, off'}`;
  return r.model_id || '';
}

// Newest finished benchmark of a model, or null.
export function latestBenchmark(model) {
  const b = (model && model.benchmarks) || [];
  return b.length ? b[0] : null;
}

// Rows for the side-by-side table: the template baseline and the candidate
// from the candidate's benchmark, and the active model from its own newest
// benchmark (on the same dataset when there is one).
export function compareColumns(candidate, active) {
  const cb = latestBenchmark(candidate);
  const cols = [];
  if (cb) {
    cols.push({ label: `Template baseline`, side: cb.baseline, dataset: cb.dataset_id });
    cols.push({ label: `Candidate ${shortId(candidate.model_id, 24)}`, side: cb.candidate, dataset: cb.dataset_id });
  }
  if (active && candidate && active.model_id !== candidate.model_id) {
    const all = active.benchmarks || [];
    const same = cb ? all.find((b) => b.dataset_id === cb.dataset_id) : null;
    const ab = same || all[0] || null;
    cols.push({ label: `Active ${shortId(active.model_id, 24)}`, side: ab ? ab.candidate : null,
      dataset: ab ? ab.dataset_id : null, missing: !ab });
  }
  return cols;
}

// Whether the Promote button may be enabled: admin, the model is not
// already active, and its newest benchmark's gate recommends it.
export function canPromote(model, role) {
  const b = latestBenchmark(model);
  return role === 'admin' && !!b && b.promote === true && !model.active;
}

// ------------------------------------------------------------ team progress
// Local calendar day (YYYY-MM-DD) of a Date, in the browser's time zone.
export function localDay(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// The last `days` local days, oldest first, ending today.
export function dayKeys(days, now = new Date()) {
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    out.push(localDay(d));
  }
  return out;
}

// Review actions per person per local day from the server's UTC hour
// buckets ({reviewer, hour: 'YYYY-MM-DDTHH', actions}). Returns a Map from
// reviewer (null for unattributed) to an array aligned with `keys`.
export function dailyActivity(hourly, keys) {
  const index = new Map(keys.map((k, i) => [k, i]));
  const out = new Map();
  for (const b of hourly || []) {
    const i = index.get(localDay(new Date(`${b.hour}:00:00Z`)));
    if (i === undefined) continue;
    if (!out.has(b.reviewer)) out.set(b.reviewer, keys.map(() => 0));
    out.get(b.reviewer)[i] += b.actions;
  }
  return out;
}

// 0-4 shade for a heat-map cell, relative to the busiest cell.
export function level(n, max) {
  if (!n || !max) return 0;
  return Math.min(4, 1 + Math.floor((3 * n) / max));
}

// "3 min ago", "5 h ago", "2 days ago"; '' for no time.
export function ago(t, now = Date.now()) {
  if (!t) return '';
  const d = typeof t === 'number' ? t * 1000 : Date.parse(t);
  if (isNaN(d)) return String(t);
  const s = Math.max(0, (now - d) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const n = Math.floor(s / 86400);
  return n === 1 ? 'yesterday' : `${n} days ago`;
}
