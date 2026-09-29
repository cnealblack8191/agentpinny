// Browser check of touch navigation (docs/viewer.md "Using it").
//
//   node tests/viewer/test_touch_browser.mjs
//
// Runs the real viewer server and sends real touch events to Chromium via
// the DevTools protocol: pinch zoom about the fingers' midpoint, two-finger
// pan in Template box mode (which must not draw a box), a one-finger
// template box, one-finger pan, double-tap zoom, finger-sized pin taps, and
// that the browser page itself never zooms.

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
let failures = 0;
function check(name, ok, detail = '') {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}
const near = (a, b, tol) => Math.abs(a - b) <= tol;

const work = mkdtempSync(join(tmpdir(), 'pinny-touch-'));
const pdfPath = join(work, 'plan.pdf');
const centres = JSON.parse(execFileSync(PY, ['-c', `
import json, sys; sys.path.insert(0, 'tests/viewer')
from pdfgen import make_pdf, receptacle_centres_px
open(${JSON.stringify(pdfPath)}, 'wb').write(make_pdf())
print(json.dumps(receptacle_centres_px()))`], { cwd: REPO }).toString());

function startServer(dataDir) {
  const code = `
from pinny.viewer import ViewerService
from pinny.viewer.server import make_server
httpd = make_server(ViewerService(${JSON.stringify(dataDir)}), port=0)
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
async function waitFor(page, fn, arg, timeout = 20000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
  await settle(page);
}
const waitIdle = (page) => waitFor(page, () => window.__pinny && window.__pinny.idle());

// Touch gestures in page (CSS) coordinates. Each step lists every finger
// still down, as the protocol requires.
class Fingers {
  constructor(cdp, page) { this.cdp = cdp; this.page = page; }
  async send(type, points) {
    await this.cdp.send('Input.dispatchTouchEvent', { type, touchPoints: points.map(([x, y], id) => ({ x, y, id })) });
  }
  // Move fingers from `from` to `to` (arrays of [x, y]) in `steps`.
  async gesture(from, to, steps = 8) {
    await this.send('touchStart', from.slice(0, 1));
    if (from.length > 1) await this.send('touchStart', from);
    for (let k = 1; k <= steps; k++) {
      await this.send('touchMove', from.map(([x, y], i) => [x + (to[i][0] - x) * k / steps, y + (to[i][1] - y) * k / steps]));
    }
    await this.send('touchEnd', []);
    await settle(this.page);
  }
  async tap(x, y) {
    await this.send('touchStart', [[x, y]]);
    await this.send('touchEnd', []);
  }
}

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
let server;
try {
  server = await startServer(join(work, 'data'));
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 }, hasTouch: true });
  const page = await context.newPage();
  page.on('dialog', (d) => d.accept());
  page.on('pageerror', (e) => check('no page errors', false, e.message));
  await page.goto(server.base + '/');
  await page.locator('#file').setInputFiles(pdfPath);
  await waitIdle(page);
  const cdp = await context.newCDPSession(page);
  const f = new Fingers(cdp, page);
  const r = await page.evaluate(() => document.getElementById('canvas').getBoundingClientRect().toJSON());
  const toPage = (s) => [r.x + s.x, r.y + s.y];
  const view = () => page.evaluate(() => window.__pinny.view());
  const screenOf = (x, y) => page.evaluate(([a, b]) => window.__pinny.toScreen(a, b), [x, y]);
  const [cx, cy] = centres[0];

  // Pinch: spread two fingers to twice their spacing about a fixed midpoint.
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 0.5, 0), [cx, cy]);
  await settle(page);
  const mid = { x: r.width / 2, y: r.height / 2 };
  const before = await view();
  const under = await page.evaluate(([sx, sy]) => {
    const v = window.__pinny.view();
    return { x: (sx - v.panX) / v.zoom, y: (sy - v.panY) / v.zoom };
  }, [mid.x, mid.y]);
  const p = (dx) => [r.x + mid.x + dx, r.y + mid.y];
  await f.gesture([p(-50), p(50)], [p(-100), p(100)]);
  let v = await view();
  check('pinch out doubles the zoom', near(v.zoom / before.zoom, 2, 0.02), `${before.zoom} -> ${v.zoom}`);
  const s = await screenOf(under.x, under.y);
  check('pinch keeps the point between the fingers in place', near(s.x, mid.x, 2) && near(s.y, mid.y, 2),
    JSON.stringify(s));
  await f.gesture([p(-100), p(100)], [p(-50), p(50)]);
  v = await view();
  check('pinch in zooms back out', near(v.zoom, before.zoom, before.zoom * 0.02), `${v.zoom}`);
  check('the browser page itself did not zoom',
    await page.evaluate(() => !window.visualViewport || window.visualViewport.scale === 1));

  // Two-finger pan in Template box mode moves the view and draws no box.
  await page.locator('input[name=mode][value=template]').check();
  await settle(page);
  v = await view();
  await f.gesture([p(-60), p(60)], [[p(-60)[0] + 80, p(-60)[1] + 40], [p(60)[0] + 80, p(60)[1] + 40]]);
  let v2 = await view();
  check('two fingers pan in Template box mode', near(v2.panX - v.panX, 80, 2) && near(v2.panY - v.panY, 40, 2),
    `${v2.panX - v.panX}, ${v2.panY - v.panY}`);
  check('a two-finger pan draws no template box', (await page.evaluate(() => window.__pinny.template())) === null);

  // One finger in Template box mode draws the box around a receptacle.
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [cx, cy]);
  await settle(page);
  const a = await screenOf(Math.round(cx) - 22 + 0.25, Math.round(cy) - 22 + 0.25);
  const b = await screenOf(Math.round(cx) + 22 - 0.25, Math.round(cy) + 22 - 0.25);
  await f.gesture([toPage(a)], [toPage(b)]);
  const tpl = await page.evaluate(() => window.__pinny.template());
  check('one finger draws the template box', !!tpl && near(tpl.width, 44, 1) && near(tpl.height, 44, 1),
    JSON.stringify(tpl));

  // A second finger landing mid-box drops the box and pinches instead.
  const t0 = JSON.stringify(tpl);
  await f.send('touchStart', [toPage(a)]);
  await f.send('touchMove', [[toPage(a)[0] + 40, toPage(a)[1] + 40]]);
  await f.send('touchStart', [[toPage(a)[0] + 40, toPage(a)[1] + 40], [toPage(a)[0] + 140, toPage(a)[1] + 40]]);
  await f.send('touchMove', [[toPage(a)[0] + 20, toPage(a)[1] + 40], [toPage(a)[0] + 160, toPage(a)[1] + 40]]);
  await f.send('touchEnd', []);
  await settle(page);
  check('a second finger cancels the box being drawn',
    JSON.stringify(await page.evaluate(() => window.__pinny.template())) === t0);

  // Scan, then tap 15 px from a pin in Pan mode: a finger tap selects it.
  await page.locator('input[name=mode][value=pan]').check();
  await page.locator('#scan-btn').click();
  await waitFor(page, () => window.__pinny.pins().length > 0);
  await waitIdle(page);
  const pins = await page.evaluate(() => window.__pinny.pins());
  check('the touch-drawn template finds the receptacles', pins.length === centres.length, `${pins.length} pins`);
  const pin = pins[0];
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [pin.x, pin.y]);
  await settle(page);
  const ps = await screenOf(pin.x, pin.y);
  await f.tap(...toPage({ x: ps.x + 15, y: ps.y }));
  await settle(page);
  check('a finger tap 15 px from a pin selects it',
    (await page.evaluate(() => window.__pinny.selected())) === pin.pin_id);

  // One finger pans in Pan mode.
  v = await view();
  await f.gesture([[r.x + 200, r.y + 200]], [[r.x + 260, r.y + 150]]);
  v2 = await view();
  check('one finger pans in Pan mode', near(v2.panX - v.panX, 60, 1) && near(v2.panY - v.panY, -50, 1),
    `${v2.panX - v.panX}, ${v2.panY - v.panY}`);

  // Double tap on empty page zooms in 2x about the tap (far from any pin).
  const fr = await page.evaluate(() => window.__pinny.frame());
  let empty = null;
  for (let y = 100; !empty && y < fr.height; y += 150) for (let x = 100; !empty && x < fr.width; x += 150) {
    if (pins.every((q) => Math.hypot(q.x - x, q.y - y) > 200)) empty = [x, y];
  }
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 0.5, 0), empty);
  await settle(page);
  v = await view();
  const tapAt = { x: r.width / 2, y: r.height / 2 };
  await f.tap(...toPage(tapAt));
  await page.waitForTimeout(60);
  await f.tap(...toPage({ x: tapAt.x + 4, y: tapAt.y + 3 }));
  await settle(page);
  v2 = await view();
  check('a double tap zooms in 2x', near(v2.zoom / v.zoom, 2, 0.01), `${v.zoom} -> ${v2.zoom}`);
  await page.waitForTimeout(400);
  await f.tap(...toPage(tapAt));
  await page.waitForTimeout(400);
  await f.tap(...toPage(tapAt));
  await settle(page);
  check('two slow taps do not zoom', near((await view()).zoom, v2.zoom, 1e-9));
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all touch checks passed');
process.exit(failures ? 1 : 0);
