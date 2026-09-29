// Legend workflow in the viewer (docs/viewer.md "Legend and whole set"):
// find the drawing's legend, check it, scan every sheet with it, review
// sheet by sheet with each pin's tag, and count.
//
// app.js calls init(host) once and the hooks below; everything else here
// talks to the API directly. Server text is set with textContent only.

import { request, newRequestId } from './api.js';
import * as C from './legend-core.js';

const enc = encodeURIComponent;
const docPath = (v) => `/api/documents/${enc(v)}`;
const legendApi = {
  state: (v) => request('GET', `${docPath(v)}/legend`),
  read: (v, pageIndex) => request('POST', `${docPath(v)}/legend/read`,
    { json: pageIndex == null ? {} : { page_index: pageIndex } }),
  edit: (v, body) => request('POST', `${docPath(v)}/legend/edit`, { json: body }),
  confirm: (v, version) => request('POST', `${docPath(v)}/legend/confirm`, { json: { expected_version: version } }),
  saveStandard: (v, engineer) => request('POST', `${docPath(v)}/legend/save-standard`, { json: { engineer } }),
  library: () => request('GET', '/api/legend-library'),
  runs: (v) => request('GET', `${docPath(v)}/set-scans`),
  startScan: (v, requestId) => request('POST', `${docPath(v)}/set-scans`, { json: { request_id: requestId } }),
  counts: (v) => request('GET', `${docPath(v)}/counts`),
  csvUrl: (v) => `${docPath(v)}/counts.csv`,
  rasterUrl: (v, p) => `${docPath(v)}/pages/${p}/raster.png`,
};

const POLL_MS = 1500;
const NEW_TAG_KEY = 'pinny.newPinTag';
const $ = (id) => document.getElementById(id);

let host = null;
let el = {};
const L = {
  doc: null,
  legend: null, // the legend view from the server, or null
  run: null, // latest whole-set scan
  counts: null,
  selected: null, // entry id
  busy: false,
  adding: false,
  addAnyway: null, // box waiting for "Add anyway"
  image: null, // legend page raster, for the symbol pictures
  imageKey: '',
  tagFilter: '',
  lastTag: '',
  seq: 0,
  lastScanRequest: null,
};
try { L.lastTag = localStorage.getItem(NEW_TAG_KEY) || ''; } catch (e) { /* per-viewer convenience only */ }

// --------------------------------------------------------------- setup
export function init(h) {
  host = h;
  el = {
    section: $('legend-section'), details: $('legend-details'), findBtn: $('legend-find-btn'), pageBtn: $('legend-page-btn'),
    status: $('legend-status'), body: $('legend-body'), summary: $('legend-summary'), table: $('legend-table'),
    editor: $('legend-editor'), notes: $('legend-notes'), name: $('le-name'), tag: $('le-tag'),
    group: $('le-group'), save: $('le-save'), confirmEntry: $('le-confirm'), split: $('le-split'),
    del: $('le-delete'), mergeInto: $('le-merge-into'), merge: $('le-merge'), addBtn: $('legend-add-btn'),
    addAnyway: $('legend-add-anyway-btn'), problems: $('legend-problems'), confirmBtn: $('legend-confirm-btn'),
    engineer: $('legend-engineer'), engineers: $('legend-engineers'), compareBtn: $('legend-compare-btn'),
    saveStdBtn: $('legend-save-std-btn'), compare: $('legend-compare'),
    scanBody: $('setscan-body'), scanBtn: $('setscan-btn'), progress: $('setscan-progress'),
    scanStatus: $('setscan-status'), sheets: $('sheets-table'), nextSheet: $('next-sheet-btn'),
    countsSection: $('counts-section'), countsScope: $('counts-scope'), countsTable: $('counts-table'),
    csv: $('counts-csv'), tagTools: $('tag-tools'), newPinTag: $('new-pin-tag'), tagFilter: $('tag-filter'),
    tagKey: $('tag-key'), approveAs: $('approve-as-btn'),
  };
  el.approveAs.onclick = () => host.approveAs();
  el.findBtn.onclick = () => readLegend(null);
  el.pageBtn.onclick = () => readLegend(host.state().page);
  el.table.addEventListener('click', onTableClick);
  el.save.onclick = saveEntry;
  el.confirmEntry.onclick = () => entryEdit({ op: 'confirm' }, 'Marked as looking right.');
  el.split.onclick = () => entryEdit({ op: 'split' }, 'Split into one symbol per drawing.');
  el.del.onclick = () => {
    const e = selectedEntry();
    if (e && window.confirm(`Delete ${e.name} (${e.tag}) from the legend? Pinny will not count it.`)) {
      entryEdit({ op: 'delete' }, 'Deleted.');
    }
  };
  el.merge.onclick = () => {
    if (!el.mergeInto.value) return;
    entryEdit({ op: 'merge', into: el.mergeInto.value }, 'Merged: both drawings now count as one symbol.');
  };
  el.addBtn.onclick = startAdding;
  el.addAnyway.onclick = () => { if (L.addAnyway) addSymbol(L.addAnyway, true); };
  el.confirmBtn.onclick = confirmLegend;
  el.compareBtn.onclick = () => legendEdit({ op: 'set_engineer', engineer: el.engineer.value.trim() || null },
    el.engineer.value.trim() ? 'Compared with the saved legend.' : 'No engineer chosen.');
  el.saveStdBtn.onclick = saveStandard;
  el.scanBtn.onclick = startScan;
  el.sheets.addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-page]');
    if (tr) openSheet(Number(tr.dataset.page));
  });
  el.nextSheet.onclick = () => {
    const s = C.nextSheet(L.counts && L.counts.sheets, host.state().page);
    if (s) openSheet(s.page_index);
    else setStatus(el.scanStatus, 'Every sheet is marked fully reviewed.', 'ok');
  };
  el.countsScope.onchange = renderCounts;
  el.newPinTag.onchange = () => {
    L.lastTag = el.newPinTag.value;
    try { localStorage.setItem(NEW_TAG_KEY, L.lastTag); } catch (e) { /* ignore */ }
    host.render();
  };
  el.tagFilter.onchange = () => { L.tagFilter = el.tagFilter.value; host.render(); };
  document.addEventListener('pinny:page-reviewed', () => refreshCounts());
}

// ------------------------------------------------------------ hooks for app.js
export function openDocument(doc) {
  L.seq += 1;
  Object.assign(L, { doc, legend: null, run: null, counts: null, selected: null, adding: false,
    addAnyway: null, tagFilter: '', image: null, imageKey: '' });
  setStatus(el.status, '');
  setStatus(el.scanStatus, '');
  clearTimeout(pollTimer);
  if (doc) load();
  renderAll();
}

// Is this pin shown under the tag filter?
export function pinShown(p) {
  return !L.tagFilter || !isLegendScan() || p.class_label === L.tagFilter;
}

// The tag for a pin added by hand now ('' for none).
export function tagForNewPin() {
  return isLegendScan() && L.legend ? el.newPinTag.value || '' : '';
}

export function rememberTag(tag) {
  if (!tag) return;
  L.lastTag = tag;
  try { localStorage.setItem(NEW_TAG_KEY, tag); } catch (e) { /* ignore */ }
}

// A box drawn in "add a missing symbol" mode (canonical px on this page).
export function boxDrawn(box) {
  L.adding = false;
  const st = host.state();
  if (!L.legend || st.page !== L.legend.page_index) {
    setStatus(el.status, 'Draw the box on the legend page.', 'error');
    return;
  }
  addSymbol(box, false);
}

export function onSaved() {
  refreshCountsSoon();
}

export function tagColor(tag) {
  return C.PALETTE[C.tagColorIndex(tags(), tag)];
}

export function tagChipHtml(tag) {
  if (!tag) return '';
  return `<span class="tag-chip tag-c${C.tagColorIndex(tags(), tag)}">${escapeHtml(tag)}</span>`;
}

// A small filled label with the tag, next to a pin (canvas, CSS px).
export function drawTag(ctx, tag, sx, sy) {
  const c = tagColor(tag);
  ctx.font = 'bold 10px system-ui, sans-serif';
  const w = ctx.measureText(tag).width;
  ctx.fillStyle = c.bg;
  ctx.fillRect(sx + 9, sy - 4, w + 6, 13);
  ctx.fillStyle = c.fg;
  ctx.fillText(tag, sx + 12, sy + 6);
}

// On the legend page: every symbol's box, the selected one bold.
export function drawOverlay(ctx, corners, polygon) {
  const st = host.state();
  if (!L.legend || st.page !== L.legend.page_index || !st.frame) return;
  for (const e of L.legend.entries) {
    const sel = e.id === L.selected;
    const color = e.status === 'check' ? '#b00020' : tagColor(e.tag).bg;
    for (const b of e.symbol_boxes) {
      polygon(ctx, corners({ x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1] }), color,
        sel ? 3 : 1, e.status === 'check' && !sel ? [4, 3] : []);
    }
  }
}

export function renderPanel() {
  const st = host.state();
  const legendScan = isLegendScan();
  el.tagTools.hidden = !(legendScan && L.legend);
  if (!el.tagTools.hidden) renderTagTools(st);
  el.pageBtn.hidden = !(st.page != null && st.frame);
  el.pageBtn.textContent = `Read legend on page ${(st.page ?? 0) + 1}`;
  if (L.doc) renderRun();
}

export function debugState() {
  return JSON.parse(JSON.stringify({ legend: L.legend, run: L.run, counts: L.counts, selected: L.selected,
    adding: L.adding, tagFilter: L.tagFilter, busy: L.busy }));
}

// ----------------------------------------------------------------- loading
function tags() {
  return L.legend ? L.legend.entries.map((e) => e.tag) : [];
}

function isLegendScan() {
  const sc = host.state().scan;
  return !!(sc && sc.mode === 'legend');
}

function selectedEntry() {
  return L.legend && L.legend.entries.find((e) => e.id === L.selected);
}

async function load() {
  const token = L.seq;
  const v = L.doc.document_version;
  try {
    const st = await legendApi.state(v);
    if (token !== L.seq) return;
    apply(st);
    if (st.set_scan && st.set_scan.status === 'done') refreshCounts();
  } catch (err) {
    if (token === L.seq) setStatus(el.status, `Could not load the legend: ${err.message}`, 'error');
  }
  renderAll();
}

function apply(st) {
  // Once confirmed, the symbol table folds away to leave room for the sheets.
  const was = L.legend;
  if (st.legend && st.legend.confirmed && !(was && was.confirmed)) el.details.open = false;
  if (st.legend && !st.legend.confirmed && (!was || was.confirmed)) el.details.open = true;
  L.legend = st.legend;
  if (L.selected && !selectedEntry()) L.selected = null;
  showRun(st.set_scan);
}

let pollTimer = null;
function showRun(run) {
  const was = L.run;
  L.run = run;
  clearTimeout(pollTimer);
  if (C.isActiveRun(run)) pollTimer = setTimeout(pollRun, POLL_MS);
  if (run && run.status === 'done' && (!was || was.status !== 'done' || was.run_id !== run.run_id)) {
    refreshCounts();
    host.refreshPageScans();
  }
}

async function pollRun() {
  if (!L.doc) return;
  const token = L.seq;
  try {
    const { set_scans: runs } = await legendApi.runs(L.doc.document_version);
    if (token !== L.seq) return;
    showRun(runs[runs.length - 1] || null);
  } catch (err) {
    if (token !== L.seq) return;
    setStatus(el.scanStatus, `Lost touch with the scan (${err.message}); retrying...`, 'error');
    pollTimer = setTimeout(pollRun, POLL_MS * 2);
  }
  renderAll();
}

let countsTimer = null;
function refreshCountsSoon() {
  clearTimeout(countsTimer);
  countsTimer = setTimeout(refreshCounts, 500);
}

async function refreshCounts() {
  if (!L.doc) return;
  const token = L.seq;
  try {
    const c = await legendApi.counts(L.doc.document_version);
    if (token !== L.seq) return;
    L.counts = c;
  } catch (err) {
    if (token === L.seq) setStatus(el.scanStatus, `Could not load the counts: ${err.message}`, 'error');
  }
  renderAll();
}

// ----------------------------------------------------------------- actions
async function busy(fn) {
  if (L.busy) return false;
  L.busy = true;
  renderAll();
  try {
    await fn();
  } finally {
    L.busy = false;
    renderAll();
  }
  return true;
}

function readLegend(pageIndex) {
  if (!L.doc) return;
  if (L.legend && !window.confirm('Read the legend again? Changes made to the current legend are replaced.')) return;
  const token = L.seq;
  return busy(async () => {
    setStatus(el.status, pageIndex == null ? 'Looking for the legend...' : `Reading the legend on page ${pageIndex + 1}...`);
    try {
      const st = await legendApi.read(L.doc.document_version, pageIndex);
      if (token !== L.seq) return;
      apply(st);
      L.selected = null;
      const n = st.legend.entries.length;
      setStatus(el.status, `Read ${n} symbol(s). Check each row, then press Confirm legend.`, 'ok');
      goToLegendPage();
    } catch (err) {
      if (token === L.seq) setStatus(el.status, err.message, 'error');
    }
  });
}

function goToLegendPage(entry = null) {
  const st = host.state();
  const p = L.legend.page_index;
  const box = entry && entry.symbol_boxes.length ? union(entry.symbol_boxes) : null;
  const centre = () => {
    if (box) host.centreOn((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 1);
  };
  if (st.page !== p) host.selectPage(p).then(centre);
  else centre();
}

// One legend edit. Resolves to null when it was saved, else the error.
async function legendEdit(body, done) {
  if (!L.legend) return { code: 'no_legend' };
  const token = L.seq;
  let error = null;
  const ran = await busy(async () => {
    try {
      const st = await legendApi.edit(L.doc.document_version, { ...body, expected_version: L.legend.version });
      if (token !== L.seq) return;
      apply(st);
      setStatus(el.status, done, 'ok');
    } catch (err) {
      error = err;
      if (token !== L.seq) return;
      setStatus(el.status, err.message, 'error');
      if (err.code === 'stale_legend') load();
    }
  });
  return ran ? error : { code: 'busy' };
}

function entryEdit(body, done) {
  const e = selectedEntry();
  if (!e) return Promise.resolve({ code: 'no_entry' });
  return legendEdit({ ...body, entry_id: e.id }, done);
}

async function saveEntry() {
  const e = selectedEntry();
  if (!e) return;
  const edits = [];
  if (el.name.value.trim() !== e.name) edits.push({ op: 'rename', name: el.name.value });
  if (el.tag.value.trim().toUpperCase() !== e.tag) edits.push({ op: 'set_tag', tag: el.tag.value });
  if (el.group.value.trim() !== e.group) edits.push({ op: 'set_group', group: el.group.value });
  if (!edits.length) {
    setStatus(el.status, 'Nothing changed.');
    return;
  }
  for (const body of edits) {
    if (await entryEdit(body, 'Saved.')) return; // refused; the message says why
  }
}

function startAdding() {
  if (!L.legend) return;
  L.adding = true;
  L.addAnyway = null;
  el.details.open = true;
  setStatus(el.status, 'Drag a box around the missing symbol on the legend page.');
  goToLegendPage();
  host.setMode('legendbox');
  renderAll();
}

async function addSymbol(box, force) {
  const b = { x: box.x, y: box.y, width: box.width, height: box.height };
  L.addAnyway = null;
  const err = await legendEdit({ op: 'add', box: b, force }, 'Added. Check its name and tag.');
  if (err && err.code === 'row_already_read') L.addAnyway = b;
  if (!err && L.legend) {
    const last = L.legend.entries[L.legend.entries.length - 1];
    if (last) {
      L.selected = last.id;
      el.name.value = last.name;
      el.tag.value = last.tag;
      el.group.value = last.group;
    }
  }
  renderAll();
}

function confirmLegend() {
  if (!L.legend) return;
  const token = L.seq;
  return busy(async () => {
    try {
      const st = await legendApi.confirm(L.doc.document_version, L.legend.version);
      if (token !== L.seq) return;
      apply(st);
      setStatus(el.status, 'Legend confirmed. Next: Scan whole set.', 'ok');
    } catch (err) {
      if (token !== L.seq) return;
      setStatus(el.status, err.message, 'error');
      if (err.code === 'stale_legend') load();
    }
  });
}

async function saveStandard() {
  const name = el.engineer.value.trim();
  if (!name) {
    setStatus(el.status, 'Enter the engineer\'s name first.', 'error');
    return;
  }
  const token = L.seq;
  return busy(async () => {
    try {
      const st = await legendApi.saveStandard(L.doc.document_version, name);
      if (token !== L.seq) return;
      apply(st);
      setStatus(el.status, `Saved as ${name}'s standard legend. Their next drawing set is compared with it.`, 'ok');
      loadLibrary();
    } catch (err) {
      if (token === L.seq) setStatus(el.status, err.message, 'error');
    }
  });
}

let libraryLoaded = false;
async function loadLibrary() {
  try {
    const { engineers } = await legendApi.library();
    el.engineers.replaceChildren(...engineers.map((x) => {
      const o = document.createElement('option');
      o.value = x.engineer;
      return o;
    }));
    libraryLoaded = true;
  } catch (err) { /* the list is only a convenience */ }
}

function startScan() {
  if (!L.legend || !L.legend.confirmed || C.isActiveRun(L.run)) return;
  const n = L.doc.page_count - 1;
  const reviewed = L.counts && L.counts.totals.all
    ? L.counts.totals.all.approved + L.counts.totals.all.rejected + L.counts.totals.all.added : 0;
  let q = `Scan all ${n} sheet(s) with this legend? It runs on the server; you can keep working.`;
  if (reviewed) q += ` The ${reviewed} review(s) of the last whole-set scan stay saved, but each sheet gets new pins to review.`;
  if (!window.confirm(q)) return;
  // A retry after a failed request reuses its id, so a scan the server
  // started but we never heard about is not started twice.
  const key = `${L.doc.document_version}#${L.legend.version}`;
  const reuse = L.lastScanRequest && L.lastScanRequest.key === key && L.lastScanRequest.failed;
  const requestId = reuse ? L.lastScanRequest.request_id : newRequestId();
  L.lastScanRequest = { key, request_id: requestId, failed: false };
  const token = L.seq;
  return busy(async () => {
    setStatus(el.scanStatus, 'Starting the whole-set scan...');
    try {
      const run = await legendApi.startScan(L.doc.document_version, requestId);
      if (token !== L.seq) return;
      showRun(run);
      setStatus(el.scanStatus, '');
    } catch (err) {
      L.lastScanRequest.failed = true;
      if (token === L.seq) setStatus(el.scanStatus, err.message, 'error');
    }
  });
}

function openSheet(pageIndex) {
  const s = L.counts && L.counts.sheets.find((x) => x.page_index === pageIndex);
  host.selectPage(pageIndex, s && s.scan_id ? { scanId: s.scan_id } : null);
  // Reviewing is the next step: bring it into view.
  const review = document.getElementById('review-section');
  if (review && review.scrollIntoView) review.scrollIntoView({ block: 'start' });
}

// Once a drawing has a whole-set scan, the manual template scan and batch
// sections sit between the sheet list and Review; fold them away (once per
// drawing, so a person who opens them again keeps them open).
let fold = { doc: null, folded: false };
function foldManualScan(hasSetScan) {
  const d = document.getElementById('manual-scan');
  const v = L.doc && L.doc.document_version;
  if (!d || !v) return;
  if (fold.doc !== v) { // another drawing: start from its own state
    fold = { doc: v, folded: hasSetScan };
    d.open = !hasSetScan;
  } else if (hasSetScan && !fold.folded) { // its first whole-set scan just finished
    fold.folded = true;
    d.open = false;
  }
}

function onTableClick(e) {
  const box = e.target.closest('input[type=checkbox][data-count]');
  if (box) {
    const id = box.dataset.count;
    legendEdit({ op: 'set_count', entry_id: id, count: box.checked },
      box.checked ? 'Will be counted.' : 'Will not be counted.');
    return;
  }
  const tr = e.target.closest('tr[data-entry]');
  if (!tr) return;
  L.selected = tr.dataset.entry;
  const entry = selectedEntry();
  if (entry) {
    el.name.value = entry.name;
    el.tag.value = entry.tag;
    el.group.value = entry.group;
    goToLegendPage(entry);
  }
  renderAll();
  host.render();
}

// --------------------------------------------------------------- rendering
function renderAll() {
  if (!host) return;
  const hasDoc = !!L.doc;
  el.section.hidden = !hasDoc;
  el.findBtn.disabled = !hasDoc || L.busy;
  el.pageBtn.disabled = !hasDoc || L.busy;
  el.findBtn.textContent = L.legend ? 'Find legend again' : 'Find legend';
  el.body.hidden = !L.legend;
  el.scanBody.hidden = !(L.legend && (L.legend.confirmed || L.run));
  if (L.legend) {
    renderLegend();
    if (!libraryLoaded) loadLibrary();
  }
  renderRun();
  renderCounts();
  host.render();
}

function union(boxes) {
  return boxes.reduce((a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]),
    Math.max(a[3], b[3])], [Infinity, Infinity, -Infinity, -Infinity]);
}

function ensureImage() {
  const key = `${L.doc.document_version}#${L.legend.page_index}`;
  if (L.imageKey === key) return;
  L.imageKey = key;
  L.image = null;
  const img = new Image();
  img.onload = () => {
    if (L.imageKey !== key) return;
    L.image = img;
    el.table.tBodies[0].dataset.key = '';
    renderAll();
  };
  img.src = legendApi.rasterUrl(L.doc.document_version, L.legend.page_index);
}

// A picture of the symbol, cut from the legend page's raster.
function symbolCanvas(entry) {
  const cv = document.createElement('canvas');
  cv.className = 'legend-symbol';
  const dpr = window.devicePixelRatio || 1;
  const W = 56;
  const H = 36;
  cv.width = Math.round(W * dpr);
  cv.height = Math.round(H * dpr);
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (!L.image || !entry.symbol_boxes.length) return cv;
  const b = union(entry.symbol_boxes);
  const pad = 6;
  const sx = Math.max(0, b[0] - pad);
  const sy = Math.max(0, b[1] - pad);
  const sw = Math.min(L.image.naturalWidth, b[2] + pad) - sx;
  const sh = Math.min(L.image.naturalHeight, b[3] + pad) - sy;
  if (sw <= 0 || sh <= 0) return cv;
  const k = Math.min(cv.width / sw, cv.height / sh);
  const dw = sw * k;
  const dh = sh * k;
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(L.image, sx, sy, sw, sh, (cv.width - dw) / 2, (cv.height - dh) / 2, dw, dh);
  return cv;
}

function renderLegend() {
  const lg = L.legend;
  ensureImage();
  el.summary.textContent = C.legendSummary(lg);
  const key = `${lg.version}|${L.selected}|${!!L.image}|${L.busy}`;
  const tbody = el.table.tBodies[0];
  if (tbody.dataset.key !== key) {
    tbody.dataset.key = key;
    tbody.replaceChildren(...lg.entries.map((e) => {
      const tr = document.createElement('tr');
      tr.dataset.entry = e.id;
      tr.className = (e.id === L.selected ? 'selected ' : '') + (e.status === 'check' ? 'needs-look' : '');
      const sym = document.createElement('td');
      sym.append(symbolCanvas(e));
      const tag = document.createElement('td');
      const chip = document.createElement('span');
      chip.className = `tag-chip tag-c${C.tagColorIndex(tags(), e.tag)}`;
      chip.textContent = e.tag;
      tag.append(chip);
      if (e.status === 'check') {
        const warn = document.createElement('span');
        warn.className = 'legend-flag';
        warn.textContent = ' !';
        warn.title = 'Needs a look';
        tag.append(warn);
      }
      const name = document.createElement('td');
      const nm = document.createElement('div');
      nm.textContent = e.name;
      const grp = document.createElement('div');
      grp.className = 'muted legend-small';
      grp.textContent = e.group + (e.source === 'user' ? ' · added by hand' : '');
      name.append(nm, grp);
      for (const n of e.notes) {
        const d = document.createElement('div');
        d.className = 'legend-small ' + (n.blocking ? 'error' : 'legend-note');
        d.textContent = n.text;
        name.append(d);
      }
      const cnt = document.createElement('td');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = e.count;
      cb.dataset.count = e.id;
      cb.disabled = L.busy;
      cb.setAttribute('aria-label', `Count ${e.name}`);
      if (!e.count && e.not_counted_reason) cb.title = e.not_counted_reason;
      cnt.append(cb);
      tr.append(sym, tag, name, cnt);
      return tr;
    }));
  }
  // Editor for the selected symbol.
  const e = selectedEntry();
  el.editor.hidden = !e;
  if (e) {
    el.notes.textContent = e.description ? `Legend text: ${e.description}` : '';
    el.confirmEntry.hidden = e.status !== 'check';
    el.split.hidden = e.symbol_boxes.length < 2;
    const others = lg.entries.filter((x) => x.id !== e.id);
    const html = others.map((x) => `${x.id}|${x.tag}|${x.name}`).join('\n');
    if (el.mergeInto.dataset.html !== html) {
      el.mergeInto.dataset.html = html;
      el.mergeInto.replaceChildren(...[['', '- choose -'], ...others.map((x) => [x.id, `${x.tag} ${x.name}`])].map(([v, t]) => {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = t;
        return o;
      }));
    }
    for (const b of [el.save, el.confirmEntry, el.split, el.del, el.merge]) b.disabled = L.busy;
  }
  el.addBtn.disabled = L.busy;
  el.addBtn.textContent = L.adding ? 'Drag a box on the legend page...' : 'Add a missing symbol';
  el.addAnyway.hidden = !L.addAnyway;
  el.problems.textContent = lg.problems.join(' ') + (lg.warnings.length ? ` ${lg.warnings.join(' ')}` : '');
  el.problems.className = 'status' + (lg.problems.length ? ' error' : '');
  el.confirmBtn.disabled = L.busy || !lg.ready_to_confirm || lg.confirmed;
  el.confirmBtn.textContent = lg.confirmed ? 'Legend confirmed' : 'Confirm legend';
  if (document.activeElement !== el.engineer && lg.engineer && !el.engineer.value) el.engineer.value = lg.engineer;
  el.saveStdBtn.disabled = L.busy || !lg.confirmed;
  el.compare.textContent = C.comparisonText(lg.comparison);
}

function renderRun() {
  const run = L.run;
  const active = C.isActiveRun(run);
  el.scanBtn.disabled = L.busy || active || !(L.legend && L.legend.confirmed);
  el.scanBtn.title = L.legend && !L.legend.confirmed ? 'Confirm the legend first.' : '';
  el.scanBtn.textContent = run && run.status === 'done' ? 'Scan whole set again' : 'Scan whole set';
  el.progress.hidden = !active;
  if (active) {
    el.progress.max = Math.max(1, run.sheets_total);
    el.progress.value = run.sheets_done;
  }
  if (!el.scanStatus.textContent || active || (run && el.scanStatus.dataset.run !== `${run.run_id}:${run.status}`)) {
    if (run) {
      el.scanStatus.dataset.run = `${run.run_id}:${run.status}`;
      setStatus(el.scanStatus, C.runText(run), run.status === 'failed' ? 'error' : run.status === 'done' ? 'ok' : '');
    }
  }
  const sheets = L.counts && L.counts.run_id ? L.counts.sheets : [];
  const cur = host.state().page;
  const rows = sheets.map((s) => `<tr data-page="${s.page_index}" class="${s.page_index === cur ? 'current' : ''}">`
    + `<td>${s.page_index + 1}</td><td>${s.all.found}</td><td>${s.all.unreviewed}</td>`
    + `<td class="${s.error ? 'error' : s.review === 'reviewed' ? 'ok' : ''}">${escapeHtml(C.sheetStatusText(s))}</td></tr>`).join('');
  const tbody = el.sheets.tBodies[0];
  if (tbody.dataset.html !== rows) {
    tbody.dataset.html = rows;
    tbody.innerHTML = rows;
  }
  el.sheets.hidden = !sheets.length;
  if (L.counts) foldManualScan(sheets.length > 0);
  el.nextSheet.hidden = !sheets.length;
  el.nextSheet.disabled = !C.nextSheet(sheets, cur);
}

function renderCounts() {
  const c = L.counts;
  const has = !!(c && c.run_id);
  el.countsSection.hidden = !has;
  if (!has) return;
  const opts = [['', 'Whole set']].concat(c.sheets.map((s) => [String(s.page_index), `Sheet ${s.page_index + 1}`]));
  const html = opts.map((o) => o.join('|')).join('\n');
  if (el.countsScope.dataset.html !== html) {
    const keep = el.countsScope.value;
    el.countsScope.dataset.html = html;
    el.countsScope.replaceChildren(...opts.map(([v, t]) => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = t;
      return o;
    }));
    el.countsScope.value = opts.some(([v]) => v === keep) ? keep : '';
  }
  const { rows, total } = C.countsRows(c, el.countsScope.value);
  const cells = (n) => ['found', 'approved', 'rejected', 'added', 'confirmed', 'unreviewed']
    .map((k) => `<td class="num${k === 'confirmed' ? ' strong' : ''}">${n[k]}</td>`).join('');
  const body = rows.map((r) => `<tr><td title="${escapeHtml(r.name)}">${tagChipHtml(r.tag) || '<span class="muted">no tag</span>'}</td>${cells(r.n)}</tr>`).join('');
  const foot = total ? `<tr><td>All</td>${cells(total)}</tr>` : '';
  const tbody = el.countsTable.tBodies[0];
  if (tbody.dataset.html !== body) {
    tbody.dataset.html = body;
    tbody.innerHTML = body;
  }
  const tfoot = el.countsTable.tFoot;
  if (tfoot.dataset.html !== foot) {
    tfoot.dataset.html = foot;
    tfoot.innerHTML = foot;
  }
  el.csv.href = legendApi.csvUrl(c.document_version);
}

function renderTagTools(st) {
  const counted = L.legend.entries.filter((e) => e.count).map((e) => e.tag);
  const all = tags();
  const optHtml = counted.join('|');
  if (el.newPinTag.dataset.html !== optHtml) {
    el.newPinTag.dataset.html = optHtml;
    el.newPinTag.replaceChildren(...L.legend.entries.filter((e) => e.count).map((e) => {
      const o = document.createElement('option');
      o.value = e.tag;
      o.textContent = `${e.tag} ${e.name}`;
      return o;
    }));
    el.newPinTag.value = C.defaultPinTag(counted, st.pins, L.lastTag);
  }
  if (!counted.includes(el.newPinTag.value)) el.newPinTag.value = C.defaultPinTag(counted, st.pins, L.lastTag);
  const sel = st.selected;
  const as = el.newPinTag.value;
  el.approveAs.hidden = !(sel && sel.origin === 'machine' && as && sel.class_label !== as);
  el.approveAs.textContent = `Approve as ${as}`;
  const filterHtml = all.join('|');
  if (el.tagFilter.dataset.html !== filterHtml) {
    el.tagFilter.dataset.html = filterHtml;
    el.tagFilter.replaceChildren(...[['', 'All tags'], ...L.legend.entries.map((e) => [e.tag, `${e.tag} ${e.name}`])].map(([v, t]) => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = t;
      return o;
    }));
    el.tagFilter.value = L.tagFilter;
  }
  const keyHtml = L.legend.entries.filter((e) => e.count).map((e) => `${tagChipHtml(e.tag)} ${escapeHtml(e.name)}`).join('<br>');
  if (el.tagKey.dataset.html !== keyHtml) {
    el.tagKey.dataset.html = keyHtml;
    el.tagKey.innerHTML = keyHtml;
  }
}

function setStatus(node, text, kind = '') {
  node.textContent = text;
  node.className = 'status' + (kind ? ' ' + kind : '');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
