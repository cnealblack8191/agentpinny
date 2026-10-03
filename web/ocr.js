// Optional OCR (docs/ocr.md): read the sheet number, title and revision of
// the open page, and the admin's site-wide on/off switch. Kept out of app.js:
// it reads the open document and page from the URL fragment the viewer keeps
// up to date (#v=<version>&p=<page index>).
import { request } from './api.js';

const $ = (id) => document.getElementById(id);
const box = $('sheet-info');
const readBtn = $('sheet-info-btn');
const status = $('sheet-info-status');
const result = $('sheet-info-result');
const adminSection = $('ocr-section');
const engineSelect = $('ocr-engine');
const saveBtn = $('ocr-save-btn');
const adminStatus = $('ocr-status');

const enc = encodeURIComponent;
const path = (c) => `/api/documents/${enc(c.version)}/pages/${c.page}/sheet-info`;
const METHOD = { label: 'next to its label', table: 'from the revision table', pattern: 'a guess (no label found)' };

let enabled = false;
let shown = ''; // "version#page" whose saved result is on screen

function current() {
  const q = new URLSearchParams(location.hash.slice(1));
  const p = Number(q.get('p'));
  return q.get('v') && q.has('p') && Number.isInteger(p) ? { version: q.get('v'), page: p } : null;
}

function setStatus(el, text, kind) {
  el.textContent = text;
  el.className = 'status' + (kind ? ' ' + kind : '');
}

function row(label, field) {
  const tr = document.createElement('tr');
  const th = document.createElement('th');
  th.textContent = label;
  const td = document.createElement('td');
  if (field) {
    td.textContent = field.value;
    td.title = `Found ${METHOD[field.method] || field.method}, confidence ${Math.round(field.confidence)}`
      + (field.alternatives.length ? `. Also seen: ${field.alternatives.join(', ')}` : '');
    if (field.method === 'pattern') td.className = 'guess';
  } else {
    td.textContent = 'not found';
    td.className = 'muted';
  }
  tr.append(th, td);
  return tr;
}

function showResult(info) {
  result.replaceChildren();
  if (!info) {
    result.hidden = true;
    return;
  }
  const table = document.createElement('table');
  table.className = 'legend-table';
  table.append(row('Sheet', info.sheet_number), row('Title', info.sheet_title), row('Revision', info.revision));
  const note = document.createElement('p');
  note.className = 'muted legend-small';
  note.textContent = `Read by OCR ${info.read_at ? 'on ' + info.read_at.replace('T', ' ').replace('Z', ' UTC') : ''}. `
    + 'Check these against the drawing before relying on them.';
  result.append(table);
  for (const w of info.warnings || []) {
    const p = document.createElement('p');
    p.className = 'status error legend-small';
    p.textContent = w;
    result.append(p);
  }
  result.append(note);
  result.hidden = false;
}

async function refreshPage() {
  const cur = current();
  box.hidden = !enabled || !cur;
  if (box.hidden) return;
  const key = `${cur.version}#${cur.page}`;
  if (key === shown) return;
  shown = key;
  showResult(null);
  setStatus(status, '');
  try {
    const out = await request('GET', path(cur));
    if (`${cur.version}#${cur.page}` !== shown) return; // the page changed meanwhile
    showResult(out.sheet_info);
  } catch (err) {
    setStatus(status, err.message, 'error');
  }
}

readBtn.onclick = async () => {
  const cur = current();
  if (!cur) return;
  readBtn.disabled = true;
  setStatus(status, 'Reading the title block… (a few seconds)');
  try {
    const out = await request('POST', path(cur), { json: {} });
    if (`${cur.version}#${cur.page}` === shown) {
      showResult(out.sheet_info);
      setStatus(status, '');
    }
  } catch (err) {
    setStatus(status, err.message, 'error');
  } finally {
    readBtn.disabled = false;
  }
};

function showAdmin(state) {
  engineSelect.replaceChildren(...state.options.map((o) => {
    const opt = document.createElement('option');
    opt.value = o.name;
    opt.textContent = o.label + (o.license ? ` (${o.license})` : '') + (o.available ? '' : ' - not installed');
    opt.selected = o.name === state.engine;
    return opt;
  }));
  engineSelect.disabled = saveBtn.disabled = state.overridden;
  if (state.overridden) {
    setStatus(adminStatus, 'Set by PINNY_OCR_ENGINE on the server; change it there.');
  } else if (state.enabled && !state.available) {
    setStatus(adminStatus, 'OCR is on but the engine is not installed on the server.', 'error');
  }
}

saveBtn.onclick = async () => {
  saveBtn.disabled = true;
  try {
    const state = await request('POST', '/api/ocr', { json: { engine: engineSelect.value } });
    enabled = state.enabled;
    showAdmin(state);
    setStatus(adminStatus, state.enabled ? 'OCR is on for everyone.' : 'OCR is off.', 'ok');
    shown = '';
    refreshPage();
  } catch (err) {
    setStatus(adminStatus, err.message, 'error');
  } finally {
    saveBtn.disabled = false;
  }
};

async function init() {
  try {
    const [me, state] = await Promise.all([request('GET', '/api/me'), request('GET', '/api/ocr')]);
    enabled = state.enabled;
    if (me.role === 'admin') {
      adminSection.hidden = false;
      showAdmin(state);
    }
  } catch (err) {
    return; // signed out or offline: the viewer itself reports that
  }
  window.addEventListener('hashchange', refreshPage);
  refreshPage();
}

init();
