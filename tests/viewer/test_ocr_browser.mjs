// Browser check of the optional OCR (docs/ocr.md).
//
//   node tests/viewer/test_ocr_browser.mjs
//
// Needs Playwright and the Tesseract executable. Runs the development server
// (the local user is an admin) on an Arch D sheet whose title block is real
// PDF text. Checks: OCR starts off and "Read sheet info" is hidden; the admin
// turns it on; the button appears on the page; reading shows the sheet
// number, title and revision; the result is still there after a reload;
// turning OCR off hides the button again; and no page errors under the CSP.

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

const work = mkdtempSync(join(tmpdir(), 'pinny-ocr-'));
const pdfPath = join(work, 'sheet.pdf');
execFileSync(PY, ['tests/viewer/ocr_pdf.py', pdfPath], { cwd: REPO });

function startServer(dataDir) {
  const code = `
from pinny.viewer import ViewerService
from pinny.viewer.server import make_server
svc = ViewerService(${JSON.stringify(dataDir)})
httpd = make_server(svc, port=0)
print("http://127.0.0.1:%d/" % httpd.server_address[1], flush=True)
httpd.serve_forever()
`;
  const env = { ...process.env, PINNY_REVIEWER: 'e2e' };
  delete env.PINNY_OCR_ENGINE;
  return new Promise((res, rej) => {
    const proc = spawn(PY, ['-c', code], { cwd: REPO, env });
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

const { proc, base } = await startServer(join(work, 'data'));
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1300, height: 900 } });
  page.on('pageerror', (e) => check('no page errors', false, e.message));
  page.on('console', (m) => { if (m.type() === 'error') check('no console errors', false, m.text()); });
  await page.goto(base + '/');
  await page.locator('#file').setInputFiles(pdfPath);
  await page.waitForFunction(() => window.__pinny && window.__pinny.idle() && /[#&]p=0/.test(location.hash),
    null, { timeout: 60000 });

  await page.waitForSelector('#ocr-section:not([hidden])', { timeout: 10000 });
  check('admin sees the OCR switch', true);
  check('OCR starts off', await page.locator('#ocr-engine').inputValue() === 'none');
  check('Read sheet info is hidden while OCR is off', await page.locator('#sheet-info').isHidden());

  await page.locator('#ocr-engine').selectOption('tesseract');
  await page.locator('#ocr-save-btn').click();
  await page.waitForSelector('#ocr-status.ok', { timeout: 10000 });
  await page.waitForSelector('#sheet-info:not([hidden])', { timeout: 10000 });
  check('turning OCR on shows Read sheet info', true);

  await page.locator('#sheet-info-btn').click();
  await page.waitForSelector('#sheet-info-result:not([hidden]) table', { timeout: 90000 });
  const cells = async () => page.$$eval('#sheet-info-result tr', (rows) =>
    Object.fromEntries(rows.map((r) => [r.cells[0].textContent, r.cells[1].textContent])));
  let got = await cells();
  check('sheet number read', got.Sheet === 'E-101', JSON.stringify(got));
  check('sheet title read', got.Title === 'FIRST FLOOR POWER PLAN', JSON.stringify(got));
  check('revision read from the table', got.Revision === '2', JSON.stringify(got));
  check('no warnings shown', await page.locator('#sheet-info-result .status.error').count() === 0);

  await page.reload();
  await page.waitForSelector('#sheet-info-result:not([hidden]) table', { timeout: 30000 });
  got = await cells();
  check('saved result shows again after a reload', got.Sheet === 'E-101', JSON.stringify(got));

  await page.locator('#ocr-engine').selectOption('none');
  await page.locator('#ocr-save-btn').click();
  await page.waitForSelector('#ocr-status.ok', { timeout: 10000 });
  check('turning OCR off hides Read sheet info', await page.locator('#sheet-info').isHidden());
} finally {
  await browser.close();
  proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} FAILED` : 'all passed');
process.exit(failures ? 1 : 0);
