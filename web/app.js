// Pinny viewer: upload -> page -> template -> scan -> review -> report.
//
// Scan modes (docs/phase2-contracts.md P7): "template" (Phase 1),
// "template+verifier" (template candidates rescored by the active verifier)
// and "model" (the active point detector, with no template step).
//
// Coordinates: every stored position (template box, pins) is in canonical
// raster pixels (contracts §2). The page image and all overlays are drawn
// through the single view matrix from transform.js; pointer positions are
// converted back with its inverse. Nothing is stored in screen units.

import { api, ApiError, newRequestId } from './api.js';
import { EditQueue, projectPins, mergePins } from './edits.js';
import * as T from './transform.js';

const $ = (id) => document.getElementById(id);
const el = {
  file: $('file'), docSelect: $('doc-select'), docStatus: $('doc-status'), pages: $('pages'),
  templateInfo: $('template-info'), threshold: $('threshold'), scanBtn: $('scan-btn'),
  scanStatus: $('scan-status'), scanSelect: $('scan-select'), counts: $('counts'),
  saveStatus: $('save-status'), failures: $('failures'), pinInfo: $('pin-info'),
  approveBtn: $('approve-btn'), rejectBtn: $('reject-btn'), nextBtn: $('next-btn'),
  showHidden: $('show-hidden'), reportLink: $('report-link'), pinTable: $('pin-table'),
  zoomIn: $('zoom-in'), zoomOut: $('zoom-out'), zoomLabel: $('zoom-label'), fitBtn: $('fit-btn'),
  rotateBtn: $('rotate-btn'), hidePins: $('hide-pins'), cursorPos: $('cursor-pos'),
  viewport: $('viewport'), canvas: $('canvas'), viewMessage: $('view-message'),
  stubBanner: $('stub-banner'),
  scanMode: $('scan-mode'), modeInfo: $('mode-info'), templateStep: $('template-step'),
  templateModeLabel: $('template-mode-label'),
};

const HIDDEN_STATES = new Set(['rejected', 'removed']);
const CLICK_SLOP = 4; // CSS px a pointer may move and still count as a click
const MIN_TEMPLATE_SIDE = 8; // detector minimum (ScanSettings.min_template_side)
const SCAN_MODE_KEY = 'pinny.scanMode';
const MODE_NAMES = { template: 'Template match', 'template+verifier': 'Template + verifier',
  model: 'Point detector (no template)' };

const S = {
  docs: [],
  doc: null, // {document_id, document_version, filename, page_count}
  page: null,
  frame: null,
  image: null,
  view: { zoom: 1, rotation: 0, panX: 0, panY: 0 },
  css: { w: 0, h: 0 },
  mode: 'pan',
  scanMode: 'template', // P7 scan mode
  models: null, // GET /api/models: {modes: [...], active: {...}}
  template: null,
  dragBox: null,
  drag: null,
  spaceDown: false,
  scans: [],
  scanId: null,
  scan: null,
  serverPins: [],
  selected: null,
  scanning: false,
  lastScanRequest: null,
  // Sequence numbers: a response is applied only if its number is still current.
  seq: { doc: 0, page: 0, scan: 0, scanReq: 0 },
};

// ------------------------------------------------------------ edit queue
let storage = null;
try { storage = window.localStorage; } catch (e) { storage = null; }

const queue = new EditQueue({
  storage,
  send: (e) => {
    const body = { action: e.action, request_id: e.request_id };
    if (e.action === 'add_manual') {
      body.x = e.x;
      body.y = e.y;
    } else {
      body.pin_id = e.pin_id;
      const known = e.scan_id === S.scanId && S.serverPins.find((p) => p.pin_id === e.pin_id);
      if (known) body.expected_version = known.version;
    }
    return api.act(e.scan_id, body);
  },
  onSaved: (e, result) => {
    if (e.scan_id !== S.scanId) return; // saved; not on screen
    S.serverPins = mergePins(S.serverPins, [result.pin]);
    if (S.selected === 'tmp:' + e.request_id) S.selected = result.pin.pin_id;
    render();
  },
  onChange: () => {
    const failed = queue.entries.filter((x) => x.status === 'failed' && x.scan_id === S.scanId);
    // A conflict means the server state differs from what we show: reload it.
    if (failed.some((x) => x.error && (x.error.status === 409 || x.error.status === 404) && !x.reloaded)) {
      failed.forEach((x) => { x.reloaded = true; });
      if (S.scanId) loadScan(S.scanId, { refresh: true });
    }
    render();
  },
});

// ----------------------------------------------------------- rendering
function displayPins() {
  if (!S.scanId) return [];
  return projectPins(S.serverPins, queue.entries, S.scanId);
}

function visiblePins() {
  return displayPins().filter((p) => el.showHidden.checked || !HIDDEN_STATES.has(p.state)
    || p.pin_id === S.selected || p.failed);
}

function sizeCanvas() {
  const r = el.viewport.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  S.css = { w: r.width, h: r.height };
  el.canvas.style.width = r.width + 'px';
  el.canvas.style.height = r.height + 'px';
  el.canvas.width = Math.max(1, Math.round(r.width * dpr));
  el.canvas.height = Math.max(1, Math.round(r.height * dpr));
}

function draw() {
  const ctx = el.canvas.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = '#d8d8d8';
  ctx.fillRect(0, 0, el.canvas.width, el.canvas.height);
  if (!S.image || !S.frame || !S.css.w) return;
  // Device px per CSS px, per axis (exact after rounding the backing store).
  const kx = el.canvas.width / S.css.w;
  const ky = el.canvas.height / S.css.h;
  const m = T.matrix(S.view);
  ctx.setTransform(kx * m.a, ky * m.b, kx * m.c, ky * m.d, kx * m.e, ky * m.f);
  ctx.imageSmoothingEnabled = S.view.zoom < 1;
  ctx.drawImage(S.image, 0, 0);

  // Overlays: positions come from the same matrix, sizes stay in CSS px.
  ctx.setTransform(kx, 0, 0, ky, 0, 0);
  polygon(ctx, T.boxCorners(S.view, { x: 0, y: 0, width: S.frame.width, height: S.frame.height }),
    '#666', 1, []);
  if (S.template && needsTemplate()) polygon(ctx, T.boxCorners(S.view, S.template), '#2a6df4', 2, [6, 4]);
  if (S.dragBox) polygon(ctx, T.boxCorners(S.view, S.dragBox), '#2a6df4', 1.5, [3, 3]);
  if (el.hidePins.checked) return;
  const pins = visiblePins();
  const sel = pins.find((p) => p.pin_id === S.selected);
  if (sel && sel.box) polygon(ctx, T.boxCorners(S.view, sel.box), '#2a6df4', 1, []);
  for (const p of pins) drawPin(ctx, p, p.pin_id === S.selected);
}

function polygon(ctx, pts, color, width, dash) {
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
  ctx.closePath();
  ctx.setLineDash(dash);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
  ctx.setLineDash([]);
}

function ring(ctx, x, y, r, color, width, dash = []) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.setLineDash(dash);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
  ctx.setLineDash([]);
}

function dot(ctx, x, y, r, color) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

// Pin colours are chosen to differ from the stub raster's red markers.
const PIN_STYLE = {
  unreviewed: '#ff8c00',
  approved: '#0a8f2a',
  added: '#ff00ff',
  rejected: '#777777',
  removed: '#777777',
};

function drawPin(ctx, p, selected) {
  const s = T.toScreen(S.view, p.x, p.y);
  if (s.x < -20 || s.y < -20 || s.x > S.css.w + 20 || s.y > S.css.h + 20) return;
  const color = PIN_STYLE[p.state] || '#000';
  const dash = p.pending ? [3, 2] : [];
  if (p.state === 'rejected') {
    ctx.beginPath();
    ctx.moveTo(s.x - 5, s.y - 5); ctx.lineTo(s.x + 5, s.y + 5);
    ctx.moveTo(s.x + 5, s.y - 5); ctx.lineTo(s.x - 5, s.y + 5);
    ctx.setLineDash(dash);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.setLineDash([]);
  } else if (p.state === 'removed') {
    ring(ctx, s.x, s.y, 7, color, 1.5, [2, 2]);
  } else {
    ring(ctx, s.x, s.y, 7, color, 2, dash);
    if (p.state !== 'unreviewed') dot(ctx, s.x, s.y, 1.5, color);
  }
  if (p.failed) ring(ctx, s.x, s.y, 11, '#b00020', 2);
  if (selected) ring(ctx, s.x, s.y, p.failed ? 14 : 11, '#2a6df4', 2);
  if (p.score != null && (selected || S.view.zoom >= 0.25)) {
    const label = scoreLabel(p, 2);
    ctx.font = '11px system-ui, sans-serif';
    const w = ctx.measureText(label).width;
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.fillRect(s.x + 9, s.y - 20, w + 4, 13);
    ctx.fillStyle = '#111';
    ctx.fillText(label, s.x + 11, s.y - 10);
  }
}

// --------------------------------------------------------------- panel
function setStatus(node, text, kind = '') {
  node.textContent = text;
  node.className = 'status' + (kind ? ' ' + kind : '');
}

function setViewMessage(text, isError = false) {
  el.viewMessage.textContent = text || '';
  el.viewMessage.className = 'view-message' + (isError ? ' error' : '');
}

function pinLabel(p) {
  if (p.temp) return 'new pin';
  return p.origin === 'machine' ? p.pin_id : 'manual ' + p.pin_id.slice(0, 6);
}

// "0.93" for one score; "v 0.93 · t 0.85" when a verifier rescored a template match.
function scoreLabel(p, digits) {
  if (p.verifier_score != null && p.template_score != null) {
    return `v ${p.verifier_score.toFixed(digits)} · t ${p.template_score.toFixed(digits)}`;
  }
  return p.score != null ? p.score.toFixed(digits) : '-';
}

function modeInfo(mode) {
  return S.models && S.models.modes.find((m) => m.mode === mode);
}

function needsTemplate(mode = S.scanMode) {
  return mode !== 'model';
}

function renderModes() {
  for (const opt of el.scanMode.options) {
    const m = modeInfo(opt.value);
    const name = MODE_NAMES[opt.value] || opt.value;
    const label = m && m.model_id ? `${name}: ${m.model_id}`
      : name + (m && !m.available ? ' (no active model)' : '');
    opt.disabled = !!(S.models && m && !m.available);
    if (opt.textContent !== label) opt.textContent = label; // don't disturb an open list
  }
  if (el.scanMode.value !== S.scanMode) el.scanMode.value = S.scanMode;
  const m = modeInfo(S.scanMode);
  let text = '';
  if (!S.models) text = 'Checking which models are active...';
  else if (S.scanMode === 'template') text = 'Template matching only (no learned model).';
  else if (m && m.available) {
    text = `Active ${m.kind}: ${m.model_id}`
      + (m.threshold != null ? `, threshold ${Number(m.threshold).toFixed(2)}` : '')
      + (m.synthetic_only ? ' (trained on synthetic data only)' : '') + '.';
    if (S.scanMode === 'template+verifier') text += ' Matches below the threshold are suppressed.';
    else text += ' No template box is needed.';
  } else if (m) text = m.reason || 'This mode is not available.';
  el.modeInfo.textContent = text;
  el.modeInfo.className = m && !m.available ? 'error' : 'muted';
  el.templateStep.hidden = !needsTemplate();
  el.templateModeLabel.hidden = !needsTemplate();
}

function sortedPins(pins) {
  return [...pins].sort((a, b) => {
    if (a.origin !== b.origin) return a.origin === 'machine' ? -1 : 1;
    if (a.origin === 'machine') return (b.score ?? 0) - (a.score ?? 0);
    return 0;
  });
}

function renderPanel() {
  el.zoomLabel.textContent = S.frame ? Math.round(S.view.zoom * 1000) / 10 + '%' : '-';
  el.viewport.className = 'mode-' + S.mode + (S.drag && S.drag.kind === 'pan' ? ' dragging' : '');
  for (const r of document.querySelectorAll('input[name=mode]')) r.checked = r.value === S.mode;

  // Template
  el.templateInfo.textContent = S.template
    ? `Template: x ${S.template.x}, y ${S.template.y}, ${S.template.width} x ${S.template.height} px`
    : 'No template selected.';
  const c = S.scanId ? queue.counts(S.scanId) : { pending: 0, failed: 0 };
  const templateOk = !needsTemplate() || (S.template && S.template.width >= MIN_TEMPLATE_SIDE
    && S.template.height >= MIN_TEMPLATE_SIDE);
  const mode = modeInfo(S.scanMode);
  const modeOk = S.scanMode === 'template' || !!(mode && mode.available);
  el.scanBtn.disabled = !S.frame || !templateOk || !modeOk || S.scanning || c.pending > 0
    || c.failed > 0;
  renderModes();
  el.scanBtn.textContent = S.scanId ? 'Scan this page again' : 'Scan this page';
  el.scanBtn.title = c.pending ? 'Wait until your edits are saved.'
    : c.failed ? 'Retry or discard the failed edits first.' : '';

  // Scan picker
  el.scanSelect.disabled = !S.scans.length;
  const opts = ['<option value="">- none -</option>'].concat(S.scans.map((s, i) =>
    `<option value="${s.scan_id}">Scan ${i + 1} (${fmtTime(s.created_at)}, `
    + `${s.mode && s.mode !== 'template' ? escapeHtml(s.mode) + ', ' : ''}${s.counts.total} pins)</option>`));
  const html = opts.join('');
  if (el.scanSelect.dataset.html !== html) {
    el.scanSelect.innerHTML = html;
    el.scanSelect.dataset.html = html;
  }
  el.scanSelect.value = S.scanId || '';

  // Counts and save state
  const pins = displayPins();
  if (S.scanId) {
    const n = (st) => pins.filter((p) => p.state === st).length;
    const machine = pins.filter((p) => p.origin === 'machine').length;
    el.counts.textContent = `${machine} matches: ${n('unreviewed')} unreviewed, ${n('approved')} approved, `
      + `${n('rejected')} rejected. ${n('added')} added manually.`;
    if (machine === 0 && n('added') === 0) {
      el.counts.textContent += ' No matches were found. Add missed pins manually or rescan with a lower threshold.';
    }
    const st = S.scan && S.scan.scan_id === S.scanId ? S.scan : null;
    if (st && st.mode && st.mode !== 'template') {
      el.counts.textContent += ` Mode: ${st.mode} (${st.detector ? st.detector.version : '?'}).`;
      const sup = (st.suppressed || []).length;
      if (st.mode === 'template+verifier') {
        el.counts.textContent += ` ${sup} template match(es) suppressed by the verifier.`;
      }
    }
  } else {
    el.counts.textContent = S.frame ? 'Not scanned yet.' : '';
  }
  const other = queue.entries.filter((e) => e.scan_id !== S.scanId && e.status !== 'failed').length;
  const otherFailed = queue.entries.filter((e) => e.scan_id !== S.scanId && e.status === 'failed').length;
  let msg = '';
  let kind = '';
  if (c.failed) { msg = `${c.failed} edit(s) not saved. Retry or discard below.`; kind = 'error'; }
  else if (c.pending) msg = `Saving ${c.pending} edit(s)...`;
  else if (S.scanId) { msg = 'All changes saved.'; kind = 'ok'; }
  if (other) msg += ` Saving ${other} edit(s) on another scan.`;
  if (otherFailed) { msg += ` ${otherFailed} edit(s) on another scan failed.`; kind = 'error'; }
  if (queue.storageOk === false && (c.pending || other)) {
    msg += ' This browser cannot keep unsaved edits across a reload; do not close the page yet.';
  }
  setStatus(el.saveStatus, msg, kind);

  // Failures
  el.failures.innerHTML = '';
  for (const e of queue.entries.filter((x) => x.status === 'failed')) {
    const div = document.createElement('div');
    div.className = 'failure';
    const what = e.action === 'add_manual'
      ? `Add pin at (${e.x.toFixed(1)}, ${e.y.toFixed(1)})` : `${actionName(e.action)} ${e.pin_id}`;
    div.textContent = `${what}${e.scan_id !== S.scanId ? ' (other scan)' : ''}: ${e.error ? e.error.message : 'failed'} `;
    const retry = document.createElement('button');
    retry.textContent = 'Retry';
    retry.onclick = () => queue.retry(e.request_id);
    const discard = document.createElement('button');
    discard.textContent = 'Discard';
    discard.onclick = () => queue.discard(e.request_id);
    div.append(retry, ' ', discard);
    el.failures.append(div);
  }

  // Selected pin
  const sel = pins.find((p) => p.pin_id === S.selected);
  if (sel) {
    el.pinInfo.textContent = `${pinLabel(sel)} | ${sel.state}${sel.pending ? ' (saving)' : ''}`
      + (sel.verifier_score != null
        ? ` | verifier ${sel.verifier_score.toFixed(3)}`
          + (sel.template_score != null ? ` | template ${sel.template_score.toFixed(3)}` : '')
        : `${sel.score != null ? ` | score ${sel.score.toFixed(3)}` : ''}`)
      + `${sel.rotation != null ? ` | rot ${sel.rotation} deg` : ''}`
      + ` | (${sel.x.toFixed(1)}, ${sel.y.toFixed(1)}) px`;
    el.pinInfo.className = '';
  } else {
    el.pinInfo.textContent = 'Click a pin (Pan mode) to review it.';
    el.pinInfo.className = 'muted';
  }
  const addEntry = sel && sel.temp ? queue.find(sel.request_id) : null;
  el.approveBtn.disabled = !sel || sel.origin !== 'machine' || sel.state === 'approved';
  el.rejectBtn.disabled = !sel || (sel.temp ? !addEntry || addEntry.status === 'sending'
    : sel.state === 'rejected' || sel.state === 'removed');
  el.rejectBtn.textContent = sel && sel.origin === 'manual' ? 'Delete pin' : 'Reject';
  el.nextBtn.disabled = !pins.some((p) => p.state === 'unreviewed');

  // Report
  const canExport = S.scanId && !c.pending && !c.failed;
  if (canExport) {
    el.reportLink.href = api.reportUrl(S.scanId);
    el.reportLink.setAttribute('download', `pinny-report-${S.scanId}.json`);
    el.reportLink.className = '';
    el.reportLink.removeAttribute('aria-disabled');
    el.reportLink.textContent = 'Export report (JSON)';
  } else {
    el.reportLink.removeAttribute('href');
    el.reportLink.className = 'disabled';
    el.reportLink.setAttribute('aria-disabled', 'true');
    el.reportLink.textContent = S.scanId ? 'Export report (JSON): waiting for edits to save'
      : 'Export report (JSON)';
  }

  // Pin table
  const rows = sortedPins(visiblePins()).map((p) =>
    `<tr data-pin="${escapeHtml(p.pin_id)}" class="${p.pin_id === S.selected ? 'selected' : ''}">`
    + `<td>${escapeHtml(pinLabel(p))}</td><td>${scoreLabel(p, 3)}</td>`
    + `<td>${p.state}${p.pending ? ' (saving)' : ''}${p.failed ? ' (not saved)' : ''}</td></tr>`).join('');
  const tbody = el.pinTable.tBodies[0];
  if (tbody.dataset.html !== rows) {
    tbody.innerHTML = rows;
    tbody.dataset.html = rows;
  }

  // Pages
  for (const b of el.pages.querySelectorAll('button')) {
    b.setAttribute('aria-pressed', String(Number(b.dataset.page) === S.page));
  }
}

function actionName(a) {
  return { approve: 'Approve', reject: 'Reject', delete_pin: 'Delete', remove_manual: 'Remove' }[a] || a;
}

function fmtTime(iso) {
  if (!iso) return '?';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let rafPending = false;
function render() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    draw();
    renderPanel();
    saveHash();
  });
}

// ------------------------------------------------------ URL state (reload)
let hashTimer = null;
function saveHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    if (!S.doc) return;
    const q = new URLSearchParams();
    q.set('v', S.doc.document_version);
    if (S.page != null) q.set('p', S.page);
    if (S.scanId) q.set('s', S.scanId);
    if (S.frame) {
      q.set('z', +S.view.zoom.toFixed(6));
      q.set('r', S.view.rotation);
      q.set('x', +S.view.panX.toFixed(3));
      q.set('y', +S.view.panY.toFixed(3));
    }
    history.replaceState(null, '', '#' + q.toString());
  }, 150);
}

function readHash() {
  const q = new URLSearchParams(location.hash.slice(1));
  const num = (k) => (q.has(k) && isFinite(Number(q.get(k))) ? Number(q.get(k)) : null);
  const view = ['z', 'r', 'x', 'y'].every((k) => num(k) !== null)
    ? { zoom: T.clampZoom(num('z')), rotation: T.normRotation(num('r')), panX: num('x'), panY: num('y') }
    : null;
  return { version: q.get('v'), page: num('p'), scanId: q.get('s'), view };
}

// ------------------------------------------------------------- loading
async function refreshDocuments() {
  try {
    const { documents } = await api.documents();
    S.docs = documents;
    el.docSelect.innerHTML = '<option value="">- uploaded documents -</option>' + documents.map((d) =>
      `<option value="${escapeHtml(d.document_version)}">${escapeHtml(d.filename)} (${d.page_count} p, ${d.document_version.slice(7, 15)})</option>`).join('');
    el.docSelect.value = S.doc ? S.doc.document_version : '';
  } catch (err) {
    setStatus(el.docStatus, err.message, 'error');
  }
}

function openDocument(doc, restore = null) {
  S.seq.doc += 1;
  S.doc = doc;
  el.docSelect.value = doc.document_version;
  el.pages.innerHTML = '';
  for (let i = 0; i < doc.page_count; i++) {
    const b = document.createElement('button');
    b.textContent = `Page ${i + 1}`;
    b.title = `page_index ${i}`;
    b.dataset.page = i;
    b.onclick = () => selectPage(i);
    el.pages.append(b);
  }
  setStatus(el.docStatus, `${doc.filename}: ${doc.page_count} page(s).`);
  clearPage();
  if (restore && restore.page != null && restore.page < doc.page_count) {
    selectPage(restore.page, restore);
  } else if (doc.page_count === 1) {
    selectPage(0);
  } else {
    setViewMessage('Choose a page.');
    render();
  }
}

function clearPage() {
  S.seq.page += 1;
  S.seq.scan += 1;
  S.seq.scanReq += 1; // a scan still running for the old page can no longer apply
  Object.assign(S, { page: null, frame: null, image: null, template: null, dragBox: null, drag: null,
    scans: [], scanId: null, scan: null, serverPins: [], selected: null, scanning: false });
  setStatus(el.scanStatus, '');
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new ApiError(0, 'raster_failed', 'The page image could not be loaded.'));
    img.src = url;
  });
}

async function selectPage(i, restore = null) {
  if (!S.doc) return;
  clearPage();
  const token = S.seq.page;
  const doc = S.doc;
  S.page = i;
  setViewMessage(`Loading page ${i + 1}...`);
  render();
  try {
    const frame = await api.frame(doc.document_version, i);
    if (token !== S.seq.page) return;
    el.stubBanner.hidden = frame.render_service !== 'stub';
    const image = await loadImage(api.rasterUrl(doc.document_version, i));
    if (token !== S.seq.page) return;
    if (image.naturalWidth !== frame.width || image.naturalHeight !== frame.height) {
      throw new ApiError(0, 'frame_mismatch', `The page image is ${image.naturalWidth}x${image.naturalHeight} px `
        + `but its frame is ${frame.width}x${frame.height} px, so pins could not be placed correctly.`);
    }
    S.frame = frame;
    S.image = image;
    sizeCanvas();
    S.view = restore && restore.view ? restore.view : T.fit(S.view, frame, S.css.w, S.css.h);
    setViewMessage('');
    render();
    const { scans } = await api.pageScans(doc.document_version, i);
    if (token !== S.seq.page) return;
    S.scans = scans;
    const want = restore && restore.scanId && scans.find((s) => s.scan_id === restore.scanId);
    const pick = want ? want.scan_id : (scans.length ? scans[scans.length - 1].scan_id : null);
    if (pick) await loadScan(pick);
    else render();
  } catch (err) {
    if (token !== S.seq.page) return;
    setViewMessage(err.message, true);
    render();
  }
}

async function loadScan(scanId, { refresh = false } = {}) {
  const token = ++S.seq.scan;
  const pageToken = S.seq.page;
  if (!refresh) {
    S.scanId = scanId;
    S.serverPins = [];
    S.selected = null;
  }
  render();
  try {
    const st = await api.scanState(scanId);
    if (token !== S.seq.scan || pageToken !== S.seq.page || S.scanId !== scanId) return;
    if (!sameContext(st)) {
      setStatus(el.scanStatus, 'That scan belongs to a different page; it was not shown.', 'error');
      return;
    }
    S.scan = st;
    S.serverPins = refresh ? mergePins(S.serverPins, st.pins) : st.pins;
    render();
  } catch (err) {
    if (token !== S.seq.scan) return;
    setStatus(el.scanStatus, `Could not load the scan: ${err.message}`, 'error');
    render();
  }
}

// A scan result may be shown only on the page and frame it was made for.
function sameContext(st) {
  return S.doc && S.frame && st.document
    && st.document.document_version === S.doc.document_version
    && st.document.page_index === S.page
    && st.coordinate_frame.width === S.frame.width
    && st.coordinate_frame.height === S.frame.height;
}

async function upload(file) {
  if (!file) return;
  const docToken = S.seq.doc;
  setStatus(el.docStatus, `Uploading ${file.name}...`);
  try {
    const doc = await api.upload(file);
    await refreshDocuments();
    if (docToken !== S.seq.doc) {
      setStatus(el.docStatus, `Uploaded ${doc.filename}. Open it from the list.`, 'ok');
      return;
    }
    openDocument(doc);
    setStatus(el.docStatus, `Uploaded ${doc.filename}: ${doc.page_count} page(s). Choose a page.`, 'ok');
  } catch (err) {
    setStatus(el.docStatus, `Upload failed: ${err.message}`, 'error');
  } finally {
    el.file.value = '';
  }
}

// ---------------------------------------------------------------- scans
async function runScan() {
  if (el.scanBtn.disabled) return;
  const c = queue.counts(S.scanId);
  if (c.pending || c.failed) return;
  const reviewed = displayPins().filter((p) => p.state !== 'unreviewed').length;
  if (reviewed && !window.confirm(`Scanning again creates a new scan. The ${reviewed} reviewed pin(s) on the `
      + 'current scan stay saved and can be reopened from the Scan list.')) return;
  const mode = S.scanMode;
  const threshold = Number(el.threshold.value);
  if (needsTemplate(mode) && !(threshold >= -1 && threshold <= 1)) {
    setStatus(el.scanStatus, 'Threshold must be a number between -1 and 1.', 'error');
    return;
  }
  const ctx = { version: S.doc.document_version, page: S.page, pageToken: S.seq.page };
  const body = { document_version: ctx.version, page_index: ctx.page, mode };
  if (needsTemplate(mode)) Object.assign(body, { template_box: { ...S.template }, threshold });
  // Retrying the same request after a failure reuses its id, so a scan the
  // server finished but we never heard about is not run twice.
  const key = JSON.stringify(body);
  const reuse = S.lastScanRequest && S.lastScanRequest.key === key && S.lastScanRequest.failed;
  const requestId = reuse ? S.lastScanRequest.request_id : newRequestId();
  S.lastScanRequest = { key, request_id: requestId, failed: false };
  const token = ++S.seq.scanReq;
  S.scanning = true;
  setStatus(el.scanStatus, 'Scanning this page...');
  render();
  try {
    const st = await api.scan({ ...body, request_id: requestId });
    if (token !== S.seq.scanReq || ctx.pageToken !== S.seq.page || !sameContext(st)) {
      return; // stale: the reviewer moved on; the scan is saved in that page's list
    }
    S.seq.scan += 1; // any in-flight scan load is now stale
    if (!S.scans.some((s) => s.scan_id === st.scan_id)) {
      S.scans.push({ scan_id: st.scan_id, created_at: st.created_at, template: st.template,
        mode: st.mode, counts: st.counts });
    }
    S.scanId = st.scan_id;
    S.scan = st;
    S.serverPins = st.pins;
    S.selected = null;
    const n = st.pins.length;
    const sup = (st.suppressed || []).length;
    const none = mode === 'model' ? 'The point detector found nothing on this page. Add pins manually.'
      : mode === 'template+verifier' && sup
        ? `The verifier suppressed all ${sup} template match(es). Add pins manually, or try template mode.`
        : `No matches at threshold ${threshold.toFixed(2)}. Try a tighter box or a lower threshold, or add pins manually.`;
    setStatus(el.scanStatus, n ? `Found ${n} match(es).${sup ? ` ${sup} suppressed by the verifier.` : ''}`
      + `${st.truncated ? ' Result was capped; more may exist.' : ''}` : none, n ? 'ok' : '');
  } catch (err) {
    if (S.lastScanRequest && S.lastScanRequest.request_id === requestId) S.lastScanRequest.failed = true;
    if (err.code === 'model_not_active' || err.code === 'models_unavailable') loadModels();
    if (token !== S.seq.scanReq || ctx.pageToken !== S.seq.page) return;
    setStatus(el.scanStatus, `Scan failed: ${err.message}`, 'error');
  } finally {
    if (token === S.seq.scanReq) S.scanning = false;
    render();
  }
}

// -------------------------------------------------------------- review
function selectedPin() {
  return displayPins().find((p) => p.pin_id === S.selected) || null;
}

function review(kind) {
  const p = selectedPin();
  if (!p || !S.scanId) return;
  if (kind === 'approve') {
    if (p.origin !== 'machine' || p.state === 'approved') return;
    queue.enqueue({ scan_id: S.scanId, action: 'approve', pin_id: p.pin_id, request_id: newRequestId() });
  } else {
    if (p.temp) { // not on the server yet: drop the queued add instead
      const e = queue.find(p.request_id);
      if (e && e.status !== 'sending') {
        queue.discard(e.request_id);
        S.selected = null;
      }
      return;
    }
    if (p.state === 'rejected' || p.state === 'removed') return;
    queue.enqueue({ scan_id: S.scanId, action: 'delete_pin', pin_id: p.pin_id, request_id: newRequestId() });
  }
  render();
}

function addPin(pt) {
  if (!S.scanId) {
    setStatus(el.scanStatus, 'Scan the page first; manual pins belong to a scan.', 'error');
    render();
    return;
  }
  const p = T.clampPoint(pt, S.frame);
  const e = queue.enqueue({ scan_id: S.scanId, action: 'add_manual', x: round3(p.x), y: round3(p.y),
    request_id: newRequestId() });
  S.selected = 'tmp:' + e.request_id;
  render();
}

function round3(v) {
  return Math.round(v * 1000) / 1000;
}

function selectNext() {
  const pins = sortedPins(visiblePins());
  const start = pins.findIndex((p) => p.pin_id === S.selected);
  for (let k = 1; k <= pins.length; k++) {
    const p = pins[(start + k + pins.length) % pins.length];
    if (p.state === 'unreviewed') {
      selectPin(p.pin_id, true);
      return;
    }
  }
}

function selectPin(id, centre = false) {
  S.selected = id;
  const p = selectedPin();
  if (p && centre) {
    const s = T.toScreen(S.view, p.x, p.y);
    if (s.x < 40 || s.y < 40 || s.x > S.css.w - 40 || s.y > S.css.h - 40) {
      S.view = T.centreOn(S.view, p.x, p.y, S.css.w, S.css.h);
    }
  }
  render();
}

// --------------------------------------------------------- interaction
function localPoint(e) {
  const r = el.canvas.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function setMode(mode) {
  if (mode === 'template' && !needsTemplate()) {
    setStatus(el.scanStatus, 'The point detector needs no template box.', '');
    mode = 'pan';
  }
  S.mode = mode;
  S.drag = null;
  S.dragBox = null;
  render();
}

function setView(v) {
  S.view = S.frame ? T.clampPan(v, S.frame, S.css.w, S.css.h) : v;
  render();
}

el.viewport.addEventListener('pointerdown', (e) => {
  el.viewport.focus({ preventScroll: true });
  if (!S.frame || e.button === 2) return;
  const s = localPoint(e);
  const pan = e.button === 1 || S.spaceDown || (S.mode === 'pan' && e.button === 0);
  if (!pan && e.button !== 0) return;
  e.preventDefault();
  el.viewport.setPointerCapture(e.pointerId);
  S.drag = { kind: pan ? 'pan' : S.mode, pointerId: e.pointerId, start: s, last: s,
    startC: T.toCanonical(S.view, s.x, s.y), moved: false, button: e.button };
  render();
});

el.viewport.addEventListener('pointermove', (e) => {
  if (!S.frame) return;
  const s = localPoint(e);
  const c = T.toCanonical(S.view, s.x, s.y);
  el.cursorPos.textContent = T.insidePage(c, S.frame)
    ? `x ${c.x.toFixed(1)}  y ${c.y.toFixed(1)} px` : 'outside page';
  const d = S.drag;
  if (!d || d.pointerId !== e.pointerId) return;
  if (Math.hypot(s.x - d.start.x, s.y - d.start.y) > CLICK_SLOP) d.moved = true;
  if (d.kind === 'pan') {
    setView(T.panBy(S.view, s.x - d.last.x, s.y - d.last.y));
  } else if (d.kind === 'template' && d.moved) {
    S.dragBox = T.boxFromDrag(d.startC, c, S.frame);
    render();
  }
  d.last = s;
});

function endDrag(e, cancelled) {
  const d = S.drag;
  if (!d || d.pointerId !== e.pointerId) return;
  S.drag = null;
  const s = localPoint(e);
  const c = T.toCanonical(S.view, s.x, s.y);
  if (!cancelled) {
    if (d.kind === 'pan' && !d.moved && S.mode === 'pan' && d.button === 0) {
      const pins = visiblePins();
      const i = T.hitTest(S.view, pins, s.x, s.y);
      selectPin(i >= 0 ? pins[i].pin_id : null);
    } else if (d.kind === 'template' && d.moved) {
      const box = T.boxFromDrag(d.startC, c, S.frame);
      if (box && (box.width < MIN_TEMPLATE_SIDE || box.height < MIN_TEMPLATE_SIDE)) {
        setStatus(el.scanStatus, `The box is ${box.width} x ${box.height} px; it must be at least `
          + `${MIN_TEMPLATE_SIDE} x ${MIN_TEMPLATE_SIDE} px. Zoom in and drag again.`, 'error');
      } else if (box) {
        S.template = box;
        setStatus(el.scanStatus, '');
      }
    } else if (d.kind === 'add' && !d.moved) {
      if (T.insidePage(c, S.frame)) addPin(c);
      else setStatus(el.scanStatus, 'Click inside the page to add a pin.', 'error');
    }
  }
  S.dragBox = null;
  render();
}

el.viewport.addEventListener('pointerup', (e) => endDrag(e, false));
el.viewport.addEventListener('pointercancel', (e) => endDrag(e, true));
el.viewport.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); });
el.viewport.addEventListener('contextmenu', (e) => e.preventDefault());
el.viewport.addEventListener('pointerleave', () => { el.cursorPos.textContent = ''; });

el.viewport.addEventListener('wheel', (e) => {
  if (!S.frame) return;
  e.preventDefault();
  const s = localPoint(e);
  const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
  setView(T.zoomAt(S.view, Math.exp(-dy * 0.0015), s.x, s.y));
}, { passive: false });

function zoomCentre(f) {
  if (S.frame) setView(T.zoomAt(S.view, f, S.css.w / 2, S.css.h / 2));
}

el.zoomIn.onclick = () => zoomCentre(1.25);
el.zoomOut.onclick = () => zoomCentre(0.8);
el.fitBtn.onclick = () => S.frame && setView(T.fit(S.view, S.frame, S.css.w, S.css.h));
el.rotateBtn.onclick = () => S.frame && setView(T.rotateAt(S.view, S.css.w / 2, S.css.h / 2));
el.hidePins.onchange = render;
el.showHidden.onchange = render;
for (const r of document.querySelectorAll('input[name=mode]')) r.onchange = () => setMode(r.value);
el.approveBtn.onclick = () => review('approve');
el.rejectBtn.onclick = () => review('delete');
el.nextBtn.onclick = selectNext;
el.scanBtn.onclick = runScan;
el.scanMode.onchange = () => setScanMode(el.scanMode.value);
el.scanMode.onfocus = () => loadModels();
el.file.onchange = () => upload(el.file.files[0]);
el.docSelect.onchange = () => {
  const d = S.docs.find((x) => x.document_version === el.docSelect.value);
  if (d) openDocument(d);
};
el.scanSelect.onchange = () => {
  if (el.scanSelect.value) loadScan(el.scanSelect.value);
};
el.pinTable.addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-pin]');
  if (tr) selectPin(tr.dataset.pin, true);
});

document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, select, textarea') && e.target.type !== 'radio'
      && e.target.type !== 'checkbox') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === ' ') { S.spaceDown = true; e.preventDefault(); return; }
  const actions = {
    v: () => setMode('pan'), t: () => setMode('template'), p: () => setMode('add'),
    '+': () => zoomCentre(1.25), '=': () => zoomCentre(1.25), '-': () => zoomCentre(0.8),
    0: () => el.fitBtn.onclick(), r: () => el.rotateBtn.onclick(),
    h: () => { el.hidePins.checked = !el.hidePins.checked; render(); },
    a: () => review('approve'), x: () => review('delete'), Delete: () => review('delete'),
    Backspace: () => review('delete'), n: selectNext,
    Escape: () => { if (S.drag) { S.drag = null; S.dragBox = null; render(); } else if (S.selected) selectPin(null); else setMode('pan'); },
  };
  const fn = actions[k] || actions[k.toLowerCase()];
  if (fn) {
    e.preventDefault();
    fn();
  }
});
document.addEventListener('keyup', (e) => { if (e.key === ' ') S.spaceDown = false; });
window.addEventListener('blur', () => { S.spaceDown = false; });

window.addEventListener('beforeunload', (e) => {
  if (queue.counts().pending) {
    e.preventDefault();
    e.returnValue = '';
  }
});

new ResizeObserver(() => {
  sizeCanvas();
  render();
}).observe(el.viewport);

function watchDpr() {
  const mq = matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
  mq.addEventListener('change', () => {
    sizeCanvas();
    render();
    watchDpr();
  }, { once: true });
}
watchDpr();

// Read-only hooks for the browser tests.
window.__pinny = {
  view: () => ({ ...S.view }),
  frame: () => S.frame && { ...S.frame },
  pins: () => displayPins(),
  scanId: () => S.scanId,
  page: () => S.page,
  template: () => S.template && { ...S.template },
  scanMode: () => S.scanMode,
  models: () => S.models,
  queue: () => queue.entries.map((e) => ({ ...e })),
  idle: () => !!S.frame && !rafPending && !S.scanning && queue.counts().pending === 0,
  toScreen: (x, y) => T.toScreen(S.view, x, y),
  lookAt: (x, y, zoom, rotation) => setView(T.centreOn({ ...S.view, zoom: T.clampZoom(zoom),
    rotation: T.normRotation(rotation) }, x, y, S.css.w, S.css.h)),
};

// ---------------------------------------------------------- scan modes
async function loadModels() {
  try {
    S.models = await api.models();
  } catch (err) {
    S.models = { modes: [{ mode: 'template', available: true }], active: {} };
  }
  const m = modeInfo(S.scanMode);
  if (S.scanMode !== 'template' && !(m && m.available)) S.scanMode = 'template';
  render();
}

function setScanMode(mode) {
  if (!MODE_NAMES[mode]) mode = 'template';
  S.scanMode = mode;
  try { localStorage.setItem(SCAN_MODE_KEY, mode); } catch (err) { /* per-viewer convenience only */ }
  if (!needsTemplate() && S.mode === 'template') {
    S.mode = 'pan';
    S.drag = null;
    S.dragBox = null;
  }
  setStatus(el.scanStatus, '');
  render();
}

// --------------------------------------------------------------- start
async function start() {
  try { S.scanMode = MODE_NAMES[localStorage.getItem(SCAN_MODE_KEY)] ? localStorage.getItem(SCAN_MODE_KEY) : 'template'; } catch (err) { /* ignore */ }
  sizeCanvas();
  render();
  loadModels();
  try {
    const h = await api.health();
    el.stubBanner.hidden = h.render_service !== 'stub';
  } catch (err) {
    setViewMessage(err.message, true);
  }
  await refreshDocuments();
  const restore = readHash();
  if (restore.version) {
    const d = S.docs.find((x) => x.document_version === restore.version);
    if (d) openDocument(d, restore);
    else setStatus(el.docStatus, 'The document in the link is not on this server. Upload it again.', 'error');
  }
  queue.pump(); // resend anything left from before a reload
}

start();
