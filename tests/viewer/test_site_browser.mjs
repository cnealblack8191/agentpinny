// Browser check of the signed-in training site (docs/training-site.md).
//
//   node tests/viewer/test_site_browser.mjs
//
// Runs the real server in production mode with a throwaway signing key in
// place of Cloudflare's, and gives each browser context its own
// Cf-Access-Jwt-Assertion header, as Cloudflare Access would. Checks: the
// user bar and sign-out link, the admin-only Members panel, reviewers
// cannot delete someone else's drawing but can delete their own, a
// non-member is turned away, the page runs under the CSP, and unsaved
// edits are stored per person.

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
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
const OUTSIDER = 'stranger@example.com';
const work = mkdtempSync(join(tmpdir(), 'pinny-site-'));

// Serve in production mode; print the base URL and a token per user.
function startServer() {
  const code = `
import json, socket, sys, time, jwt
sys.path.insert(0, 'tests/viewer')
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from pdfgen import make_pdf
from pinny.viewer import ViewerService
from pinny.viewer.auth import Authenticator, CloudflareAccessVerifier
from pinny.viewer.server import Site, SiteServer
from pinny.viewer.settings import Settings
from pinny.viewer.sitedb import SiteDB
data = Path(${JSON.stringify(work)}) / "data"
open(Path(${JSON.stringify(work)}) / "a.pdf", "wb").write(make_pdf(tag="a"))
open(Path(${JSON.stringify(work)}) / "b.pdf", "wb").write(make_pdf(tag="b"))
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
origin = "http://127.0.0.1:%d" % port
settings = Settings(env="production", data_dir=data, origin=origin, cf_team_domain="t.cloudflareaccess.com",
                    cf_aud="aud", admin_emails=(${JSON.stringify(ADMIN)},), version="e2e")
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ver = CloudflareAccessVerifier("t.cloudflareaccess.com", "aud", key_for=lambda kid: key.public_key())
svc = ViewerService(data)
db = SiteDB(data); db.ensure_admins(settings.admin_emails)
site = Site(svc, settings, Authenticator(settings, db, ver))
site.sitedb.put_member(${JSON.stringify(REVIEWER)}, "reviewer", actor="setup")
def tok(email):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": ["aud"], "iss": "https://t.cloudflareaccess.com",
                       "iat": now, "exp": now + 3600}, key, algorithm="RS256", headers={"kid": "k"})
httpd = SiteServer(site, "127.0.0.1", port)
print(json.dumps({"base": origin, "tokens": {e: tok(e) for e in (${JSON.stringify(ADMIN)},
      ${JSON.stringify(REVIEWER)}, ${JSON.stringify(OUTSIDER)})}}), flush=True)
httpd.serve_forever()
`;
  return new Promise((res, rej) => {
    const proc = spawn(PY, ['-c', code], { cwd: REPO });
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

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
let server = null;
try {
  server = await startServer();
  const cspErrors = [];
  async function open(email) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 },
      extraHTTPHeaders: { 'Cf-Access-Jwt-Assertion': server.tokens[email] } });
    const page = await ctx.newPage();
    page.on('dialog', (d) => d.accept());
    page.on('pageerror', (e) => check(`${email}: no page errors`, false, e.message));
    page.on('console', (m) => { if (/Content Security Policy/i.test(m.text())) cspErrors.push(m.text()); });
    await page.goto(server.base + '/');
    return { ctx, page };
  }

  // ------------------------------------------------------------- admin
  const admin = await open(ADMIN);
  let page = admin.page;
  await waitFor(page, () => document.getElementById('user-bar').textContent.includes('Signed in'));
  const bar = await page.locator('#user-bar').textContent();
  check('user bar names the admin', bar.includes(ADMIN) && bar.includes('(admin)'), bar);
  check('sign-out link goes to Cloudflare Access',
    (await page.locator('#user-bar a').getAttribute('href')) === '/cdn-cgi/access/logout');
  await waitFor(page, () => document.querySelectorAll('#members-table tbody tr').length === 2);
  check('admin sees the Members panel', await page.locator('#members-section').isVisible());
  await page.locator('#member-email').fill('draftsman@example.com');
  await page.locator('#member-add-btn').click();
  await waitFor(page, () => document.querySelectorAll('#members-table tbody tr').length === 3);
  check('admin adds a member', (await page.locator('#members-table').textContent()).includes('draftsman@example.com'));
  await page.locator('#file').setInputFiles(join(work, 'a.pdf'));
  await waitFor(page, () => window.__pinny.frame() !== null);
  check('admin can delete their own upload', !(await page.locator('#delete-doc-btn').isDisabled()));
  if (SHOTS) await page.screenshot({ path: join(SHOTS, 'site-admin.png') });

  // ---------------------------------------------------------- reviewer
  const rev = await open(REVIEWER);
  page = rev.page;
  await waitFor(page, () => document.getElementById('user-bar').textContent.includes('Signed in'));
  check('reviewer sees their own name', (await page.locator('#user-bar').textContent()).includes(`${REVIEWER} (reviewer)`));
  check('reviewer does not see Members', !(await page.locator('#members-section').isVisible()));
  await waitFor(page, () => document.querySelectorAll('#doc-select option').length === 2);
  await page.locator('#doc-select').selectOption({ index: 1 });
  await waitFor(page, () => window.__pinny.frame() !== null);
  check("reviewer cannot delete the admin's drawing", await page.locator('#delete-doc-btn').isDisabled());
  await page.locator('#file').setInputFiles(join(work, 'b.pdf'));
  await waitFor(page, () => window.__pinny.frame() !== null && document.getElementById('doc-status').textContent.includes('Uploaded'));
  check('reviewer can delete their own upload', !(await page.locator('#delete-doc-btn').isDisabled()));
  await page.locator('#delete-doc-btn').click();
  await waitFor(page, () => document.getElementById('doc-status').textContent.includes('Deleted'));
  check('delete removes it from the list', (await page.locator('#doc-select option').count()) === 2);
  const qkey = await page.evaluate(() => window.__pinny.queueKey());
  const adminKey = await admin.page.evaluate(() => window.__pinny.queueKey());
  check('unsaved edits are stored per person', qkey === `pinny.viewer.outbox.v1:${REVIEWER}`
    && adminKey === `pinny.viewer.outbox.v1:${ADMIN}`, `${qkey} / ${adminKey}`);

  // ---------------------------------------------------------- outsider
  const out = await open(OUTSIDER);
  await waitFor(out.page, () => document.getElementById('user-bar').textContent.length > 0);
  check('a non-member is turned away', (await out.page.locator('#user-bar').textContent()) === 'Not a Pinny member.'
    && (await out.page.locator('#view-message').textContent()).includes('not a Pinny member'));
  check('no CSP violations', cspErrors.length === 0, cspErrors.join(' | '));
} finally {
  await browser.close();
  if (server) server.proc.kill();
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `${failures} check(s) failed` : 'all checks passed');
process.exit(failures ? 1 : 0);
