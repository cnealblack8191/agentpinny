// Browser check of the legend workflow (docs/viewer.md "Legend and whole set").
//
//   node tests/viewer/test_legend_browser.mjs
//
// Runs the real viewer server on a synthetic drawing set (tests/legend/pdfgen.py:
// a legend page, a plan, and a plan on a /Rotate 90 page) and drives the UI in
// Chromium: upload, find the legend, see each symbol's picture, untick and
// tick Count, change a tag, confirm, scan the whole set with progress, open
// the next sheet, see each pin's tag and colour, filter by tag, approve,
// reject, add a pin with a tag, mark the sheet reviewed, and check the counts
// table against the server. The page must run under the CSP.

import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
function loadPlaywright() {
  for (const p of [process.env.PLAYWRIGHT_MODULE, 'playwright', '/opt/node22/lib/node_modules/playwright']) {
    if (!p) continue;
    try { return require(p); } catch (e) { /* next */ }
  }
  throw new Error('Playwright not found; set PLAYWRIGHT_MODULE.');
}
const { chromium } = loadPlaywright();

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const PY = process.env.PYTHON || 'python3';
const SHOTS = process.env.PINNY_SCREENSHOT_DIR;
let failures = 0;
function check(name, ok, detail = '') {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}

const work = mkdtempSync(join(tmpdir(), 'pinny-legend-'));
const pdfPath = join(work, 'set.pdf');
const plainPath = join(work, 'plain.pdf'); // no legend
// Symbol centres per sheet (canonical px of the displayed page), by tag.
const want = JSON.parse(execFileSync(PY, ['-c', `
import json
from pathlib import Path
from tests.viewer.test_legend_workflow import legend_set
from tests.scan.test_scan import SHEET1, SHEET2, TAG, PX
import sys; sys.path.insert(0, 'tests/viewer')
from pdfgen import make_pdf
open(${JSON.stringify(plainPath)}, 'wb').write(make_pdf())
legend_set(Path(${JSON.stringify(pdfPath)}))
print(json.dumps({str(i + 1): [[TAG[k], x * PX, y * PX] for k, x, y, _ in s if k in TAG]
                  for i, s in enumerate((SHEET1, SHEET2))}))`], { cwd: REPO }).toString());

function startServer(dataDir) {
  const code = `
from pinny.viewer import ViewerService
from pinny.viewer.server import make_server
svc = ViewerService(${JSON.stringify(dataDir)})
httpd = make_server(svc, port=0)
print("http://127.0.0.1:%d/" % httpd.server_address[1], flush=True)
httpd.serve_forever()
`;
  return new Promise((res, rej) => {
    const proc = spawn(PY, ['-c', code], { cwd: REPO, env: { ...process.env, PINNY_REVIEWER: 'e2e' } });
    let out = '';
    proc.stdout.on('data', (d) => {
      out += d;
      const m = out.match(/http:\/\/127\.0\.0\.1:(\d+)\//);
      if (m) res({ proc, base: `http://127.0.0.1:${m[1]}` });
    });
    proc.stderr.on('data', (d) => process.stderr.write(d));
    proc.on('exit', (c) => rej(new Error('server exited ' + c + out)));
  });
}

const settle = (page) => page.evaluate(() => new Promise((r) =>
  requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 30)))));
async function waitFor(page, fn, arg, timeout = 60000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
  await settle(page);
}
const waitIdle = (page) => waitFor(page, () => window.__pinny && window.__pinny.idle());
const legendOf = (page) => page.evaluate(() => window.__pinny.legend());

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
let server = null;
try {
  server = await startServer(join(work, 'data'));
  const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
  const dialogs = [];
  const cspErrors = [];
  page.on('dialog', (d) => { dialogs.push(d.message()); d.accept(); });
  page.on('pageerror', (e) => check('no page errors', false, e.message));
  page.on('console', (m) => { if (/Content Security Policy/i.test(m.text())) cspErrors.push(m.text()); });
  await page.goto(server.base + '/');
  check('no legend section before a drawing is open', await page.locator('#legend-section').isHidden());
  await page.locator('#file').setInputFiles(pdfPath);
  await waitFor(page, () => document.querySelectorAll('#pages button').length === 3);
  check('legend section appears for the open drawing', await page.locator('#legend-section').isVisible());

  // --------------------------------------------------------- find legend
  await page.locator('#legend-find-btn').click();
  await waitFor(page, () => window.__pinny.legend().legend && window.__pinny.page() === 0 && window.__pinny.idle());
  let lg = (await legendOf(page)).legend;
  check('the legend is read from page 1', lg.page_index === 0 && lg.entries.length === 5,
    lg.entries.map((e) => e.tag).join(','));
  await waitFor(page, () => document.querySelectorAll('#legend-table tbody tr').length === 5);
  const tagsShown = await page.locator('#legend-table tbody tr td:nth-child(2)').allTextContents();
  check('table lists each tag', tagsShown.map((t) => t.trim()).join(',') === 'D,G,DD,Q,T', tagsShown.join(','));
  await page.waitForTimeout(300);
  const ink = await page.evaluate(() => [...document.querySelectorAll('#legend-table canvas.legend-symbol')].map((cv) => {
    const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
    let dark = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i] < 128) dark++;
    return dark;
  }));
  check('each row shows its symbol cut from the legend page', ink.length === 5 && ink.every((n) => n > 10), ink.join(','));
  const tRow = page.locator('#legend-table tbody tr').nth(4);
  check('the thermostat (by mech) starts not counted', !(await tRow.locator('input[type=checkbox]').isChecked()));
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'legend-read.png') });

  // Toggle Count, and change a tag.
  const qBox = page.locator('#legend-table tbody tr').nth(3).locator('input[type=checkbox]');
  await qBox.uncheck();
  await waitFor(page, () => window.__pinny.legend().legend.entries[3].count === false && !window.__pinny.legend().busy);
  check('unticking Count is saved', true);
  await page.locator('#legend-table tbody tr').nth(3).locator('input[type=checkbox]').check();
  await waitFor(page, () => window.__pinny.legend().legend.entries[3].count === true);
  await page.locator('#legend-table tbody tr').nth(1).locator('td').nth(2).click(); // the GFCI row
  await waitFor(page, () => window.__pinny.legend().selected === 'L2' && window.__pinny.idle());
  const view = await page.evaluate(() => {
    const e = window.__pinny.legend().legend.entries[1];
    const b = e.symbol_boxes[0];
    const s = window.__pinny.toScreen((b[0] + b[2]) / 2, (b[1] + b[3]) / 2);
    const r = document.getElementById('viewport').getBoundingClientRect();
    return { s, w: r.width, h: r.height };
  });
  check('clicking a row brings its symbol to the middle of the view',
    Math.abs(view.s.x - view.w / 2) < 3 && Math.abs(view.s.y - view.h / 2) < 3, JSON.stringify(view));
  check('the editor opens for the row', await page.locator('#legend-editor').isVisible());
  await page.locator('#le-tag').fill('GF');
  await page.locator('#le-save').click();
  await waitFor(page, () => window.__pinny.legend().legend.entries[1].tag === 'GF');
  check('a tag change is saved', (await page.locator('#legend-status').textContent()).includes('Saved'));
  check('setscan waits for a confirmed legend', await page.locator('#setscan-body').isHidden());

  // ------------------------------------------------------------- confirm
  await page.locator('#legend-confirm-btn').click();
  await waitFor(page, () => window.__pinny.legend().legend.confirmed);
  check('confirm legend', (await page.locator('#legend-confirm-btn').textContent()) === 'Legend confirmed');

  // --------------------------------------------------------- scan the set
  await page.evaluate(() => {
    window.__samples = [];
    window.__sampler = setInterval(() => {
      const p = document.getElementById('setscan-progress');
      window.__samples.push({ hidden: p.hidden, max: p.max, text: document.getElementById('setscan-status').textContent });
    }, 20);
  });
  await page.locator('#setscan-btn').click();
  await waitFor(page, () => {
    const r = window.__pinny.legend().run;
    return r && r.status === 'done' && window.__pinny.legend().counts && window.__pinny.legend().counts.run_id;
  });
  const samples = await page.evaluate(() => { clearInterval(window.__sampler); return window.__samples; });
  check('start asks first, naming the sheet count', dialogs.some((m) => m.includes('Scan all 2 sheet(s)')), dialogs.join(' | '));
  check('progress shows sheets while scanning', samples.some((s) => !s.hidden && s.max === 2)
    && samples.some((s) => /sheet\(s\)/.test(s.text)), JSON.stringify(samples.slice(0, 3)));
  const status = await page.locator('#setscan-status').textContent();
  check('the result is summed up', status.includes('Scanned 2 sheet(s) and found 13 symbol(s)'), status);
  const sheetRows = await page.locator('#sheets-table tbody tr').allTextContents();
  check('sheets list shows each sheet with its counts', sheetRows.length === 2 && sheetRows[0].startsWith('2')
    && sheetRows[0].includes('to review'), sheetRows.join(' | '));
  check('counts panel appears', await page.locator('#counts-section').isVisible());

  // -------------------------------------------------- review sheet by sheet
  await page.locator('#next-sheet-btn').click();
  await waitFor(page, () => window.__pinny.page() === 1 && window.__pinny.pins().length > 0 && window.__pinny.idle());
  let pins = await page.evaluate(() => window.__pinny.pins());
  const wantTags = { D: 'D', G: 'GF', DD: 'DD', Q: 'Q' };
  const onSymbols = (ps, sheet) => ps.every((p) => want[sheet].some(([t, x, y]) =>
    wantTags[t] === p.class_label && Math.hypot(x - p.x, y - p.y) < 25))
    && want[sheet].every(([t, x, y]) => ps.some((p) => p.class_label === wantTags[t] && Math.hypot(x - p.x, y - p.y) < 25));
  check('next sheet opens sheet 2 with a tagged pin on every symbol', pins.length === 10 && onSymbols(pins, '1'),
    pins.map((p) => `${p.class_label}@${Math.round(p.x)},${Math.round(p.y)}`).join(' '));
  check('tag tools and key show for a legend scan', await page.locator('#tag-tools').isVisible()
    && (await page.locator('#tag-key').textContent()).includes('GFCI'));
  const tagCells = await page.locator('#pin-table tbody tr td:nth-child(4)').allTextContents();
  check('pin list has a tag column', tagCells.length === 10 && tagCells.includes('GF'), tagCells.join(','));
  // Each pin's label is drawn in its tag's colour.
  const colours = await page.evaluate(() => {
    const out = [];
    const cv = document.getElementById('canvas');
    const ctx = cv.getContext('2d');
    const k = cv.width / cv.getBoundingClientRect().width;
    const chips = {};
    for (const el of document.querySelectorAll('#tag-key .tag-chip')) chips[el.textContent] = getComputedStyle(el).backgroundColor;
    for (const p of window.__pinny.pins()) {
      const s = window.__pinny.toScreen(p.x, p.y);
      const d = ctx.getImageData(Math.round((s.x + 10) * k), Math.round((s.y - 2) * k), 1, 1).data;
      out.push({ tag: p.class_label, px: `rgb(${d[0]}, ${d[1]}, ${d[2]})`, chip: chips[p.class_label] });
    }
    return out;
  });
  check('each pin carries a label in its tag colour, matching the key', colours.every((c) => c.px === c.chip),
    JSON.stringify(colours.filter((c) => c.px !== c.chip)));
  check('different tags get different colours', new Set(colours.map((c) => c.chip)).size === 4);
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'legend-sheet.png') });

  // Filter by tag.
  await page.locator('#tag-filter').selectOption('GF');
  await settle(page);
  const shown = await page.locator('#pin-table tbody tr').count();
  check('filter by tag shows only that tag', shown === 2, String(shown));
  await page.locator('#tag-filter').selectOption('');
  await settle(page);

  // Approve one, reject one, add one with a tag.
  const rows = page.locator('#pin-table tbody tr');
  await rows.nth(0).click();
  await page.locator('#viewport').focus();
  await page.keyboard.press('a');
  await waitIdle(page);
  await rows.nth(1).click();
  await page.locator('#viewport').focus();
  await page.keyboard.press('x');
  await waitIdle(page);
  await page.locator('#new-pin-tag').selectOption('DD');
  await page.locator('#viewport').focus();
  await page.keyboard.press('p');
  await page.evaluate(() => window.__pinny.lookAt(1700, 1500, 0.5, 0));
  await settle(page);
  const r = await page.evaluate(() => document.getElementById('canvas').getBoundingClientRect().toJSON());
  const at = await page.evaluate(() => window.__pinny.toScreen(1700, 1500));
  await page.mouse.click(r.x + at.x, r.y + at.y);
  await waitIdle(page);
  pins = await page.evaluate(() => window.__pinny.pins());
  const added = pins.find((p) => p.origin === 'manual');
  check('a pin added by hand gets the chosen tag', added && added.class_label === 'DD' && !added.temp,
    JSON.stringify(added));
  // Correct a pin's symbol: approve a duplex as a GFCI.
  await page.keyboard.press('v');
  const dPin = pins.find((p) => p.class_label === 'D' && p.state === 'unreviewed');
  await page.evaluate((id) => {
    [...document.querySelectorAll('#pin-table tbody tr')].find((x) => x.dataset.pin === id).click();
  }, dPin.pin_id);
  await page.locator('#new-pin-tag').selectOption('GF');
  await settle(page);
  check('approve as another tag is offered for the selected pin',
    (await page.locator('#approve-as-btn').textContent()) === 'Approve as GF');
  await page.locator('#approve-as-btn').click();
  await waitIdle(page);
  const relabelled = (await page.evaluate(() => window.__pinny.pins())).find((p) => p.pin_id === dPin.pin_id);
  check('the pin is approved with the corrected tag', relabelled.state === 'approved'
    && relabelled.class_label === 'GF', JSON.stringify(relabelled));
  pins = await page.evaluate(() => window.__pinny.pins());
  // Approve the rest, then mark the sheet fully reviewed.
  for (const p of pins.filter((x) => x.state === 'unreviewed')) {
    await page.evaluate((id) => {
      const tr = [...document.querySelectorAll('#pin-table tbody tr')].find((x) => x.dataset.pin === id);
      tr.click();
    }, p.pin_id);
    await page.locator('#viewport').focus();
    await page.keyboard.press('a');
  }
  await waitIdle(page);
  pins = await page.evaluate(() => window.__pinny.pins());
  check('every machine pin is answered', pins.every((p) => p.state !== 'unreviewed'));
  await page.locator('#mark-reviewed-btn').click();
  await waitFor(page, () => document.getElementById('mark-reviewed-status').textContent.includes('fully reviewed'));
  await waitFor(page, () => {
    const c = window.__pinny.legend().counts;
    return c && c.sheets[0].review === 'reviewed';
  });
  const sheetRows2 = await page.locator('#sheets-table tbody tr').allTextContents();
  check('the sheets list shows the sheet as reviewed', sheetRows2[0].includes('reviewed')
    && !sheetRows2[0].includes('to review'), sheetRows2.join(' | '));

  // ------------------------------------------------------------ counts
  const server_ = await page.evaluate(async () => (await fetch(`/api/documents/${encodeURIComponent(
    window.__pinny.legend().counts.document_version)}/counts`)).json());
  const table = await page.evaluate(() => [...document.querySelectorAll('#counts-table tbody tr')].map((tr) =>
    [...tr.querySelectorAll('td')].map((td) => td.textContent.trim())));
  const keys = ['found', 'approved', 'rejected', 'added', 'confirmed', 'unreviewed'];
  const expectRows = server_.tags.filter((t) => t.counted).map((t) =>
    [t.tag, ...keys.map((k) => String((server_.totals.by_tag[t.tag] || {})[k] || 0))]);
  check('counts table matches the server, per tag', JSON.stringify(table) === JSON.stringify(expectRows),
    JSON.stringify(table));
  const s2 = server_.sheets[0].all;
  check('sheet 2: 1 rejected, 1 added, 10 confirmed', s2.rejected === 1 && s2.added === 1 && s2.confirmed === 10
    && s2.unreviewed === 0, JSON.stringify(s2));
  const g = server_.sheets[0].by_tag.GF;
  check('a corrected tag counts as the new symbol; found stays as Pinny found it',
    g.found === 2 && g.approved === 3, JSON.stringify(g));
  const foot = await page.locator('#counts-table tfoot tr td').allTextContents();
  check('the total row adds up', foot[0] === 'All' && foot[5] === String(server_.totals.all.confirmed), foot.join(','));
  await page.locator('#counts-scope').selectOption('2');
  await settle(page);
  const oneSheet = await page.evaluate(() => [...document.querySelectorAll('#counts-table tfoot td')].map((x) => x.textContent));
  check('counts for one sheet', oneSheet[1] === String(server_.sheets[1].all.found), oneSheet.join(','));
  const csv = await page.evaluate(async () => {
    const a = document.getElementById('counts-csv');
    const res = await fetch(a.href);
    return { type: res.headers.get('content-type'), text: await res.text(), download: a.hasAttribute('download') };
  });
  check('counts download as CSV', csv.type.startsWith('text/csv') && csv.download
    && csv.text.startsWith('Sheet,Sheet status,Tag'), csv.type);

  // ------------------------------------------------ the rotated sheet
  await page.locator('#sheets-table tbody tr').nth(1).click();
  await waitFor(page, () => window.__pinny.page() === 2 && window.__pinny.pins().length > 0 && window.__pinny.idle());
  pins = await page.evaluate(() => window.__pinny.pins());
  const frame = await page.evaluate(() => window.__pinny.frame());
  check('the rotated sheet shows landscape with its pins on the symbols', frame.width > frame.height
    && pins.length === 3 && onSymbols(pins, '2'), pins.map((p) => `${p.class_label}@${Math.round(p.x)},${Math.round(p.y)}`).join(' '));
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'legend-rotated.png') });

  // Reload: the legend, scan and counts come back.
  await page.reload();
  await waitFor(page, () => window.__pinny.legend().counts && window.__pinny.legend().counts.run_id
    && window.__pinny.legend().legend && window.__pinny.legend().legend.confirmed);
  check('after a reload the legend and counts are back', true);

  // A drawing without a legend points to the template workflow.
  await page.locator('#file').setInputFiles(plainPath);
  await waitFor(page, () => document.querySelectorAll('#pages button').length === 1 && window.__pinny.idle());
  check('another drawing starts without a legend', await page.locator('#legend-body').isHidden()
    && await page.locator('#counts-section').isHidden());
  await page.locator('#legend-find-btn').click();
  await waitFor(page, () => document.getElementById('legend-status').className.includes('error'));
  const msg = await page.locator('#legend-status').textContent();
  check('no legend: a plain message pointing to the template workflow', msg.includes('could not find a symbol legend')
    && msg.includes('Template box'), msg);
  check('no CSP violations', cspErrors.length === 0, cspErrors.join(' | '));
  await page.close();
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all checks passed');
process.exit(failures ? 1 : 0);
