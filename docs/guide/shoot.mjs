// Screenshots for the Pinny user guide, from the real site run by the test servers.
// node docs/guide/shoot.mjs docs/guide/shots   (needs Playwright + the dev install)
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const REPO = new URL('../..', import.meta.url).pathname;
const OUT = process.argv[2];
const PY = 'python3';
const ADMIN = 'boss@example.com';

function start(script) {
  const work = mkdtempSync(join(tmpdir(), 'pinny-guide-'));
  return new Promise((res, rej) => {
    const proc = spawn(PY, [script, work], { cwd: REPO });
    let out = '';
    proc.stdout.on('data', (d) => {
      out += d;
      const line = out.split('\n').find((l) => l.startsWith('{'));
      if (line) res({ proc, ...JSON.parse(line) });
    });
    proc.stderr.on('data', (d) => process.stderr.write(d));
    proc.on('exit', (c) => rej(new Error('server exited ' + c + out)));
  });
}
const settle = (page, ms = 60) => page.evaluate((t) => new Promise((r) =>
  requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, t)))), ms);
const waitFor = async (page, fn, arg, timeout = 30000) => {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
  await settle(page);
};
const idle = (page) => waitFor(page, () => window.__pinny && window.__pinny.idle());
const loaded = (page, n) => waitFor(page, (x) => document.body && document.body.dataset.loaded === x, n);
const shot = (page, name, opts = {}) => page.screenshot({ path: join(OUT, name + '.png'), ...opts });

const browser = await chromium.launch();
const procs = [];
try {
  // ------------------------------------------------ training site server
  const s = await start('tests/viewer/training_site_server.py');
  procs.push(s.proc);
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 });
  const page = await ctx.newPage();
  page.on('dialog', (d) => d.accept());

  // Set-password page, then sign in.
  await page.goto(`${s.base}/setup.html#token=${s.links[ADMIN]}`);
  await waitFor(page, () => !document.getElementById('setup-form').hidden);
  await page.locator('#password').fill('a long guide password');
  await page.locator('#password2').fill('a long guide password');
  await shot(page, '01-set-password', { clip: { x: 0, y: 0, width: 1440, height: 520 } });
  await page.locator('#setup-form button').click();
  await page.waitForURL(s.base + '/');
  const anon = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 })).newPage();
  await anon.goto(`${s.base}/login.html`);
  await settle(anon, 300);
  await shot(anon, '02-sign-in', { clip: { x: 0, y: 0, width: 1440, height: 520 } });

  // Viewer on the scanned page.
  await page.goto(`${s.base}/#v=${encodeURIComponent(s.document_version)}&p=0&s=${s.scan_id}`);
  await page.reload();
  await idle(page);
  await waitFor(page, () => window.__pinny.pins().length > 0);
  await settle(page, 400);
  await shot(page, '03-viewer-scanned');

  // Template box (draw one around the first receptacle).
  const centres = JSON.parse(execFileSync(PY, ['-c', `
import json, sys; sys.path.insert(0, 'tests/viewer')
from pdfgen import receptacle_centres_px
print(json.dumps(receptacle_centres_px()))`], { cwd: REPO }).toString());
  const r = await page.evaluate(() => document.getElementById('canvas').getBoundingClientRect().toJSON());
  const [cx, cy] = centres[0];
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 2, 0), [cx, cy]);
  await settle(page);
  await page.locator('input[name=mode][value=template]').check();
  const scr = (x, y) => page.evaluate(([a, b]) => window.__pinny.toScreen(a, b), [x, y]);
  const a = await scr(Math.round(cx) - 22, Math.round(cy) - 22);
  const b = await scr(Math.round(cx) + 22, Math.round(cy) + 22);
  await page.mouse.move(r.x + a.x, r.y + a.y);
  await page.mouse.down();
  await page.mouse.move(r.x + b.x, r.y + b.y, { steps: 6 });
  await page.mouse.up();
  await settle(page, 200);
  await shot(page, '04-template-box');
  await page.locator('input[name=mode][value=pan]').check();

  // Pin popup: Correct / Wrong / Next.
  const pins = await page.evaluate(() => window.__pinny.pins());
  const target = pins.find((p) => p.state === 'unreviewed') || pins[0];
  await page.evaluate(([x, y]) => window.__pinny.lookAt(x, y, 1.6, 0), [target.x, target.y]);
  await settle(page);
  const t = await scr(target.x, target.y);
  await page.mouse.click(r.x + t.x, r.y + t.y);
  await settle(page, 200);
  await shot(page, '05-pin-popup');
  const pb = await page.locator('#pin-pop').boundingBox();
  await shot(page, '05b-pin-popup-close', { clip: { x: Math.max(0, pb.x - 160), y: Math.max(0, pb.y - 120),
    width: pb.width + 320, height: pb.height + 240 } });

  // + Missed button.
  await page.keyboard.press('Escape');
  await page.locator('#missed-fab').click();
  await settle(page, 200);
  await shot(page, '06-missed');
  await page.keyboard.press('Escape');
  await settle(page);

  // Training pages.
  for (const [hash, name] of [['dashboard', '07-dashboard'], ['queue', '08-label-queue'],
    ['datasets', '10-datasets'], ['runs', '11-training-runs'], ['models', '12-models'], ['team', '13-team']]) {
    await page.goto(`${s.base}/training.html#${hash}`);
    await loaded(page, hash);
    await settle(page, 300);
    await shot(page, name, { fullPage: hash === 'models' });
  }

  // ---------------------------------------------- legend / symbol server
  const y = await start('tests/viewer/symbol_site_server.py');
  procs.push(y.proc);
  const c2 = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 });
  const p2 = await c2.newPage();
  p2.on('dialog', (d) => d.accept());
  await p2.goto(`${y.base}/setup.html#token=${y.links[ADMIN]}`);
  await waitFor(p2, () => !document.getElementById('setup-form').hidden);
  await p2.locator('#password').fill('a long guide password');
  await p2.locator('#password2').fill('a long guide password');
  await p2.locator('#setup-form button').click();
  await p2.waitForURL(y.base + '/');
  await p2.goto(`${y.base}/#v=${encodeURIComponent(y.document_version)}&p=0`);
  await p2.reload();
  await idle(p2);
  await settle(p2, 800);
  await shot(p2, '14-legend');
  await p2.goto(`${y.base}/training.html#symbols`);
  await loaded(p2, 'symbols');
  await settle(p2, 300);
  await shot(p2, '09-symbol-types');
  console.log('done');
} finally {
  await browser.close();
  for (const p of procs) p.kill();
}
