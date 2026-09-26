// Browser check of batch scans (contracts §5a, docs/viewer.md "Batch scans").
//
//   node tests/viewer/test_batch_browser.mjs
//
// Runs the real viewer server on a synthetic 5-page drawing set (receptacles
// on pages 1, 2 and 4, a blank page 3 and a page 5 smaller than the
// template) and drives the UI in Chromium: draw the template once, scan all
// pages, watch progress, walk the cross-page review list with B and A, retry
// the failed page, reload, and scan a listed subset of pages.

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
const SHOTS = process.env.PINNY_SCREENSHOT_DIR; // optional: save screenshots here
let failures = 0;
function check(name, ok, detail = '') {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}

const work = mkdtempSync(join(tmpdir(), 'pinny-batch-'));
const pdfPath = join(work, 'set.pdf');
const COUNTS = [5, 2, 0, 3]; // receptacles per page; page 5 is too small to scan
const centres = JSON.parse(execFileSync(PY, ['-c', `
import json, sys; sys.path.insert(0, 'tests/viewer')
from pdfgen import RECEPTACLES_PT, receptacle_centres_px
from tests.factory import PageSpec, build_pdf
specs = [PageSpec(receptacles=list(RECEPTACLES_PT[:n])) for n in ${JSON.stringify(COUNTS)}]
specs.append(PageSpec(width_pt=10, height_pt=10))
open(${JSON.stringify(pdfPath)}, 'wb').write(build_pdf(specs))
print(json.dumps(receptacle_centres_px()))`], { cwd: REPO }).toString());
const TOTAL = COUNTS.reduce((a, b) => a + b, 0);

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
const batchOf = (page) => page.evaluate(() => window.__pinny.batch());

async function dragTemplate(page, [cx, cy]) {
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [cx, cy]);
  await settle(page);
  await page.locator('input[name=mode][value=template]').check();
  await settle(page);
  const r = await page.evaluate(() => document.getElementById('canvas').getBoundingClientRect().toJSON());
  const box = { x: Math.round(cx) - 22, y: Math.round(cy) - 22, w: 44, h: 44 };
  const a = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [box.x + 0.25, box.y + 0.25]);
  const b = await page.evaluate(([x, y]) => window.__pinny.toScreen(x, y), [box.x + box.w - 0.25, box.y + box.h - 0.25]);
  await page.mouse.move(r.x + a.x, r.y + a.y);
  await page.mouse.down();
  await page.mouse.move(r.x + b.x, r.y + b.y, { steps: 5 });
  await page.mouse.up();
  await settle(page);
  await page.keyboard.press('v');
}

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
let server = null;
try {
  server = await startServer(join(work, 'data'));
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const dialogs = [];
  page.on('dialog', (d) => { dialogs.push(d.message()); d.accept(); });
  page.on('pageerror', (e) => check('no page errors', false, e.message));
  await page.goto(server.base + '/');
  await page.locator('#file').setInputFiles(pdfPath);
  await waitFor(page, () => document.querySelectorAll('#pages button').length === 5);
  check('batch button needs a template first', await page.locator('#batch-btn').isDisabled());
  await page.locator('#pages button').first().click();
  await waitIdle(page);
  check('no batch yet message', (await page.locator('#batch-status').textContent()).includes('No batch yet'));

  // -------------------------------------------- draw once, scan all pages
  await dragTemplate(page, centres[0]);
  check('batch button enabled once a box is drawn', !(await page.locator('#batch-btn').isDisabled()));
  check('batch button says all pages', (await page.locator('#batch-btn').textContent()) === 'Scan all pages');
  await page.locator('#batch-btn').click();
  await waitFor(page, () => window.__pinny.batch() !== null);
  check('start asks for confirmation naming the page count and template page',
    dialogs.some((m) => m.includes('Scan 5 page(s)') && m.includes('page 1')), dialogs.join(' | '));
  await waitFor(page, () => window.__pinny.batch().status === 'complete');
  await waitIdle(page);
  let b = await batchOf(page);
  check('every page was scanned; the tiny page failed',
    b.pages.map((p) => p.status).join(',') === 'done,done,done,done,failed', b.pages.map((p) => p.status).join(','));
  check('marks per page match the drawing', b.pages.slice(0, 4).map((p) => p.counts.total).join(',') === COUNTS.join(','));
  check('page on screen opened its batch scan', (await page.evaluate(() => window.__pinny.pins().length)) === COUNTS[0]);
  const status = await page.locator('#batch-status').textContent();
  check('progress line reports the result', status.includes('Finished: 4 of 5 page(s) scanned, 1 failed')
    && status.includes(`${TOTAL} of ${TOTAL} mark(s) still to review`), status);
  check('progress bar is full', await page.evaluate(() => {
    const p = document.getElementById('batch-progress');
    return !p.hidden && p.value === p.max && p.max === 5;
  }));
  const rows = await page.locator('#batch-table tbody tr').allTextContents();
  check('batch table lists each page', rows.length === 5 && rows[4].includes('failed:'), rows.join(' | '));
  const badges = await page.evaluate(() => [...document.querySelectorAll('#pages button')].map((x) => x.dataset.badge));
  check('page buttons show batch state', badges.join(',') === 'review,review,review,review,failed', badges.join(','));
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'batch-complete.png') });

  // ------------------------------------- walk the review list across pages
  const visited = new Set();
  let lastScore = -Infinity;
  let ordered = true;
  for (let k = 0; k < TOTAL; k++) {
    const before = await page.evaluate(() => [window.__pinny.scanId(), window.__pinny.selected()]);
    await page.locator('#viewport').focus();
    await page.keyboard.press('b');
    await waitFor(page, (bf) => {
      const s = [window.__pinny.scanId(), window.__pinny.selected()];
      return s[1] && (s[0] !== bf[0] || s[1] !== bf[1]) && window.__pinny.idle();
    }, before);
    const sel = await page.evaluate(() => {
      const p = window.__pinny.pins().find((x) => x.pin_id === window.__pinny.selected());
      return { page: window.__pinny.page(), score: p.score, state: p.state, x: p.x, y: p.y,
        screen: window.__pinny.toScreen(p.x, p.y) };
    });
    visited.add(sel.page);
    if (sel.score < lastScore - 1e-9) ordered = false;
    lastScore = sel.score;
    if (k === 0) {
      check('B selects an unreviewed mark and brings it into view', sel.state === 'unreviewed'
        && sel.screen.x > 0 && sel.screen.y > 0 && sel.screen.x < 1000 && sel.screen.y < 900, JSON.stringify(sel));
    }
    await page.keyboard.press('a');
    await waitIdle(page);
  }
  check('B visited every page with marks', [...visited].sort().join(',') === '0,1,3', [...visited].join(','));
  check('marks came most uncertain first', ordered);
  await page.keyboard.press('b');
  await waitFor(page, () => document.getElementById('batch-status').textContent.includes('Nothing left'));
  check('B says when nothing is left', true);
  await waitFor(page, () => window.__pinny.batch().pin_counts.approved === 10);
  const rows2 = await page.locator('#batch-table tbody tr').allTextContents();
  check('table review counts follow the saved reviews', rows2[0].includes('0 of 5'), rows2.join(' | '));
  const badges2 = await page.evaluate(() => [...document.querySelectorAll('#pages button')].map((x) => x.dataset.badge));
  check('reviewed pages turn green, the blank page still needs a look',
    badges2.join(',') === 'reviewed,reviewed,review,reviewed,failed', badges2.join(','));

  // -------------------------------------------------- retry, then reload
  await page.locator('#batch-retry-btn').click();
  await waitFor(page, () => window.__pinny.batch().pages[4].attempts === 2
    && window.__pinny.batch().status === 'complete');
  check('retry runs the failed page again', (await batchOf(page)).pages[4].status === 'failed');
  await page.locator('#batch-table tbody tr').nth(3).click();
  await waitFor(page, () => window.__pinny.page() === 3 && window.__pinny.pins().length === 3);
  check('clicking a table row opens that page and its batch scan', true);
  const batchId = (await batchOf(page)).batch_id;
  await page.reload();
  await waitFor(page, (id) => window.__pinny.batch() && window.__pinny.batch().batch_id === id, batchId);
  check('the batch is shown again after a reload', true);

  // ---------------------------------------------- scan a subset of pages
  await page.locator('#pages button').first().click();
  await waitIdle(page);
  await dragTemplate(page, centres[0]);
  await page.locator('#batch-pages').fill('2, 4');
  await settle(page);
  check('button says listed pages', (await page.locator('#batch-btn').textContent()) === 'Scan listed pages');
  await page.locator('#batch-btn').click();
  await waitFor(page, (id) => window.__pinny.batch().batch_id !== id
    && window.__pinny.batch().status === 'complete', batchId);
  b = await batchOf(page);
  check('only the listed pages were scanned', b.pages.map((p) => p.page_index).join(',') === '1,3'
    && b.pin_counts.total === COUNTS[1] + COUNTS[3]);
  check('the batch list offers both batches', (await page.locator('#batch-select option').count()) === 3);
  await page.locator('#batch-pages').fill('9');
  await page.locator('#batch-btn').click();
  await settle(page);
  check('a bad page list is explained', (await page.locator('#scan-status').textContent()).includes('5 page(s)'));
  await page.close();
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all checks passed');
process.exit(failures ? 1 : 0);
