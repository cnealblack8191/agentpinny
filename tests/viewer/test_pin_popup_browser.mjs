// Browser check of the review popup beside the selected pin (docs/viewer.md).
//
//   node tests/viewer/test_pin_popup_browser.mjs
//
// Runs the real viewer server, scans a page, and checks that selecting a pin
// shows Correct / Wrong / Next next to it, that they review the pin (by
// mouse and by finger) and are saved, that pressing them does not pan or
// deselect, and that the popup hides while panning and follows the pin.

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

const work = mkdtempSync(join(tmpdir(), 'pinny-pinpop-'));
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
  const f = new Fingers(await context.newCDPSession(page), page);
  const r = await page.evaluate(() => document.getElementById('canvas').getBoundingClientRect().toJSON());
  const screenOf = (x, y) => page.evaluate(([a, b]) => window.__pinny.toScreen(a, b), [x, y]);
  const pop = page.locator('#pin-pop');
  const pinById = (id) => page.evaluate((i) => window.__pinny.pins().find((p) => p.pin_id === i), id);

  // Template box around the first receptacle, then scan.
  const [cx, cy] = centres[0];
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [cx, cy]);
  await settle(page);
  await page.locator('input[name=mode][value=template]').check();
  const a = await screenOf(Math.round(cx) - 22 + 0.25, Math.round(cy) - 22 + 0.25);
  const b = await screenOf(Math.round(cx) + 22 - 0.25, Math.round(cy) + 22 - 0.25);
  await page.mouse.move(r.x + a.x, r.y + a.y);
  await page.mouse.down();
  await page.mouse.move(r.x + b.x, r.y + b.y, { steps: 5 });
  await page.mouse.up();
  await page.locator('input[name=mode][value=pan]').check();
  await page.locator('#scan-btn').click();
  await waitFor(page, () => window.__pinny.pins().length > 0);
  await waitIdle(page);
  const pins = await page.evaluate(() => window.__pinny.pins());
  check('scan found the receptacles', pins.length === centres.length, `${pins.length} pins`);
  check('no popup before a pin is selected', await pop.isHidden());

  // Click a pin: the popup appears beside it.
  const clickPin = async (p) => {
    await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [p.x, p.y]);
    await settle(page);
    const s = await screenOf(p.x, p.y);
    await page.mouse.click(r.x + s.x, r.y + s.y);
    await settle(page);
    return s;
  };
  let s = await clickPin(pins[0]);
  check('selecting a pin shows the popup', await pop.isVisible());
  let box = await pop.boundingBox();
  check('the popup sits next to the pin',
    box && box.x > r.x + s.x && box.x - (r.x + s.x) < 30 && box.y < r.y + s.y && box.y + box.height > r.y + s.y,
    JSON.stringify(box));
  const view0 = await page.evaluate(() => window.__pinny.view());

  // Correct (mouse): approved and saved; view and selection unchanged.
  await page.locator('#pin-pop-ok').click();
  await waitIdle(page);
  let p0 = await pinById(pins[0].pin_id);
  check('Correct approves the pin', p0.state === 'approved' && !p0.pending, p0.state);
  check('pressing Correct does not pan or deselect',
    JSON.stringify(await page.evaluate(() => window.__pinny.view())) === JSON.stringify(view0)
    && (await page.evaluate(() => window.__pinny.selected())) === pins[0].pin_id);
  check('the popup shows the pin is approved', (await page.locator('#pin-pop-state').textContent()) === 'Approved'
    && await page.locator('#pin-pop-ok').isDisabled());
  const keyOk = await page.evaluate(() => document.activeElement && document.activeElement.id === 'viewport');
  check('keyboard focus goes back to the page view', keyOk);

  // Wrong (finger tap): rejected and saved.
  s = await clickPin(pins[1]);
  box = await pop.locator('#pin-pop-wrong').boundingBox();
  await f.tap(box.x + box.width / 2, box.y + box.height / 2);
  await settle(page);
  await waitIdle(page);
  const p1 = await pinById(pins[1].pin_id);
  check('Wrong (tapped with a finger) rejects the pin', p1.state === 'rejected' && !p1.pending, p1.state);
  check('after Wrong the popup shows Rejected and offers Correct as an undo',
    (await page.locator('#pin-pop-state').textContent()) === 'Rejected'
    && await page.locator('#pin-pop-wrong').isDisabled() && await page.locator('#pin-pop-ok').isEnabled());

  // Next: selects the next unreviewed pin, and the popup follows it.
  await clickPin(pins[2]);
  await page.locator('#pin-pop-next').click();
  await settle(page);
  const selId = await page.evaluate(() => window.__pinny.selected());
  const sel = await pinById(selId);
  check('Next selects another unreviewed pin', !!sel && selId !== pins[2].pin_id && sel.state === 'unreviewed');
  s = await screenOf(sel.x, sel.y);
  box = await pop.boundingBox();
  check('the popup follows the newly selected pin',
    box && Math.abs(box.y + box.height / 2 - (r.y + s.y)) < 3, JSON.stringify(box));

  // Hidden while panning; back after.
  await page.mouse.move(r.x + 100, r.y + 100);
  await page.mouse.down();
  await page.mouse.move(r.x + 160, r.y + 130, { steps: 3 });
  check('the popup hides while panning', await pop.isHidden());
  await page.mouse.up();
  await settle(page);
  check('the popup is back after the pan', await pop.isVisible());

  // A manual pin offers Delete, not Correct.
  await page.locator('input[name=mode][value=add]').check();
  const fr = await page.evaluate(() => window.__pinny.frame());
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [fr.width / 2, fr.height / 2]);
  await settle(page);
  await page.mouse.click(r.x + r.width / 2, r.y + r.height / 2);
  await waitIdle(page);
  check('a manual pin offers Delete and no Correct',
    (await page.locator('#pin-pop-wrong').textContent()).includes('Delete') && await page.locator('#pin-pop-ok').isHidden());

  // Esc deselects and hides the popup.
  await page.keyboard.press('Escape');
  await settle(page);
  check('Esc hides the popup', await pop.isHidden());

  // + Missed: from a pin's popup, one click marks a device the scan did not
  // find, then the viewer is back in Pan with the new pin selected.
  await page.locator('input[name=mode][value=pan]').check();
  const before = (await page.evaluate(() => window.__pinny.pins())).length;
  await clickPin(pins[3]);
  check('the popup offers + Missed', await page.locator('#pin-pop-missed').isEnabled());
  await page.locator('#pin-pop-missed').click();
  await settle(page);
  check('+ Missed switches to marking', await page.locator('input[name=mode][value=add]').isChecked()
    && (await page.locator('#scan-status').textContent()).includes('Click the device the scan missed'),
    `${await page.locator('input[name=mode]:checked').getAttribute('value')} | ${await page.locator('#scan-status').textContent()}`);
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1, 0), [fr.width / 2 + 60, fr.height / 2 + 40]);
  await settle(page);
  await page.mouse.click(r.x + r.width / 2, r.y + r.height / 2);
  await waitIdle(page);
  const after = await page.evaluate(() => window.__pinny.pins());
  const selNow = await page.evaluate(() => window.__pinny.selected());
  const newPin = after.find((q) => q.pin_id === selNow);
  check('one missed device was marked', after.length === before + 1 && !!newPin && newPin.origin === 'manual'
    && newPin.state === 'added', `${before} -> ${after.length}`);
  check('after marking, the viewer is back in Pan', await page.locator('input[name=mode][value=pan]').isChecked());
  check('the popup calls it a missed device', (await page.locator('#pin-pop-state').textContent()).startsWith('Missed'));

  // M with Esc: cancels without adding anything.
  await page.keyboard.press('Escape');
  await page.keyboard.press('m');
  await settle(page);
  check('M starts marking a missed device', await page.locator('input[name=mode][value=add]').isChecked()
    && await page.locator('#missed-btn').evaluate((b) => b.classList.contains('active')));
  await page.keyboard.press('Escape');
  await settle(page);
  check('Esc cancels marking', await page.locator('input[name=mode][value=pan]').isChecked()
    && (await page.evaluate(() => window.__pinny.pins())).length === before + 1);
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all popup checks passed');
process.exit(failures ? 1 : 0);
