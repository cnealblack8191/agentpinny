// Browser check of the training pages (docs/training-site.md section 3).
//
//   node tests/viewer/test_training_browser.mjs
//
// Serves the real site in production mode with small training data
// (tests/viewer/training_site_server.py): one scanned drawing, a synthetic
// dataset, two models with fake benchmark results (the gate recommends one)
// and a training job a fake worker keeps running. Checks every page as an
// admin, the Label queue link into the viewer and "mark page fully
// reviewed", a real dataset build, progress and cancel on Training runs,
// the promote and deactivate flow, and that a reviewer sees no admin
// actions. The pages must run under the CSP without errors.

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
const work = mkdtempSync(join(tmpdir(), 'pinny-training-'));

function startServer() {
  return new Promise((res, rej) => {
    const proc = spawn(PY, ['tests/viewer/training_site_server.py', work], { cwd: REPO });
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
async function waitFor(page, fn, arg, timeout = 30000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
  await settle(page);
}
const loaded = (page, name) => waitFor(page, (n) => !!document.body && document.body.dataset.loaded === n, name);

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
let server = null;
try {
  server = await startServer();
  const cspErrors = [];

  // The one place this test signs people in: the one-time set-password link.
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
  const api = (page, method, path, json) => page.evaluate(async ([m, p, j]) => {
    const r = await fetch(p, { method: m, headers: j ? { 'Content-Type': 'application/json' } : {},
      body: j ? JSON.stringify(j) : undefined });
    return { status: r.status, body: await r.json() };
  }, [method, path, json]);

  // ------------------------------------------------------------ admin
  const { page } = await signIn(ADMIN);
  check('the viewer links to the training pages',
    (await page.locator('.site-nav a[href="training.html#dashboard"]').count()) === 1);
  await page.locator('.site-nav a[href="training.html#dashboard"]').click();
  await loaded(page, 'dashboard');
  const tiles = await page.locator('#dash-tiles').textContent();
  check('dashboard: label counts', /2\s*positive labels/.test(tiles) && /3\s*pins not reviewed yet/.test(tiles), tiles);
  check('dashboard: readiness for both models', (await page.locator('[data-readiness]').count()) === 2);
  check('admin sees Datasets and Training runs', await page.locator('[data-nav=datasets]').isVisible()
    && await page.locator('[data-nav=runs]').isVisible());
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'training-dashboard.png') });

  // Label queue -> viewer on the most uncertain pin.
  await page.locator('[data-nav=queue]').click();
  await loaded(page, 'queue');
  check('queue: the scanned page needs review', (await page.locator('#queue-pages tbody tr').count()) === 1
    && (await page.locator('#queue-pages tbody').textContent()).includes('needs review'));
  check('queue: most uncertain pins listed', (await page.locator('#queue-pins tbody tr').count()) === 3);
  check('queue: mark is disabled while pins are unreviewed',
    await page.locator('#queue-pages button[data-mark]').isDisabled());
  const href = await page.locator('#queue-pages a.review-link').getAttribute('href');
  const pinId = new URLSearchParams(href.split('#')[1]).get('pin');
  await page.locator('#queue-pages a.review-link').click();
  await waitFor(page, () => window.__pinny && window.__pinny.frame() !== null && window.__pinny.selected() !== null);
  check('Review opens the viewer on that pin', (await page.evaluate(() => window.__pinny.selected())) === pinId
    && (await page.evaluate(() => window.__pinny.scanId())) === server.scan_id);
  // Review the rest (through the API, as the viewer would), then mark in the viewer.
  for (const pin of server.pins.slice(2)) {
    const r = await api(page, 'POST', `/api/scans/${server.scan_id}/actions`,
      { action: 'reject', pin_id: pin, request_id: crypto.randomUUID() });
    if (r.status !== 200) check('review a pin', false, JSON.stringify(r));
  }
  await page.locator('#mark-reviewed-btn').click();
  await waitFor(page, () => document.getElementById('mark-reviewed-status').textContent.length > 0);
  check('viewer: mark page fully reviewed', (await page.locator('#mark-reviewed-status').textContent())
    .includes('marked fully reviewed'), await page.locator('#mark-reviewed-status').textContent());
  await page.goto(server.base + '/training.html#queue');
  await loaded(page, 'queue');
  check('queue: a fully reviewed page leaves the queue', await page.locator('#queue-empty').isVisible());

  // Datasets: list, then a real build in the in-process runner.
  await page.locator('[data-nav=datasets]').click();
  await loaded(page, 'datasets');
  check('datasets: the synthetic dataset is listed',
    (await page.locator(`#datasets-table tr[data-dataset="${server.dataset_id}"]`).textContent()).includes('synthetic'));
  await page.locator('#build-dataset-btn').click();
  await waitFor(page, () => document.getElementById('datasets-table').tBodies[0].rows.length === 2, null, 60000);
  check('datasets: building one from the reviews adds it', true);
  check('datasets: train buttons for admins', (await page.locator('button[data-train=verifier]').count()) === 2);

  // Training runs: progress and log grow by polling; cancel stops the run.
  await page.locator('[data-nav=runs]').click();
  await loaded(page, 'runs');
  check('runs: the nightly-shutdown note is shown', (await page.locator('#runs-shutdown-note').textContent())
    .includes('7 PM Eastern'));
  const kinds = await page.locator('#runs-table tbody').textContent();
  check('runs: the dataset build and the training job are listed',
    kinds.includes('Build dataset') && kinds.includes('Train verifier'));
  await page.locator(`#runs-table tr[data-job="${server.running_job}"]`).click();
  await waitFor(page, () => document.getElementById('run-log').textContent.includes('epoch'));
  const before = await page.locator('#run-progress').evaluate((e) => e.value);
  await waitFor(page, (b) => document.getElementById('run-progress').value > b, before, 15000);
  check('runs: progress advances by polling', (await page.locator('#run-progress').evaluate((e) => e.value)) > before);
  check('runs: the log tail follows', (await page.locator('#run-log').textContent()).split('\n').length > 2);
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'training-runs.png') });
  await page.locator('#run-cancel-btn').click();
  await waitFor(page, () => document.getElementById('run-summary').textContent.startsWith('cancelled'), null, 15000);
  check('runs: cancel stops the run', (await page.locator('#run-summary').textContent()).startsWith('cancelled'));

  // Models: side by side; Promote only where the gate recommends it.
  await page.locator('[data-nav=models]').click();
  await loaded(page, 'models');
  const good = page.locator(`.model[data-model="${server.good_model}"]`);
  const weak = page.locator(`.model[data-model="${server.weak_model}"]`);
  const heads = await good.locator('table.compare thead th').allTextContents();
  check('models: baseline and candidate side by side', heads.some((t) => t.startsWith('Template baseline'))
    && heads.some((t) => t.startsWith('Candidate')), heads.join(' | '));
  check('models: gate verdicts shown', (await good.textContent()).includes('recommends promotion')
    && (await weak.textContent()).includes('does not recommend promotion'));
  check('models: Promote enabled only when the gate recommends it',
    !(await good.locator('button[data-promote]').isDisabled()) && await weak.locator('button[data-promote]').isDisabled());
  await good.locator('button[data-promote]').click();
  await waitFor(page, () => document.getElementById('models-status').textContent.includes('is now the active'));
  check('models: promoted', (await page.locator('[data-active=verifier]').textContent()).includes(server.good_model));
  check('models: the promoted model is marked active', (await good.textContent()).includes('active')
    && await good.locator('button[data-promote]').isDisabled());
  const weakHeads = await weak.locator('table.compare thead th').allTextContents();
  check('models: a candidate is compared with the active model', weakHeads.some((t) => t.startsWith('Active')),
    weakHeads.join(' | '));
  const audit = await api(page, 'GET', '/api/audit');
  check('promotion is audited', audit.body.events.some((e) => e.action === 'model_promoted'
    && e.target === server.good_model && e.detail.benchmark_job_id === server.good_job));
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'training-models.png') });

  // ---------------------------------------------------------- reviewer
  const rev = await signIn(REVIEWER);
  const rp = rev.page;
  await rp.goto(server.base + '/training.html#models');
  await loaded(rp, 'models');
  check('reviewer: no Datasets or Training runs links', !(await rp.locator('[data-nav=datasets]').isVisible())
    && !(await rp.locator('[data-nav=runs]').isVisible()));
  check('reviewer: models are listed', (await rp.locator('.model').count()) === 2);
  const visible = async (sel) => {
    for (const e of await rp.locator(sel).all()) if (await e.isVisible()) return true;
    return false;
  };
  check('reviewer: no Promote, Benchmark or Deactivate', !(await visible('button[data-promote]'))
    && !(await visible('button[data-benchmark]')) && !(await visible('button[data-deactivate]')));
  await rp.goto(server.base + '/training.html#runs');
  await loaded(rp, 'dashboard');
  check('reviewer: admin pages fall back to the dashboard', await rp.locator('#page-dashboard').isVisible()
    && !(await rp.locator('#page-runs').isVisible()));
  const refused = await api(rp, 'POST', '/api/training/promote', { benchmark_job_id: server.good_job });
  check('reviewer: the server refuses promotion too', refused.status === 403);
  await rp.goto(server.base + '/training.html#queue');
  await loaded(rp, 'queue');
  check('reviewer: sees the label queue', await rp.locator('#page-queue').isVisible());

  // ------------------------------------------------------- deactivate
  await page.reload();
  await loaded(page, 'models');
  await page.locator('button[data-deactivate=verifier]').click();
  await waitFor(page, () => document.getElementById('models-status').textContent.includes('No verifier is active'));
  check('models: deactivate', (await page.locator('[data-active=verifier]').textContent()).includes('None'));

  check('no CSP violations', cspErrors.length === 0, cspErrors.join(' | '));
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all checks passed');
process.exit(failures ? 1 : 0);
