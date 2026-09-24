// Browser check of the scan-mode selector (docs/phase2-integration.md).
//
//   node tests/viewer/test_modes_browser.mjs
//
// Runs the real viewer server with the fake P6 models from
// test_modes_fakes.py (no torch, no weights) and drives the UI in Chromium:
// the mode list shows which model is active, "model" mode hides the
// template-box step, the template modes keep it, pins show the verifier and
// template scores, and the Phase 1 keyboard shortcuts still work.

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

const work = mkdtempSync(join(tmpdir(), 'pinny-modes-'));
const pdfPath = join(work, 'plan.pdf');
const centres = JSON.parse(execFileSync(PY, ['-c', `
import json, sys; sys.path.insert(0, 'tests/viewer')
from pdfgen import make_pdf, receptacle_centres_px
open(${JSON.stringify(pdfPath)}, 'wb').write(make_pdf())
print(json.dumps(receptacle_centres_px()))`], { cwd: REPO }).toString());

// Serve with fake models; with promote=1 a verifier (rejecting centre 1)
// and a point detector (returning three points) are promoted first.
function startServer(dataDir, promote) {
  const code = `
import sys, json
from pinny.viewer import ViewerService
from pinny.viewer.server import make_server
from tests.viewer.test_modes_fakes import FAKE_CLASSES, make_model, promote
data = ${JSON.stringify(dataDir)}
c = json.loads(${JSON.stringify(JSON.stringify(centres))})
if ${promote ? 'True' : 'False'}:
    v = make_model(data, "verifier", threshold=0.5, fake={"reject": [c[1]], "low": 0.2, "high": 0.9})
    d = make_model(data, "detector", threshold=0.5,
                   fake={"points": [[c[0][0], c[0][1], 0.97], [c[2][0], c[2][1], 0.8], [c[3][0], c[3][1], 0.7]]})
    promote(data, v, data)
    promote(data, d, data)
svc = ViewerService(data, model_classes=FAKE_CLASSES)
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
async function waitFor(page, fn, arg, timeout = 20000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
  await settle(page);
}
const waitIdle = (page) => waitFor(page, () => window.__pinny && window.__pinny.idle());

async function openPage(browser, base) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  page.on('dialog', (d) => d.accept());
  page.on('pageerror', (e) => check('no page errors', false, e.message));
  await page.goto(base + '/');
  await page.locator('#file').setInputFiles(pdfPath);
  await waitIdle(page);
  await waitFor(page, () => window.__pinny.models() !== null);
  return page;
}

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

const optionState = (page) => page.evaluate(() => [...document.getElementById('scan-mode').options]
  .map((o) => ({ value: o.value, disabled: o.disabled, text: o.textContent })));

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const servers = [];
try {
  // ---------------------------------------------------- no active models
  {
    const dir = join(work, 'none');
    const s = await startServer(dir, false);
    servers.push(s);
    const page = await openPage(browser, s.base);
    const opts = await optionState(page);
    check('no models: only template mode is selectable',
      !opts[0].disabled && opts[1].disabled && opts[2].disabled, JSON.stringify(opts));
    check('no models: options say no model is active', opts[1].text.includes('no active model'));
    check('no models: template step is shown', await page.locator('#template-step').isVisible());
    await dragTemplate(page, centres[0]);
    await page.locator('#scan-btn').click();
    await waitFor(page, () => window.__pinny.pins().length > 0);
    await waitIdle(page);
    const pins = await page.evaluate(() => window.__pinny.pins());
    check('template mode scans as in Phase 1', pins.length === centres.length && pins.every((p) => p.score >= 0.8),
      `${pins.length} pins`);
    const cell = await page.locator('#pin-table tbody tr td:nth-child(2)').first().textContent();
    check('template pins show one score', /^\d\.\d{3}$/.test(cell), cell);
    await page.close();
  }

  // --------------------------------------------------- with active models
  {
    const dir = join(work, 'models');
    const s = await startServer(dir, true);
    servers.push(s);
    const page = await openPage(browser, s.base);
    const models = await page.evaluate(() => window.__pinny.models());
    const opts = await optionState(page);
    check('models: every mode is selectable', opts.every((o) => !o.disabled), JSON.stringify(opts));
    check('models: options name the active models',
      opts[1].text.includes(models.active.verifier) && opts[2].text.includes(models.active.detector));

    // Model mode: no template step, T does nothing, scan without a template.
    await page.locator('#scan-mode').selectOption('model');
    await settle(page);
    check('model mode hides the template step', !(await page.locator('#template-step').isVisible()));
    check('model mode hides the Template box tool', !(await page.locator('#template-mode-label').isVisible()));
    check('model mode says which detector is active',
      (await page.locator('#mode-info').textContent()).includes(models.active.detector));
    check('model mode can scan with no template', !(await page.locator('#scan-btn').isDisabled()));
    await page.keyboard.press('t');
    await settle(page);
    check('T does not enter template mode in model mode',
      await page.locator('input[name=mode][value=pan]').isChecked());
    await page.locator('#scan-btn').click();
    await waitFor(page, () => window.__pinny.pins().length === 3);
    await waitIdle(page);
    let pins = await page.evaluate(() => window.__pinny.pins());
    check('model scan shows the detector points with 40 px boxes',
      pins.every((p) => p.box.width === 40 && p.rotation === 0) && pins[0].score === 0.97);
    if (SHOTS) await page.screenshot({ path: join(SHOTS, 'mode-model.png') });
    check('model scan counts line names the mode',
      (await page.locator('#counts').textContent()).includes('Mode: model'));

    // Shortcuts: N selects, A approves, X rejects.
    await page.locator('#viewport').focus();
    await page.keyboard.press('n');
    await settle(page);
    await page.keyboard.press('a');
    await waitIdle(page);
    await page.keyboard.press('n');
    await settle(page);
    await page.keyboard.press('x');
    await waitIdle(page);
    pins = await page.evaluate(() => window.__pinny.pins());
    const states = pins.map((p) => p.state).sort().join(',');
    check('N / A / X shortcuts review pins in model mode', states === 'approved,rejected,unreviewed', states);

    // Template + verifier: template step back, pins carry both scores.
    await page.locator('#scan-mode').selectOption('template+verifier');
    await settle(page);
    check('template+verifier shows the template step', await page.locator('#template-step').isVisible());
    check('template+verifier shows the Template box tool', await page.locator('#template-mode-label').isVisible());
    check('template+verifier needs a template first', await page.locator('#scan-btn').isDisabled());
    await dragTemplate(page, centres[0]);
    check('template+verifier can scan once a box is drawn', !(await page.locator('#scan-btn').isDisabled()));
    const before = await page.evaluate(() => window.__pinny.scanId());
    await page.locator('#scan-btn').click();
    await waitFor(page, (b) => window.__pinny.scanId() !== b && window.__pinny.pins().length > 0, before);
    await waitIdle(page);
    pins = await page.evaluate(() => window.__pinny.pins());
    check('verifier drops the rejected match', pins.length === centres.length - 1, `${pins.length} pins`);
    check('pins carry verifier and template scores',
      pins.every((p) => p.verifier_score === 0.9 && p.template_score >= 0.8 && p.score === 0.9));
    const cell = await page.locator('#pin-table tbody tr td:nth-child(2)').first().textContent();
    check('pin table shows both scores', /^v 0\.900 · t \d\.\d{3}$/.test(cell), cell);
    await page.locator('#pin-table tbody tr').first().click();
    await settle(page);
    const info = await page.locator('#pin-info').textContent();
    if (SHOTS) await page.screenshot({ path: join(SHOTS, 'mode-template-verifier.png') });
    check('pin info shows both scores', info.includes('verifier 0.900') && info.includes('template '), info);
    check('status reports the suppressed match',
      (await page.locator('#scan-status').textContent()).includes('1 suppressed'));
    check('scan list labels the mode',
      (await page.locator('#scan-select').textContent()).includes('template+verifier'));

    // The mode choice survives a reload (per-browser convenience).
    await page.reload();
    await waitIdle(page);
    await waitFor(page, () => window.__pinny.models() !== null);
    check('scan mode is remembered after a reload',
      (await page.evaluate(() => window.__pinny.scanMode())) === 'template+verifier');
    await page.close();
  }
} finally {
  await browser.close();
  for (const s of servers) s.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all checks passed');
process.exit(failures ? 1 : 0);
