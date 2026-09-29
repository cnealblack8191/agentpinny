// Browser check of the Symbol types page (docs/set-scanning.md "Learning each
// symbol type").
//
//   node tests/viewer/test_symbols_browser.mjs
//
// Serves the real site in production mode (tests/viewer/symbol_site_server.py)
// with a drawing set scanned from its legend and two sheets reviewed. As an
// admin: the page lists each reviewed tag with its counts, Train runs the job
// and the row shows the learning switched on with its check, Switch off /
// Switch on work. A reviewer sees the page without the admin buttons. The
// page must run under the CSP without errors.

import { spawn } from 'node:child_process';
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

const ADMIN = 'boss@example.com';
const REVIEWER = 'assistant@example.com';
const work = mkdtempSync(join(tmpdir(), 'pinny-symbols-'));

function startServer() {
  return new Promise((res, rej) => {
    const proc = spawn(PY, ['tests/viewer/symbol_site_server.py', work], { cwd: REPO });
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

const settle = (page) => page.evaluate(() => new Promise((r) =>
  requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 30)))));
async function waitFor(page, fn, arg, timeout = 60000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 100 });
  await settle(page);
}
const loaded = (page, name) => waitFor(page, (n) => !!document.body && document.body.dataset.loaded === n, name);

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
let server = null;
try {
  server = await startServer();
  const cspErrors = [];
  async function signIn(email) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    page.on('dialog', (d) => d.accept());
    page.on('pageerror', (e) => check(`${email}: no page errors`, false, e.message));
    page.on('console', (m) => { if (/Content Security Policy/i.test(m.text())) cspErrors.push(m.text()); });
    await page.goto(`${server.base}/setup.html#token=${server.links[email]}`);
    await waitFor(page, () => !document.getElementById('setup-form').hidden);
    await page.locator('#password').fill('a long test password');
    await page.locator('#password2').fill('a long test password');
    await page.locator('#setup-form button').click();
    await page.waitForURL(server.base + '/');
    return { ctx, page };
  }

  const { page } = await signIn(ADMIN);
  await page.goto(`${server.base}/training.html#symbols`);
  await loaded(page, 'symbols');
  const row = page.locator('#symbols-table tbody tr[data-tag="D"]');
  check('the reviewed tag is listed', (await row.count()) === 1);
  const cells = await row.locator('td').allTextContents();
  check('its counts are shown', cells[1] === String(2 * server.real) && cells[2] === String(2 * server.crossed)
    && cells[4] === '2', JSON.stringify(cells));
  check('not trained yet', cells[5].includes('Not trained yet'), cells[5]);
  check('admin sees Train', await row.locator('button[data-train]').isVisible());
  check('Switch is hidden before training', await row.locator('button[data-switch]').isHidden());

  await row.locator('button[data-train]').click();
  await waitFor(page, () => {
    const r = document.querySelector('#symbols-table tbody tr[data-tag="D"]');
    return r && /\bon\b/.test(r.children[5].textContent) && /caught/.test(r.children[5].textContent);
  });
  const learning = await row.locator('td').nth(5).textContent();
  check('after Train the learning is on, with its check', /caught \d+ of \d+ wrong/.test(learning)
    && learning.includes(`kept ${2 * server.real} of ${2 * server.real} correct`), learning);
  check('Train now reads Retrain', (await row.locator('button[data-train]').textContent()) === 'Retrain');
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'symbol-types.png') });

  await row.locator('button[data-switch]').click();
  await waitFor(page, () => /learning is off/.test(document.getElementById('symbols-status').textContent));
  check('Switch off turns it off', /\boff\b/.test(await row.locator('td').nth(5).textContent())
    && (await row.locator('button[data-switch]').textContent()) === 'Switch on');
  await row.locator('button[data-switch]').click();
  await waitFor(page, () => /learning is on/.test(document.getElementById('symbols-status').textContent));
  check('Switch on turns it back on', (await row.locator('button[data-switch]').textContent()) === 'Switch off');

  // A reviewer sees the page, without the admin buttons.
  const r = await signIn(REVIEWER);
  await r.page.goto(`${server.base}/training.html#symbols`);
  await loaded(r.page, 'symbols');
  const rrow = r.page.locator('#symbols-table tbody tr[data-tag="D"]');
  check('a reviewer sees the symbol types', (await rrow.count()) === 1);
  check('a reviewer sees no Train or Switch buttons', await rrow.locator('button[data-train]').isHidden()
    && await rrow.locator('button[data-switch]').isHidden() && await r.page.locator('#train-all-btn').isHidden());
  check('no CSP errors', cspErrors.length === 0, cspErrors.join(' | '));
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all symbol-type checks passed');
process.exit(failures ? 1 : 0);
