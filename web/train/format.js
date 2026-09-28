// Pure helpers for the training pages (unit-tested in tests/viewer/test_training.mjs).

export const JOB_NAMES = {
  build_dataset: 'Build dataset', train_verifier: 'Train verifier', train_detector: 'Train detector',
  benchmark: 'Benchmark',
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
