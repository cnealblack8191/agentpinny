// Training pages (docs/training-site.md section 3): Dashboard, Label queue,
// Datasets, Training runs and Models, one hash route each (#dashboard ...).
// Every server string goes in with textContent; nothing uses innerHTML.
// Admin actions are hidden from reviewers (the server refuses them anyway).
import { training as api } from './train/api.js';
import * as F from './train/format.js';

const $ = (id) => document.getElementById(id);
const PAGES = ['dashboard', 'queue', 'datasets', 'runs', 'models'];
const ADMIN_PAGES = new Set(['datasets', 'runs']);
const POLL_MS = 2000;

const S = { me: null, page: null, jobId: null, loading: false, active: false, seq: 0 };

// ------------------------------------------------------------------ DOM
function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'hidden' || k === 'disabled') e[k] = !!v;
    else e.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c != null && c !== false));
}

function rows(table, list) {
  fill(table.tBodies[0], list);
}

function setStatus(node, text, kind = '') {
  node.textContent = text || '';
  node.className = 'status' + (kind ? ' ' + kind : '');
}

const isAdmin = () => !!(S.me && S.me.role === 'admin');
const badge = (text, kind) => h('span', { class: `badge ${kind || ''}` }, text);

async function act(button, fn, statusNode, done) {
  button.disabled = true;
  try {
    const out = await fn();
    if (done) setStatus(statusNode, typeof done === 'function' ? done(out) : done, 'ok');
    await refresh();
    return out;
  } catch (err) {
    setStatus(statusNode, err.message, 'error');
    return null;
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------------ dashboard
function tile(value, label) {
  return h('div', { class: 'tile' }, h('div', { class: 'value' }, value), h('div', { class: 'label' }, label));
}

async function showDashboard() {
  const d = await api.dashboard();
  fill($('dash-tiles'),
    tile(d.labels.positive, 'positive labels'),
    tile(d.labels.negative, 'negative labels'),
    tile(d.labels.unlabeled, 'pins not reviewed yet'),
    tile(`${d.pins_reviewed} / ${d.pins_total}`, 'pins reviewed'),
    tile(`${d.pages.fully_reviewed} / ${d.pages.scanned}`, 'pages marked fully reviewed'),
    tile(d.pages.detector_eligible, 'pages usable for the detector'),
    tile(d.documents.uploaded, 'drawings uploaded'),
    tile(d.documents.with_labels, 'drawings with labels'));
  const card = (title, r) => h('div', { class: 'card', dataset: { readiness: title.toLowerCase() } },
    h('h3', {}, `${title} `, r.ready ? badge('ready', 'ok') : badge('not yet', 'warn')),
    h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Needs'), h('th', {}, 'Have'), h('th', {}, 'Target'))),
      h('tbody', {}, r.checks.map((c) => h('tr', {},
        h('td', {}, c.name), h('td', { class: c.ok ? 'ok' : 'bad' }, c.have), h('td', {}, c.need))))));
  fill($('dash-readiness'), card('Verifier', d.readiness.verifier), card('Detector', d.readiness.detector));
  fill($('dash-active'), h('ul', {}, ['verifier', 'detector'].map((k) =>
    h('li', {}, `${k}: `, d.active[k] ? h('span', { class: 'mono' }, d.active[k]) : 'none (template matching only)'))));
}

// ---------------------------------------------------------------- queue
async function showQueue() {
  const q = await api.queue(50);
  rows($('queue-pages'), q.pages.map((p) => {
    const mark = h('button', { dataset: { mark: p.canonical_page_id } }, 'Mark fully reviewed');
    mark.disabled = p.status !== 'ready_to_mark';
    mark.title = p.status === 'ready_to_mark' ? '' : 'Approve or reject every pin on the page first.';
    mark.onclick = () => act(mark, () => api.markReviewed(p.document_version, p.page_index), $('status'),
      `Marked ${p.filename || 'the drawing'} page ${p.page_index + 1} fully reviewed.`);
    const open = h('a', { href: F.viewerLink({ ...p, scan_id: p.first_pin ? p.first_pin.scan_id : p.latest_scan_id,
      pin_id: p.first_pin ? p.first_pin.pin_id : null }), class: 'review-link' }, 'Review');
    return h('tr', { dataset: { page: p.canonical_page_id } },
      h('td', {}, p.filename || F.shortId(p.document_version, 15)), h('td', {}, p.page_index + 1),
      h('td', {}, p.unreviewed), h('td', {}, p.best_margin == null ? '–' : F.num(p.best_margin)),
      h('td', {}, p.status === 'needs_review' ? badge('needs review', 'warn') : badge('ready to mark', 'ok')),
      h('td', {}, h('span', { class: 'actions' }, open, mark)));
  }));
  $('queue-empty').hidden = q.pages.length > 0;
  rows($('queue-pins'), q.items.map((i) => h('tr', {},
    h('td', {}, i.filename || F.shortId(i.document_version, 15)), h('td', {}, i.page_index + 1),
    h('td', {}, F.num(i.score)), h('td', {}, F.num(i.margin)),
    h('td', {}, h('a', { href: F.viewerLink(i) }, 'Open')))));
}

// ------------------------------------------------------------- datasets
async function showDatasets() {
  const [{ datasets }, { jobs }] = await Promise.all([api.datasets(), api.jobs(20)]);
  const building = jobs.find((j) => j.kind === 'build_dataset' && F.isActiveJob(j));
  $('build-dataset-btn').disabled = !!building;
  if (building) setStatus($('datasets-status'), `Building a dataset (${F.pct(building.progress, 0)})...`);
  S.active = S.active || !!building;
  const epochs = () => {
    const n = Number($('train-epochs').value);
    return Number.isInteger(n) && n > 0 ? n : undefined;
  };
  rows($('datasets-table'), datasets.map((d) => {
    const v = (s) => `${d.counts.verifier[s].pos}/${d.counts.verifier[s].neg}`;
    const p = (s) => `${d.counts.detector[s].pages} (${d.counts.detector[s].points})`;
    const trainBtn = (kind) => {
      const b = h('button', { class: 'admin-only', dataset: { train: kind } }, `Train ${kind}`);
      b.hidden = !isAdmin();
      b.onclick = () => act(b, () => api.train(kind, d.dataset_id, epochs()), $('datasets-status'),
        (job) => `Started ${F.jobSummary(job)}. Follow it on Training runs.`);
      return b;
    };
    return h('tr', { dataset: { dataset: d.dataset_id } },
      h('td', { class: 'mono', title: d.dataset_id }, F.shortId(d.dataset_id), d.synthetic ? [' ', badge('synthetic', 'warn')] : null),
      h('td', {}, F.when(d.created_at)),
      h('td', {}, `train ${v('train')} · val ${v('val')} · test ${v('test')}`),
      h('td', {}, `train ${p('train')} · val ${p('val')} · test ${p('test')}`),
      h('td', {}, `${d.gate.test_documents} documents, ${d.gate.test_points} points `,
        d.gate.ok ? badge('enough to promote', 'ok') : badge('too small to promote', 'warn')),
      h('td', {}, h('span', { class: 'actions' }, trainBtn('verifier'), trainBtn('detector'))));
  }));
  $('datasets-empty').hidden = datasets.length > 0;
}

// ----------------------------------------------------------------- runs
async function showRuns() {
  const { jobs } = await api.jobs(50);
  S.active = S.active || jobs.some(F.isActiveJob);
  if (!S.jobId && jobs.length) S.jobId = jobs[0].job_id;
  rows($('runs-table'), jobs.map((j) => {
    const tr = h('tr', { dataset: { job: j.job_id }, class: j.job_id === S.jobId ? 'selected' : '' },
      h('td', { class: 'mono' }, F.shortId(j.job_id, 8)), h('td', {}, F.jobSummary(j)),
      h('td', {}, j.status), h('td', { class: 'nowrap' }, h('progress', { max: 1, value: j.progress || 0 }), ' ', F.pct(j.progress, 0)),
      h('td', {}, j.requested_by || ''), h('td', {}, F.when(j.created_at)), h('td', {}, F.jobOutcome(j)));
    tr.onclick = () => { S.jobId = j.job_id; refresh(); };
    return tr;
  }));
  $('runs-empty').hidden = jobs.length > 0;
  const detail = $('run-detail');
  detail.hidden = !S.jobId;
  if (!S.jobId) return;
  let j;
  try {
    j = await api.job(S.jobId);
  } catch (err) {
    S.jobId = null;
    detail.hidden = true;
    return;
  }
  $('run-title').textContent = F.jobSummary(j);
  $('run-summary').textContent = `${j.status}${j.requested_by ? ` · started by ${j.requested_by}` : ''}`
    + `${F.jobOutcome(j) ? ` · ${F.jobOutcome(j)}` : ''}`;
  $('run-progress').value = j.progress || 0;
  const log = $('run-log');
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = j.log_tail || '(no log yet)';
  if (atEnd) log.scrollTop = log.scrollHeight;
  const cancel = $('run-cancel-btn');
  cancel.disabled = !F.isActiveJob(j) || j.cancel_requested;
  cancel.onclick = () => act(cancel, () => api.cancel(j.job_id), $('status'), 'Cancel requested.');
}

// --------------------------------------------------------------- models
function metricsTable(cols) {
  const val = (c, f) => (c.side ? f(c.side) : '–');
  const line = (label, f) => h('tr', {}, h('th', {}, label), cols.map((c) => h('td', { class: 'num' }, val(c, f))));
  return h('table', { class: 'grid compare' },
    h('thead', {}, h('tr', {}, h('th', {}, ''), cols.map((c) => h('th', { class: 'num' }, c.label)))),
    h('tbody', {},
      line('Precision', (s) => F.pct(s.metrics.precision)),
      line('Recall', (s) => F.pct(s.metrics.recall)),
      line('True positives', (s) => s.counts.true_positives),
      line('False positives', (s) => s.counts.false_positives),
      line('Missed (false negatives)', (s) => s.counts.false_negatives),
      h('tr', {}, h('th', {}, 'Test dataset'), cols.map((c) => h('td', { class: 'num mono' },
        c.missing ? 'not benchmarked' : F.shortId(c.dataset))))));
}

function modelCard(m, activeModel, active, runningBench) {
  const bench = F.latestBenchmark(m);
  const admin = isAdmin();
  const benchBtn = h('button', { dataset: { benchmark: m.model_id } }, bench ? 'Benchmark again' : 'Benchmark');
  benchBtn.hidden = !admin;
  benchBtn.disabled = !!runningBench;
  benchBtn.onclick = () => act(benchBtn, () => api.benchmark(m.model_id), $('models-status'),
    'Benchmark started. It runs on the test split of the model\'s dataset.');
  const promoteBtn = h('button', { dataset: { promote: m.model_id } }, 'Promote');
  promoteBtn.hidden = !admin;
  promoteBtn.disabled = !F.canPromote(m, S.me && S.me.role);
  promoteBtn.title = promoteBtn.disabled ? (m.active ? 'Already active.' : 'The benchmark gate does not recommend it.') : '';
  promoteBtn.onclick = () => {
    if (!window.confirm(`Make ${m.model_id} the active ${m.kind}? Scans in ${m.kind === 'verifier'
      ? "'Template + verifier'" : "'Point detector'"} mode will use it.`)) return;
    act(promoteBtn, () => api.promote(bench.job_id), $('models-status'), `${m.model_id} is now the active ${m.kind}.`);
  };
  const gate = bench ? h('div', {},
    h('p', {}, 'Benchmark gate: ', bench.promote ? badge('recommends promotion', 'ok') : badge('does not recommend promotion', 'bad'),
      bench.synthetic_only ? [' ', badge(bench.label || 'synthetic', 'warn')] : null,
      ` · ${bench.page_count} test pages, ${bench.document_count} documents, ${bench.reference_points} points`),
    metricsTable(F.compareColumns(m, activeModel)),
    (bench.conditions || []).length ? h('ul', { class: 'conditions' }, bench.conditions.map((c) =>
      h('li', { class: c.passed ? 'ok' : 'bad' }, `${c.passed ? 'pass' : 'fail'}: ${c.requirement}`)))
      : null)
    : h('p', { class: 'muted' }, 'Not benchmarked yet.');
  return h('div', { class: `model${m.active ? ' active' : ''}`, dataset: { model: m.model_id } },
    h('h3', {}, m.model_id, ' ', m.active ? badge('active', 'ok') : badge('candidate', ''),
      m.synthetic_only ? [' ', badge('synthetic data', 'warn')] : null),
    h('p', { class: 'muted' }, `${m.kind} · ${m.arch || ''} · trained ${F.when(m.created_at)} on dataset `,
      h('span', { class: 'mono' }, F.shortId(m.dataset_id)),
      m.operating_point ? ` · threshold ${F.num(m.operating_point.threshold)}` : ''),
    runningBench ? h('p', {}, `Benchmark running (${F.pct(runningBench.progress, 0)})...`) : null,
    gate,
    h('div', { class: 'actions' }, benchBtn, promoteBtn));
}

async function showModels() {
  const [m, jobs] = await Promise.all([api.models(), isAdmin() ? api.jobs(50) : Promise.resolve({ jobs: [] })]);
  const running = new Map();
  for (const j of jobs.jobs) {
    if (j.kind === 'benchmark' && F.isActiveJob(j) && j.payload && !running.has(j.payload.model_id)) {
      running.set(j.payload.model_id, j);
    }
  }
  S.active = S.active || running.size > 0;
  const byId = new Map(m.models.map((x) => [x.model_id, x]));
  fill($('models-active'), ['verifier', 'detector'].map((k) => {
    const a = m.active[k];
    const off = h('button', { dataset: { deactivate: k } }, `Deactivate ${k}`);
    off.hidden = !isAdmin() || !a;
    off.onclick = () => {
      if (!window.confirm(`Stop using ${a.model_id}? ${k === 'verifier' ? "'Template + verifier'" : "'Point detector'"} `
        + 'scans are unavailable until another model is promoted.')) return;
      act(off, () => api.deactivate(k), $('models-status'), `No ${k} is active now.`);
    };
    return h('div', { class: 'card', dataset: { active: k } }, h('h3', {}, `Active ${k}`),
      a ? h('p', {}, h('span', { class: 'mono' }, a.model_id), h('br'), `promoted ${F.when(a.promoted_at)}`)
        : h('p', { class: 'muted' }, 'None'),
      h('div', { class: 'actions' }, off));
  }));
  const activeModel = (kind) => (m.active[kind] ? byId.get(m.active[kind].model_id) : null);
  fill($('models-list'), m.models.map((x) => modelCard(x, activeModel(x.kind), m.active, running.get(x.model_id))));
  $('models-empty').hidden = m.models.length > 0;
}

// --------------------------------------------------------------- router
const SHOW = { dashboard: showDashboard, queue: showQueue, datasets: showDatasets, runs: showRuns, models: showModels };

function route() {
  let page = location.hash.slice(1) || 'dashboard';
  if (!PAGES.includes(page) || (ADMIN_PAGES.has(page) && !isAdmin())) page = 'dashboard';
  return page;
}

async function refresh() {
  const page = route();
  if (page !== S.page) {
    S.page = page;
    setStatus($('status'), '');
  }
  for (const sec of document.querySelectorAll('[data-page]')) sec.hidden = sec.dataset.page !== page;
  for (const a of document.querySelectorAll('[data-nav]')) {
    if (a.dataset.nav === page) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  const seq = ++S.seq;
  S.loading = true;
  S.active = false;
  try {
    await SHOW[page]();
  } catch (err) {
    if (seq === S.seq) setStatus($('status'), err.message, 'error');
  } finally {
    if (seq === S.seq) {
      S.loading = false;
      document.body.dataset.loaded = page;
    }
  }
}

function poll() {
  // Live pages refresh while something is running; the log follows along.
  if (!document.hidden && !S.loading && S.active) refresh();
}

async function start() {
  try {
    S.me = await api.me();
  } catch (err) {
    if (err.status === 401) {
      location.replace('login.html?next=' + encodeURIComponent(location.pathname + location.hash));
      return;
    }
    setStatus($('status'), err.message, 'error');
    $('user-bar').textContent = err.status === 403 ? 'Not a Pinny member.' : 'Not signed in.';
    return;
  }
  const bar = $('user-bar');
  bar.textContent = `Signed in as ${S.me.email} (${S.me.role})`;
  if (S.me.sign_in) {
    const b = h('button', { type: 'button', class: 'link' }, 'Sign out');
    b.onclick = async () => {
      try { await api.logout(); } catch (err) { /* the cookie is cleared either way */ }
      location.replace('login.html');
    };
    bar.append(' · ', b);
  }
  for (const e of document.querySelectorAll('.admin-only')) e.hidden = !isAdmin();
  $('build-dataset-btn').onclick = () => act($('build-dataset-btn'), () => api.buildDataset(), $('datasets-status'),
    'Building a dataset from the current reviews. Follow it on Training runs.');
  window.addEventListener('hashchange', refresh);
  document.addEventListener('visibilitychange', poll);
  setInterval(poll, POLL_MS);
  await refresh();
}

start();
