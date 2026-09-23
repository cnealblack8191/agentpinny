// End-to-end browser test of the viewer against the real local server
// (with the stub render service). Measures overlay alignment (check I4) from
// the pixels actually drawn on the canvas.
//
//   node tests/viewer/browser_e2e.mjs
//
// Needs Python deps (numpy, opencv) and Playwright with Chromium. Set
// PLAYWRIGHT_MODULE to the Playwright package path if it isn't resolvable.

import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';

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
const TOL = 1.0; // canonical px (I4)
const results = [];
let failures = 0;

function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}

// ------------------------------------------------------------- fixtures
const work = mkdtempSync(join(tmpdir(), 'pinny-e2e-'));
const pdfPath = join(work, 'plan.pdf');
execFileSync('python3', ['-c', `
import sys; sys.path.insert(0, 'tests/viewer')
from pdfgen import make_pdf
open(${JSON.stringify(pdfPath)}, 'wb').write(make_pdf([(612, 792, 0), (612, 792, 90), (1728, 1152, 0)]))
`], { cwd: REPO });

function startServer(dataDir) {
  return new Promise((res, rej) => {
    const proc = spawn('python3', ['-m', 'pinny.viewer', '--data-dir', dataDir, '--port', '0'],
      { cwd: REPO, env: { ...process.env, PINNY_REVIEWER: 'e2e' } });
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

// --------------------------------------------------------------- helpers
const settle = (page) => page.evaluate(() => new Promise((r) =>
  requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 30)))));

async function waitFor(page, fn, arg, timeout = 20000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
  await settle(page);
}

const waitIdle = (page) => waitFor(page, () => window.__pinny && window.__pinny.idle());

// Weighted centroid (CSS px, viewport-relative) of red or magenta pixels on
// the canvas inside a square of half-size `r` CSS px around (cx, cy).
function centroid(page, kind, cx, cy, r) {
  return page.evaluate(({ kind, cx, cy, r }) => {
    const c = document.getElementById('canvas');
    const kx = c.width / parseFloat(c.style.width);
    const ky = c.height / parseFloat(c.style.height);
    const x0 = Math.max(0, Math.floor((cx - r) * kx));
    const y0 = Math.max(0, Math.floor((cy - r) * ky));
    const x1 = Math.min(c.width, Math.ceil((cx + r) * kx));
    const y1 = Math.min(c.height, Math.ceil((cy + r) * ky));
    if (x1 <= x0 || y1 <= y0) return null;
    const d = c.getContext('2d').getImageData(x0, y0, x1 - x0, y1 - y0).data;
    let sw = 0; let sx = 0; let sy = 0;
    for (let j = 0; j < y1 - y0; j++) {
      for (let i = 0; i < x1 - x0; i++) {
        const k = 4 * (j * (x1 - x0) + i);
        const R = d[k]; const G = d[k + 1]; const B = d[k + 2];
        const w = kind === 'red' ? R - Math.max(G, B) : Math.min(R, B) - G;
        if (w <= 40) continue;
        sw += w; sx += w * (x0 + i + 0.5); sy += w * (y0 + j + 0.5);
      }
    }
    return sw ? { x: sx / sw / kx, y: sy / sw / ky, weight: sw } : null;
  }, { kind, cx, cy, r });
}

async function canvasOrigin(page) {
  return page.evaluate(() => {
    const r = document.getElementById('canvas').getBoundingClientRect();
    return { x: r.left, y: r.top };
  });
}

function markers(frame) {
  const i = 30;
  const { width: w, height: h } = frame;
  return [[i, i], [w - i, i], [i, h - i], [w - i, h - i], [Math.floor(w / 2), Math.floor(h / 2)]];
}

async function setHidePins(page, hide) {
  const box = page.locator('#hide-pins');
  if ((await box.isChecked()) !== hide) await box.click();
  await settle(page);
}

async function setMode(page, mode) {
  await page.locator(`input[name=mode][value=${mode}]`).check();
}

async function apiJson(base, path, init) {
  const r = await fetch(base + path, init);
  return r.json();
}

// Measure raster-marker alignment: drawn red centroid vs the canonical point.
async function markerAlignment(page, label, zooms, rotations) {
  const frame = await page.evaluate(() => window.__pinny.frame());
  await setHidePins(page, true);
  let worst = 0;
  let where = '';
  for (const rotation of rotations) {
    for (const zoom of zooms) {
      for (const [mx, my] of markers(frame)) {
        await page.evaluate(([x, y, z, r]) => window.__pinny.lookAt(x, y, z, r), [mx, my, zoom, rotation]);
        await settle(page);
        const expect = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [mx, my]);
        const got = await centroid(page, 'red', expect.x, expect.y, Math.max(6, 9 * zoom));
        if (!got) { worst = Infinity; where = `no marker at ${mx},${my} z${zoom} r${rotation}`; continue; }
        const err = Math.hypot(got.x - expect.x, got.y - expect.y) / zoom;
        if (err > worst) { worst = err; where = `(${mx},${my}) zoom ${zoom} rot ${rotation}`; }
      }
    }
  }
  check(`${label}: raster markers vs transform, max error ${worst.toFixed(3)} canonical px`,
    worst <= TOL, `worst at ${where}`);
  await setHidePins(page, false);
  return worst;
}

// Pins drawn vs raster markers (both measured from pixels, independent of
// the app's own transform).
async function pinVsMarker(page, label, points, zoom, rotation) {
  let worst = 0;
  let where = '';
  await page.keyboard.press('Escape'); // clear selection ring
  for (const [mx, my] of points) {
    await page.evaluate(([x, y, z, r]) => window.__pinny.lookAt(x, y, z, r), [mx, my, zoom, rotation]);
    await settle(page);
    const s = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [mx, my]);
    const ring = await centroid(page, 'magenta', s.x, s.y, 13);
    await setHidePins(page, true);
    const red = await centroid(page, 'red', s.x, s.y, Math.max(6, 9 * zoom));
    await setHidePins(page, false);
    if (!ring || !red) { worst = Infinity; where = `missing at ${mx},${my}`; continue; }
    const err = Math.hypot(ring.x - red.x, ring.y - red.y) / zoom;
    if (err > worst) { worst = err; where = `(${mx},${my})`; }
  }
  check(`${label}: pin overlay vs raster marker at zoom ${zoom}, rot ${rotation}: max ${worst.toFixed(3)} canonical px`,
    worst <= TOL, where);
}

// ------------------------------------------------------------------ run
const browser = await chromium.launch();

async function pass(dpr) {
    const { proc, base } = await startServer(join(work, `data-${dpr}`));
    try {
    console.log(`\n=== deviceScaleFactor ${dpr} ===`);
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: dpr });
    const page = await context.newPage();
    page.on('pageerror', (e) => check(`no page errors (dpr ${dpr})`, false, e.message));
    page.on('dialog', (d) => d.accept());
    await page.goto(base + '/');

    // 1. Upload
    await page.locator('#file').setInputFiles(pdfPath);
    await waitFor(page, () => document.querySelectorAll('#pages button').length === 3);
    check(`upload shows 3 pages (dpr ${dpr})`, true);
    const status = await page.locator('#doc-status').textContent();
    check('upload status readable', /3 page/.test(status), status);
    check('stub banner visible', await page.locator('#stub-banner').isVisible());

    // 2. Choose a page
    await page.locator('#pages button').nth(0).click();
    await waitIdle(page);
    const frame = await page.evaluate(() => window.__pinny.frame());
    check('page 1 frame is 1700x2200 @200dpi', frame.width === 1700 && frame.height === 2200 && frame.dpi === 200);

    // 3. Raster markers at several zooms, rotations
    await markerAlignment(page, `dpr ${dpr} page 1`, [0.25, 0.5, 1, 2, 4, 8], [0, 90, 180, 270]);

    // 4. Template selection by drag (at rotation 90, zoom 2)
    const doc = (await apiJson(base, '/api/documents')).documents[0];
    const sym = JSON.parse(execFileSync('python3', ['-c', `
import json
from pinny.viewer.render_stub import stub_symbol_centres
print(json.dumps([s for s in stub_symbol_centres(${JSON.stringify(doc.document_version)}, 0, 1700, 2200)]))`],
    { cwd: REPO }).toString());
    const [sx, sy] = sym.find((s) => s[2] === 0);
    const want = { x: sx - 22, y: sy - 22, width: 44, height: 44 };
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 2, 90), [sx, sy]);
    await settle(page);
    await setMode(page, 'template');
    const o = await canvasOrigin(page);
    const a = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [want.x + 0.25, want.y + 0.25]);
    const b = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [want.x + want.width - 0.25, want.y + want.height - 0.25]);
    await page.mouse.move(o.x + a.x, o.y + a.y);
    await page.mouse.down();
    await page.mouse.move(o.x + (a.x + b.x) / 2, o.y + (a.y + b.y) / 2, { steps: 4 });
    await page.mouse.move(o.x + b.x, o.y + b.y, { steps: 4 });
    await page.mouse.up();
    await settle(page);
    const tpl = await page.evaluate(() => window.__pinny.template());
    check('template drag at rot 90 / zoom 2 gives the intended canonical box',
      JSON.stringify(tpl) === JSON.stringify(want), JSON.stringify(tpl));
    check('template drag did not pan the view or add pins',
      (await page.evaluate(() => window.__pinny.pins().length)) === 0);

    // Selection is clamped to the page.
    await page.evaluate(() => window.__pinny.lookAt(0, 0, 1, 0));
    await settle(page);
    const c0 = await page.evaluate(() => window.__pinny.toScreen(-40, -40));
    const c1 = await page.evaluate(() => window.__pinny.toScreen(30, 25));
    await page.mouse.move(o.x + c0.x, o.y + c0.y);
    await page.mouse.down();
    await page.mouse.move(o.x + c1.x, o.y + c1.y, { steps: 5 });
    await page.mouse.up();
    await settle(page);
    const clamped = await page.evaluate(() => window.__pinny.template());
    check('selection dragged past the page edge is clipped to the page',
      clamped.x === 0 && clamped.y === 0 && clamped.width === 30 && clamped.height === 25, JSON.stringify(clamped));

    // Reselect the real template, scan.
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [sx, sy]);
    await settle(page);
    const a2 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [want.x + 0.25, want.y + 0.25]);
    const b2 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [want.x + want.width - 0.25, want.y + want.height - 0.25]);
    await page.mouse.move(o.x + a2.x, o.y + a2.y);
    await page.mouse.down();
    await page.mouse.move(o.x + b2.x, o.y + b2.y, { steps: 5 });
    await page.mouse.up();
    await settle(page);

    // 5. Scan
    await page.locator('#scan-btn').click();
    await waitFor(page, () => window.__pinny.pins().length > 0);
    await waitIdle(page);
    const pins = await page.evaluate(() => window.__pinny.pins());
    check('scan shows all 14 stub symbols with scores', pins.length === 14 && pins.every((p) => p.score >= 0.8),
      `${pins.length} pins`);
    const exact = pins.every((p) => sym.some(([x, y]) => x === p.x && y === p.y));
    check('scan pins are at the symbol centres', exact);
    const tableRows = await page.locator('#pin-table tbody tr').count();
    check('pin table lists every match', tableRows === 14);

    // Machine pins drawn where the symbols are (orange ring vs symbol centre).
    // 6. Approve / reject / add
    await setMode(page, 'pan');
    const first = pins[0];
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1.5, 0), [first.x, first.y]);
    await settle(page);
    const fp = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [first.x, first.y]);
    await page.mouse.click(o.x + fp.x + 3, o.y + fp.y - 2); // near the pin
    await settle(page);
    check('clicking near a pin in pan mode selects it',
      (await page.locator('#pin-info').textContent()).includes(first.pin_id));
    await page.locator('#approve-btn').click();
    await waitIdle(page);
    const scanId = await page.evaluate(() => window.__pinny.scanId());
    let server = await apiJson(base, `/api/scans/${scanId}`);
    check('approve saved to the server', server.pins.find((p) => p.pin_id === first.pin_id).state === 'approved');
    check('save status says saved', (await page.locator('#save-status').textContent()).includes('All changes saved'));

    await page.keyboard.press('n'); // next unreviewed
    await settle(page);
    const second = await page.evaluate(() => window.__pinny.pins().find((p) => p.pin_id !== undefined && document.getElementById('pin-info').textContent.includes(p.pin_id + ' ')));
    await page.keyboard.press('x');
    await waitIdle(page);
    server = await apiJson(base, `/api/scans/${scanId}`);
    check('reject (X key) saved to the server', second && server.pins.find((p) => p.pin_id === second.pin_id).state === 'rejected',
      second ? second.pin_id : 'no pin selected');

    // Manual pins at the markers, placed by clicking in add mode at several
    // zoom levels and rotations; then read back.
    await setMode(page, 'add');
    const addCases = [[0.5, 0], [1, 90], [2, 180], [4, 270], [8, 0]];
    const mk = markers(frame);
    let worstAdd = 0;
    for (let n = 0; n < mk.length; n++) {
      const [mx, my] = mk[n];
      const [zoom, rot] = addCases[n];
      await page.evaluate(([x, y, z, r]) => window.__pinny.lookAt(x, y, z, r), [mx, my, zoom, rot]);
      await settle(page);
      await setHidePins(page, true);
      const s = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [mx, my]);
      const red = await centroid(page, 'red', s.x, s.y, Math.max(6, 9 * zoom));
      await setHidePins(page, false);
      await page.mouse.click(o.x + red.x, o.y + red.y);
      await waitIdle(page);
      const manual = (await page.evaluate(() => window.__pinny.pins())).filter((p) => p.origin === 'manual');
      const last = manual.reduce((best, p) => (Math.hypot(p.x - mx, p.y - my) < Math.hypot(best.x - mx, best.y - my) ? p : best));
      worstAdd = Math.max(worstAdd, Math.hypot(last.x - mx, last.y - my));
    }
    server = await apiJson(base, `/api/scans/${scanId}`);
    const savedManual = server.pins.filter((p) => p.origin === 'manual');
    check('5 manual pins saved (4 corners + centre)', savedManual.length === 5);
    check(`click-to-pin at zooms 0.5-8 and all rotations: max error ${worstAdd.toFixed(3)} canonical px`, worstAdd <= TOL);
    check('manual pins are inside the page', savedManual.every((p) => p.x >= 0 && p.y >= 0 && p.x <= 1700 && p.y <= 2200));

    // Clicking outside the page in add mode adds nothing.
    await page.evaluate(() => window.__pinny.lookAt(0, 0, 1, 0));
    await settle(page);
    const out = await page.evaluate(() => window.__pinny.toScreen(-60, -60));
    await page.mouse.click(o.x + out.x, o.y + out.y);
    await waitIdle(page);
    check('click outside the page does not add a pin',
      (await page.evaluate(() => window.__pinny.pins().filter((p) => p.origin === 'manual').length)) === 5);

    // 7. Pins stay aligned after pan, zoom, resize and reload.
    await setMode(page, 'pan');
    for (const [z, r] of [[1, 0], [2, 90], [4, 180], [8, 270], [0.5, 0]]) {
      await pinVsMarker(page, `dpr ${dpr}`, mk, z, r);
    }
    // Pan by dragging and zoom with the wheel, then measure without re-centring.
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 2, 0), mk[4]);
    await settle(page);
    await page.mouse.move(o.x + 600, o.y + 400);
    await page.mouse.down();
    await page.mouse.move(o.x + 537, o.y + 371, { steps: 6 });
    await page.mouse.up();
    await page.mouse.move(o.x + 480, o.y + 350);
    await page.mouse.wheel(0, -240);
    await settle(page);
    const v = await page.evaluate(() => window.__pinny.view());
    const s4 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), mk[4]);
    await page.keyboard.press('Escape');
    await settle(page);
    const ring = await centroid(page, 'magenta', s4.x, s4.y, 13);
    await setHidePins(page, true);
    const red4 = await centroid(page, 'red', s4.x, s4.y, 9 * v.zoom);
    await setHidePins(page, false);
    const errPan = Math.hypot(ring.x - red4.x, ring.y - red4.y) / v.zoom;
    check(`after mouse pan + wheel zoom (zoom ${v.zoom.toFixed(3)}): pin vs marker ${errPan.toFixed(3)} canonical px`, errPan <= TOL);

    await page.setViewportSize({ width: 900, height: 700 });
    await settle(page);
    await pinVsMarker(page, `dpr ${dpr} after resize to 900x700`, mk, 3, 90);
    await page.setViewportSize({ width: 1280, height: 860 });
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 2.5, 270), mk[1]);
    await settle(page);
    await page.waitForTimeout(300); // URL hash is written after a short debounce
    const viewBefore = await page.evaluate(() => window.__pinny.view());
    await page.reload();
    await waitIdle(page);
    await waitFor(page, () => window.__pinny.pins().length >= 19);
    const viewAfter = await page.evaluate(() => window.__pinny.view());
    check('reload restores document, page, scan and view',
      Math.abs(viewAfter.zoom - viewBefore.zoom) < 1e-6 && viewAfter.rotation === viewBefore.rotation
      && Math.abs(viewAfter.panX - viewBefore.panX) < 0.01, JSON.stringify(viewAfter));
    const afterReload = await page.evaluate(() => window.__pinny.pins());
    const sameStates = afterReload.find((p) => p.pin_id === first.pin_id).state === 'approved'
      && afterReload.filter((p) => p.origin === 'manual').length === 5;
    check('reload shows saved review state', sameStates);
    await pinVsMarker(page, `dpr ${dpr} after reload`, mk, 2.5, 270);
    await markerAlignment(page, `dpr ${dpr} after reload`, [1, 4], [0, 90]);

    // Delete a manual pin.
    await setMode(page, 'pan');
    const m0 = afterReload.find((p) => p.origin === 'manual');
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 2, 0), [m0.x, m0.y]);
    await settle(page);
    const mp = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [m0.x, m0.y]);
    await page.mouse.click(o.x + mp.x, o.y + mp.y);
    await settle(page);
    check('manual pin selected shows Delete pin', (await page.locator('#reject-btn').textContent()) === 'Delete pin');
    await page.locator('#reject-btn').click();
    await waitIdle(page);
    server = await apiJson(base, `/api/scans/${scanId}`);
    check('delete manual pin saved (state removed)', server.pins.find((p) => p.pin_id === m0.pin_id).state === 'removed');

    // 8. Save failure: shown, retried, never dropped.
    await page.route('**/api/scans/*/actions', (route) => route.fulfill({ status: 503,
      contentType: 'application/json', body: '{"error":{"code":"down","message":"Server unavailable"}}' }));
    const third = (await page.evaluate(() => window.__pinny.pins())).find((p) => p.state === 'unreviewed');
    await page.locator('#pin-table tbody tr', { hasText: third.pin_id + '' }).first().click();
    await page.locator('#approve-btn').click();
    await settle(page);
    check('failing save shows pending state', /Saving 1 edit/.test(await page.locator('#save-status').textContent()));
    await waitFor(page, () => window.__pinny.queue().some((e) => e.status === 'failed'), null, 15000);
    check('after automatic retries the edit is marked not saved with Retry',
      /not saved/.test(await page.locator('#save-status').textContent())
      && (await page.locator('#failures button', { hasText: 'Retry' }).count()) === 1);
    check('report export disabled while an edit is unsaved',
      (await page.locator('#report-link').getAttribute('aria-disabled')) === 'true');
    // Reload while the edit is unsaved: it must survive and still be retryable.
    await page.reload();
    await waitIdle(page).catch(() => {});
    await waitFor(page, () => window.__pinny.queue().length === 1);
    check('unsaved edit survives a reload', true);
    await page.unroute('**/api/scans/*/actions');
    await page.locator('#failures button', { hasText: 'Retry' }).click();
    await waitIdle(page);
    server = await apiJson(base, `/api/scans/${scanId}`);
    check('retry after recovery saves the edit', server.pins.find((p) => p.pin_id === third.pin_id).state === 'approved');

    // In-flight edit lost to a reload is re-sent exactly once.
    await page.route('**/api/scans/*/actions', () => { /* never answer */ });
    const fourth = (await page.evaluate(() => window.__pinny.pins())).find((p) => p.state === 'unreviewed');
    await page.locator('#pin-table tbody tr', { hasText: fourth.pin_id }).first().click();
    await page.locator('#approve-btn').click();
    await settle(page);
    await page.unroute('**/api/scans/*/actions');
    await page.reload();
    await waitIdle(page);
    server = await apiJson(base, `/api/scans/${scanId}`);
    check('edit in flight during reload is re-sent and saved', server.pins.find((p) => p.pin_id === fourth.pin_id).state === 'approved');

    // Report
    const href = await page.locator('#report-link').getAttribute('href');
    const report = await apiJson(base, href);
    check('report export has original and corrected parts',
      report.original_detections.provenance === 'original_detector_output'
      && report.corrected_pins.provenance === 'reviewed_corrections'
      && report.original_detections.detections.length === 14);

    // 9. Stale scan responses must not replace the current view.
    // Slow scan on page 1, switch to page 2 and scan there quickly.
    let release;
    const gate = new Promise((r) => { release = r; });
    let slowed = 0;
    await page.route('**/api/scans', async (route) => {
      if (route.request().method() === 'POST' && slowed++ === 0) {
        await gate;
      }
      await route.continue();
    });
    await setMode(page, 'template');
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [sx, sy]);
    await settle(page);
    const a3 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [want.x + 0.25, want.y + 0.25]);
    const b3 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [want.x + 43.75, want.y + 43.75]);
    await page.mouse.move(o.x + a3.x, o.y + a3.y);
    await page.mouse.down();
    await page.mouse.move(o.x + b3.x, o.y + b3.y, { steps: 4 });
    await page.mouse.up();
    await settle(page);
    await page.locator('#scan-btn').click(); // slow, held at the gate (confirm dialog auto-accepted)
    await settle(page);
    await page.locator('#pages button').nth(1).click(); // rotated page, 2200x1700
    await waitIdle(page);
    const f2 = await page.evaluate(() => window.__pinny.frame());
    check('rotated page (/Rotate 90) frame is 2200x1700', f2.width === 2200 && f2.height === 1700);
    const sym2 = JSON.parse(execFileSync('python3', ['-c', `
import json
from pinny.viewer.render_stub import stub_symbol_centres
print(json.dumps(stub_symbol_centres(${JSON.stringify(doc.document_version)}, 1, 2200, 1700)))`], { cwd: REPO }).toString());
    const [tx, ty] = sym2.find((s) => s[2] === 0);
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [tx, ty]);
    await settle(page);
    const a4 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [tx - 21.75, ty - 21.75]);
    const b4 = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [tx + 21.75, ty + 21.75]);
    await page.mouse.move(o.x + a4.x, o.y + a4.y);
    await page.mouse.down();
    await page.mouse.move(o.x + b4.x, o.y + b4.y, { steps: 4 });
    await page.mouse.up();
    await settle(page);
    await page.locator('#scan-btn').click();
    await waitFor(page, () => window.__pinny.pins().length > 0);
    const page2Scan = await page.evaluate(() => window.__pinny.scanId());
    release(); // now let page 1's slow scan finish
    await page.waitForTimeout(2500);
    await settle(page);
    const nowScan = await page.evaluate(() => window.__pinny.scanId());
    const nowPage = await page.evaluate(() => window.__pinny.page());
    const pins2 = await page.evaluate(() => window.__pinny.pins());
    check('stale scan response from page 1 did not replace page 2',
      nowScan === page2Scan && nowPage === 1 && pins2.every((p) => sym2.some(([x, y]) => x === p.x && y === p.y)));
    const p1scans = (await apiJson(base, `/api/documents/${encodeURIComponent(doc.document_version)}/pages/0/scans`)).scans;
    check('slow page-1 scan was still saved in page 1\'s scan list', p1scans.length === 2);
    await page.unroute('**/api/scans');
    await markerAlignment(page, `dpr ${dpr} rotated page`, [0.5, 1, 4], [0, 90, 180, 270]);

    // Stale scan load: pick an old scan (slow), then the new one (fast).
    await page.locator('#pages button').nth(0).click();
    await waitIdle(page);
    const ids = p1scans.map((s) => s.scan_id);
    let release2;
    const gate2 = new Promise((r) => { release2 = r; });
    await page.route(`**/api/scans/${ids[0]}`, async (route) => { await gate2; await route.continue(); });
    await page.locator('#scan-select').selectOption(ids[0]);
    await page.locator('#scan-select').selectOption(ids[1]);
    await waitFor(page, (id) => window.__pinny.scanId() === id && window.__pinny.pins().length > 0, ids[1]);
    release2();
    await page.waitForTimeout(500);
    const shown = await page.evaluate(() => ({ id: window.__pinny.scanId(), n: window.__pinny.pins().length }));
    check('stale scan-load response did not replace the selected scan', shown.id === ids[1] && shown.n === 14,
      JSON.stringify(shown));
    await page.unroute(`**/api/scans/${ids[0]}`);

    // Upload error readable.
    const bad = join(work, 'notes.pdf');
    writeFileSync(bad, 'this is not a pdf');
    await page.locator('#file').setInputFiles(bad);
    await waitFor(page, () => /Upload failed/.test(document.getElementById('doc-status').textContent));
    check('upload error is shown in plain words', /not a PDF/.test(await page.locator('#doc-status').textContent()));

    await context.close();
    } finally {
      proc.kill();
    }
}

try {
  for (const dpr of [1, 2]) await pass(dpr);
} finally {
  await browser.close();
  rmSync(work, { recursive: true, force: true });
}

console.log(`\n${results.length - failures}/${results.length} checks passed`);
process.exit(failures ? 1 : 0);
