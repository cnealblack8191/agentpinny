# Pinny training site: plan for running on the internet

Status: plan, 2026-09-25, from the coordination session on
`claude/quirky-mccarthy-hcttkj`. Code references are to `origin/phase-2`
unless noted.

The training site is the existing viewer (upload a drawing, box a template,
scan, approve, reject or add pins) plus pages to see how much labelled data
there is, build datasets, train models, compare a new model with the active
one and promote the winner. It will be reachable from the internet on the
owner's own servers. It holds client drawings, so treat everything as
confidential: invited users only, and every page behind sign-in.

## 1. Server facts

Fill this in once and paste it into the prompts that ask for it.

```
SERVER FACTS
- Machine(s) for the site: OS, CPU cores, RAM, free SSD space, GPU (if any)
- Docker available? (yes / no / can install)
- Network: public IP with ports 80/443 open | behind an office or home router | behind an existing reverse proxy (nginx, IIS, Traefik, ...)
- Domain or subdomain for the site, and where its DNS is managed
- Company sign-in: Microsoft 365 / Entra ID | Google Workspace | other
- Who will use it: employees only | also invited customers; roughly how many
- Where backups go today (NAS, cloud bucket, backup service)
- A separate machine with a GPU for training (optional)
- Anything already running on these machines that must not be disturbed
```

## 2. What in the current code is unsafe on the internet

The viewer was built as a single-user app on `127.0.0.1`
(`pinny/viewer/server.py:2`). That was right for a local prototype. On the
internet these become real problems:

| # | Problem | Where | Fixed in |
|---|---|---|---|
| 1 | No sign-in or access control. Anyone who finds the URL can list every drawing, download page images, run scans, change pins and export reports. | every route in `pinny/viewer/server.py` | Step 3 |
| 2 | No ownership. There is one global document list (`pinny/render/service.py:213`). Uploading the same file as someone else returns *their* document id (`:187`), and the `version_owned_by_other_document` error (`:184`) confirms that someone already uploaded a given file. | render service, viewer | Step 3 |
| 3 | Every review is recorded under the server's OS account (`pinny/learning/store.py:121`, `pinny/viewer/service.py:105`). | learning store, viewer | Step 3 |
| 4 | Python's built-in `http.server`: no TLS, no read timeout (a slow client holds a thread forever), and an unbounded thread per connection (`server.py:214`). | viewer server | Step 3 |
| 5 | Uploads are read into memory whole, up to 200 MB each (`server.py:112`, `render/service.py:47`). A few parallel uploads exhaust RAM. | viewer server | Step 3 |
| 6 | Heavy work runs inside web requests: PDF inspection on upload (`render/service.py:188`), page renders up to 100 MP (about 300 MB each), scans up to 60 s. PDFium sits behind one process-wide lock (`render/pdf.py:19`), so one large render stalls everyone. | render, viewer | Step 4 |
| 7 | Untrusted PDFs are parsed inside the web process (PDFium, and `pikepdf` in `pinny.vector` on `claude/intelligent-wozniak-3kf3wk`). A parser bug would hand an attacker the server. | render, vector | Step 4 |
| 8 | No CSRF protection. The JSON and upload handlers accept any Content-Type and never check `Origin` (`server.py:111`, `:171`). Harmless locally, exploitable once a sign-in cookie exists. | viewer server | Step 3 |
| 9 | The only security header is `nosniff` (`server.py:192`): no CSP, HSTS or clickjacking protection. | viewer server | Step 3 |
| 10 | Internal exception text and server paths reach the client (`model_load_failed` and `model_mismatch` in `viewer/service.py`, and the generic `PinnyError` pass-through in `server.py`). | viewer | Step 3 |
| 11 | Nothing can be deleted. Drawings, rasters and crops are kept forever. | render, store | Step 3 |
| 12 | Two different default data directories. The render service uses `./pinny-data` relative to the working directory (`render/service.py:55`). The learning store and training CLI use `~/.local/share/pinny` (`learning/store.py:132`). | render, store | Step 3 |
| 13 | Version provenance comes from `git rev-parse` at run time (`viewer/service.py:64`, `models/artifact.py`, `benchmark/modes.py`), which gives `git:unknown` in a deployed image. | several | Step 3 |
| 14 | Several CLIs take file paths (`registry promote --evidence`, `build-dataset --export/--out-dir`, `dataset-info <path>`). If the web layer passes client input into them, that is arbitrary file read and write. | models, training | Steps 4 and 7 |
| 15 | The browser's unsaved-edit queue uses one fixed key (`web/edits.js:12`). On a shared computer, one person's unsaved edits would be sent under the next person's sign-in. | web | Step 3 |

Keep what is already good: directory names come only from content hashes,
`version_hex` validates ids, page and pixel limits exist, model weights load
with `weights_only=True` after a sha256 check, requests are idempotent, the
UI escapes server text, and the lock file pins hashes.

## 3. Security baseline (every session follows this)

1. **Deny by default.** Every route under `/api/` needs a signed-in member,
   except `/healthz`. Each route declares its minimum role, and a test fails
   if one doesn't.
2. **Check ownership on every lookup**: documents, pages, rasters, scans,
   pins, reports, datasets, jobs and models. Answer 404, not 403, for another
   workspace's objects.
3. **Validate ids at the edge**: `document_version` as `sha256:<64 hex>`,
   scan and job ids as UUIDs, model ids against the registry's pattern.
4. **Clients never send file paths.** Jobs take ids. The server resolves
   paths under `$PINNY_DATA_DIR`.
5. **CSRF.** State-changing requests must carry
   `Content-Type: application/json` (or `application/pdf` for uploads) and an
   `Origin` equal to the site's origin. Any cookie the app sets is
   `Secure; HttpOnly; SameSite=Lax`.
6. **Headers on every response**:
   `Content-Security-Policy: default-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
   `Strict-Transport-Security: max-age=31536000`,
   `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
   `Cache-Control: no-store` on API JSON and `private` on rasters.
7. **Uploads stream to disk** with the size limit enforced while reading.
   They are checked for `%PDF-` and parsed only by sandboxed workers.
8. **No PDF parsing, rendering, scanning or training in the web process.**
9. **Errors to clients** carry a stable code, a plain message and a request
   id. Paths, exception text and stack traces go only to the server log.
10. **Reviewer identity** is the signed-in user's stable id and email, never
    the OS user. `$PINNY_REVIEWER` is honoured only by the CLI and tests.
11. **Admin-only**: training, promotion, deactivation, member management,
    deleting other people's uploads, and model uploads.
12. **Deletion** removes the PDF, rasters and crops, keeps a tombstone for
    scans and labels, flags datasets built from the document, and writes an
    audit event.
13. **Production mode** (`PINNY_ENV=production`) refuses to start without
    `PINNY_DATA_DIR`, the gate settings and a baked-in version string.
    Secrets come from the environment or a secret store, never from git.
14. **Dependencies** stay pinned with hashes in `requirements.lock.txt`. A
    new dependency means updating `pyproject.toml`, the lock file and this
    document.

## 4. Target architecture

```
 Internet
    |  HTTPS only
    v
 +---------------------------------+
 | Gate: TLS + company sign-in     |   Cloudflare Tunnel + Access,
 | rate limits, body-size limit    |   or Caddy + oauth2-proxy
 +----------------+----------------+
                  | verified identity (signed token)
                  v
 +---------------------------------+          +--------------------------------+
 | web: API + static UI            |  jobs -> | scan workers (sandboxed)       |
 | Starlette on uvicorn            |          | ingest, render, scan           |
 | sign-in, workspaces, quotas     |          | no network, low-privilege user,|
 | never opens a PDF               |          | memory and time limits         |
 +----------------+----------------+          +--------------------------------+
                  |                           | training worker (admin jobs)   |
                  |                           | dataset, train, benchmark      |
                  v                           +---------------+----------------+
 +----------------------------------------------------------------------------+
 | $PINNY_DATA_DIR on a local SSD: workspaces/<id>/documents, learning db,     |
 | jobs db, models/   ---> nightly backup to your existing backup target       |
 +----------------------------------------------------------------------------+
```

**Gate (sign-in and TLS).** Don't build a login system. Use the company
identity provider, which already has MFA and offboarding.

| Your network | Recommended gate |
|---|---|
| Server behind an office or home router, or you'd rather not open ports | Cloudflare Tunnel + Cloudflare Access. The server makes an outbound connection only. Free for small teams; needs the domain's DNS on Cloudflare. |
| Server with a public IP | Caddy (automatic HTTPS) + oauth2-proxy connected to Microsoft Entra ID or Google Workspace |
| An existing nginx, IIS or Traefik | Keep it for TLS and add oauth2-proxy behind it |

The app listens only on localhost or a private network and verifies the
gate's signed token (the Cloudflare Access JWT, or oauth2-proxy's token). It
also keeps its own members table, so an account the gate lets through but
nobody invited still gets 403.

**Roles.** `admin`: members, training, promotion, deleting anything.
`reviewer`: upload, scan, label, delete own uploads.

**Workspaces.** Every stored object carries a `workspace_id`. Start with one
workspace for your team, but check it everywhere from day one, so adding
customers later is configuration, not a rewrite. Storage moves to
`$PINNY_DATA_DIR/workspaces/<workspace_id>/documents/<sha256>/`, and
identical files are de-duplicated only within one workspace.

**Web process.** Replace `http.server` with Starlette on uvicorn, which runs
on Linux and Windows. `ViewerService` stays the service layer. One web
process is enough to start, because the learning store is SQLite with a
single writer.

**Jobs and sandbox.** A `jobs` table in SQLite, so no Redis. Workers poll it
and run each job in a fresh subprocess:

* Linux or Docker: a separate worker container with no network, a read-only
  root filesystem, a non-root user and container memory and CPU limits,
  plus `RLIMIT_AS` and `RLIMIT_CPU` per job.
* Windows without Docker: a dedicated low-privilege local account, a Job
  Object for memory and CPU limits, and a firewall rule that blocks the
  worker's outbound traffic.

Job kinds: `ingest`, `render_page`, `scan`, `build_dataset`,
`train_verifier`, `train_detector`, `benchmark`, `refresh_template_bank`.
Training runs one job at a time, on a GPU machine if you have one.

**Starting limits** (tune them in Step 8):

| Limit | Start at |
|---|---|
| Upload size | 100 MB (was 200 MB) |
| Pages per PDF | 500 |
| Pixels per page raster | 100 MP |
| Scan job | 120 s wall clock and 1.5 GB memory, then killed |
| Training job | 8 GB memory, admin-only, one at a time |
| Concurrent jobs per user | 2 |
| Uploads per user per day | 50 |
| Storage per workspace | 20 GB, alert at 80 % |
| JSON body | 1 MB |
| Gate request timeout | 60 s (uploads 300 s) |

**Data.** Keep `$PINNY_DATA_DIR` on a local disk of the server. Never put the
SQLite files on a network share, because SQLite's WAL mode is not safe over
SMB or NFS. Back up nightly with SQLite's online backup (never a plain file
copy of a live database) plus the `workspaces/` and `models/` folders. Keep
30 days and test a restore monthly. Turn on disk encryption (BitLocker or
LUKS) if the drawings are confidential.

**Operations.** JSON access logs (user, workspace, route, status, duration).
An audit log of uploads, deletions, promotions and membership changes
(reviews are already recorded as events). Alerts on 5xx spikes, disk above
80 %, failed jobs and failed backups. An uptime check on `/healthz`, which
says only "ok". Rebuild images monthly and whenever pypdfium2 or PDFium ships
a security fix. Deploy to staging before production.

## 5. Steps and prompts

Steps 3, 4 and 5 can run in parallel sessions, and so can 7a and 7b.
Everything else runs in order. Cloud sessions can't reach your servers, so
steps 6b and 9 run in Claude Code on the server itself (or you follow
`docs/deployment.md` by hand).

New ownership areas for `docs/agent-ownership.md`:

| Area | Paths | Branch |
|---|---|---|
| Training-site contract | `docs/training-site.md`, `docs/training-site-plan.md` | `training-site` (coordinator) |
| Secure web tier | `pinny/viewer/`, `web/` (viewer pages), `tests/viewer/` | `training-site-web` |
| Jobs and sandbox | `pinny/jobs/`, `tests/jobs/` | `training-site-jobs` |
| Deployment | `deploy/`, `docs/deployment.md`, CI image builds | `training-site-deploy` |
| Training API | training routes in `pinny/viewer/` or `pinny/site/` | `training-site-train-api` |
| Training pages | `web/` training pages, Playwright tests | `training-site-train-ui` |

### Step 0: you, no prompt

1. Fill in the server facts in section 1.
2. Launch invite-only, and pick the gate from the table in section 4.
3. Let the session on `claude/intelligent-wozniak-3kf3wk` finish and push
   before Step 1.

### Step 1: combine the branches

> Create branch `training-site` from `origin/phase-2` and merge `origin/claude/intelligent-wozniak-3kf3wk` into it (they split at c05a98f). Resolve conflicts in pinny/detection/, pinny/learning/ (the schema migrations must cover both sides), docs/contracts.md, evaluation/ and the dependency files: fold requirements.txt into pyproject.toml and regenerate requirements.lock.txt with uv, keeping hashes. Copy docs/training-site-plan.md from origin/claude/quirky-mccarthy-hcttkj. Run every test suite plus the evaluator's mini-corpus gate and fix what breaks. Add the ownership areas from docs/training-site-plan.md section 5 to docs/agent-ownership.md and update the README. Push `training-site` and report the test counts.

### Step 2: contract and threat model (docs only)

> On `training-site`, read docs/training-site-plan.md, then write docs/training-site.md, the contract every later session builds against. Cover: (1) identity from the sign-in gate, how the app verifies it, the members table and the admin and reviewer roles; (2) workspaces, the workspace_id on every stored object and the new storage layout; (3) every HTTP endpoint, existing viewer routes and new training routes, with request and response JSON, minimum role and error codes; (4) the job model: kinds, states, id-only payloads, limits, cancellation and crash recovery; (5) the limits and quotas table; (6) audit events; (7) deletion and retention; (8) a short threat model that maps each problem in plan section 2 to its fix; (9) the seam between the web tier and the workers: a RenderClient interface in pinny/viewer/render_client.py. My server facts: <paste SERVER FACTS>. Docs only, no code. Push.

### Step 3: secure web tier (session A)

> Branch `training-site-web` from `training-site`. Implement the web tier of docs/training-site.md, following the baseline in docs/training-site-plan.md section 3. It fixes the problems in plan section 2 marked "Step 3". Move the HTTP layer from http.server to Starlette on uvicorn and keep every existing route working, with ViewerService as the service layer. Verify the gate's identity token. Add members, roles and workspaces, with ownership checks on every object; per-workspace storage and de-duplication; streaming uploads; Content-Type and Origin checks; the security headers; generic client errors with request ids; a delete-document endpoint; one PINNY_DATA_DIR for everything; a production mode that refuses to start without its settings; and a build-time version string. Call the render service only through the RenderClient interface in the contract, because Step 4 swaps in workers. In web/, show the signed-in user and a sign-out link, key the edit queue by user, and keep every page working under the CSP. Add an authorization-matrix test (every route called as anonymous, another workspace's member, reviewer and admin) and a test that fails when a route has no declared role. Push.

### Step 4: jobs and sandbox (session B)

> Branch `training-site-jobs` from `training-site`. Implement the job model in docs/training-site.md in pinny/jobs/: a SQLite-backed queue; a worker entry point (python -m pinny.jobs.worker --pool scan|train); one subprocess per job with memory, CPU-time and wall-clock limits and no network access (rlimits and containers on Linux, Job Objects on Windows); cancellation; and recovery of jobs orphaned by a restart. Job kinds: ingest, render_page, scan, build_dataset, train_verifier, train_detector, benchmark, refresh_template_bank. Each calls the existing Python APIs with ids only and never passes client input as a file path. Implement a job-backed RenderClient (the interface in the contract) so the web process never opens a PDF. Enforce the per-user and per-workspace quotas. Test with synthetic PDFs, including a job that exceeds its memory limit, one that hangs, and a worker killed mid-job. This fixes the problems in plan section 2 marked "Step 4". Push.

### Step 5: deployment package (session C)

> Branch `training-site-deploy` from `training-site`. Using docs/training-site-plan.md section 4 and my server facts: <paste SERVER FACTS>, create deploy/: images for web and worker (non-root, read-only root filesystem, worker with no network); a compose file with memory and CPU limits (or Windows service scripts if my server can't run Docker); the gate config for my network and identity provider; staging and production profiles with separate data dirs and hostnames; nightly backups (SQLite online backup plus workspaces/ and models/) to my backup target with 30-day retention, and a restore script; log rotation; health checks; and alerts for disk space, 5xx errors, failed jobs and failed backups. Add pip-audit and the image builds to CI. Write docs/deployment.md: first-time setup, deploy, roll back, restore, rotate secrets, add and remove a user. Don't change pinny/. Push.

### Step 6a: merge the platform (cloud session)

> On `training-site`, merge `training-site-web`, `training-site-jobs` and `training-site-deploy`. Switch the web tier to the job-backed RenderClient so the web process never opens a PDF, and get every test passing, including the authorization-matrix test. If Docker works here, start the compose stack with the staging profile and a fake gate, and smoke-test upload, scan, review, export and delete as two users in different workspaces. Update docs/agent-ownership.md. Push.

### Step 6b: staging on your server (Claude Code on the server)

> You're on my server. Server facts: <paste SERVER FACTS>. Clone github.com/cnealblack8191/agentpinny, check out `training-site`, and follow docs/deployment.md to set up STAGING only: the gate, the web and worker services, the data directory, backups and alerts. Leave everything listed under "must not be disturbed" alone. Show me each command that changes the system before running it. Finish by running one backup, restoring it into a scratch directory, and giving me the staging URL.

### Step 7a: training API (parallel with 7b)

> Branch `training-site-train-api` from `training-site`. Add the training endpoints from docs/training-site.md on top of the job runner: dashboard stats (labels, fully reviewed pages, readiness against the contract's thresholds), the review queue (most uncertain first, using the learning store's review_queue), mark page fully reviewed, build dataset, train verifier and detector, benchmark, list models, promote and deactivate. Promote takes a benchmark job id, never a file path, and calls pinny.models.registry.promote with that job's report. Training, promotion and model management are admin-only. Extend the authorization-matrix test to every new route. Push.

### Step 7b: training pages (parallel with 7a)

> Branch `training-site-train-ui` from `training-site`. Build the training pages in web/ from docs/training-site.md: Dashboard; Label queue (opens the viewer on the most uncertain pins, with "mark page fully reviewed"); Datasets; Training runs (progress and log tail by polling); Models (candidate vs active benchmark side by side, with Promote enabled only for admins and only when the benchmark gate recommends it). Plain JS with no build step, no inline script or style, and server text inserted only with textContent or escaping. Stub endpoints that aren't merged yet. Add Playwright tests for each page and for a reviewer who must not see admin actions. Push.

### Step 8: attack it before users do

> On `training-site`, merge `training-site-train-api` and `training-site-train-ui`, then test the site as an attacker would, against the compose stack with the staging profile. Run /security-review on the branch and an OWASP ZAP baseline scan. Try every object type across workspaces, CSRF from another origin, hostile PDFs (decompression bombs, 10,000 pages, huge page sizes, broken xref tables), a slow-upload client and a job that never finishes. Run pip-audit. Load-test with Locust at <number of users> users, each uploading and scanning a real Arch D sheet, and record memory and latency. Fix every finding, set the worker pool sizes and limits in docs/training-site.md from the measurements, and write docs/security-verification.md. Push.

### Step 9: go live

Check these yourself first:

1. From a phone on mobile data, open the URL. You must land on the sign-in
   page, and an account nobody invited must be refused.
2. From outside, only port 443 (and 80, redirecting to it) is reachable. With
   Cloudflare Tunnel, no inbound port is open at all. The app and worker
   ports are never reachable.
3. A backup has run and you have restored it once.
4. Invite the first reviewers. Keep admins to one or two people.

Then, in Claude Code on the server:

> You're on my server. Set up production from `training-site` following docs/deployment.md, with its own data directory and hostname, separate from staging. Show me each command that changes the system before running it. Then confirm from outside that unauthenticated requests are redirected to sign-in, that the app and worker ports aren't reachable, and that the first production backup succeeds.

After a few weeks of labelling, attach the benchmark reports from the Models
page to a new session:

> Here are benchmark reports from production (attached). Retune the template, verifier and model thresholds and report precision and recall per page, with evidence. Change defaults only where the evidence supports it. Branch from `training-site`, push, and tell me what to deploy.

## 6. Later (not needed for launch)

* **Customers.** Give each customer a workspace and a separate model. Never
  train on one customer's drawings for another without written consent. Add
  terms of use and a data processing agreement, and a way to handle abuse.
* **Postgres**, once you need more than one web process.
* **Claude features** from `docs/improvement-review.md` (legend reader, crop
  adjudicator): API key on the server only, admin opt-in per workspace, send
  crops only, and log what was sent.
* **Public demo.** The browser-only scan page (`scripts/browser-scan/` on
  `claude/gifted-euler-hg9hr3`) runs in the visitor's browser and uploads
  nothing, so it can be hosted publicly as a static page. Before that,
  upgrade pdf.js: the page loads 3.11.174 from cdnjs
  (`index.src.html:402`), which runs script embedded in a malicious PDF
  (CVE-2024-4367, fixed in 4.2.67). Self-host pdf.js and the Google fonts
  so the page loads nothing from third parties.
