# Pinny training site: contract

Status: v1, 2026-09-27. This is the contract every training-site session
builds against. It follows the plan in `docs/training-site-plan.md`, whose
section 1 holds the server facts: one EC2 instance in the owner's AWS
account at `pinny.ecinc.us`, Caddy for HTTPS, Pinny's own sign-in,
three users, backups to S3. Anything here that contradicts `docs/contracts.md` sections
1-6 is a mistake; those still govern documents, coordinates, scans and
reviews.

Implementation status is marked per section: **built** (on
`claude/beautiful-brahmagupta-vqz21o`), or **planned** (Step 7 training
pages). The deployment (Step 5) is `deploy/`, run as described in
`docs/deployment.md`: Caddy and native systemd services on one instance
that runs on weekdays 07:00-19:00 New York time.

## 1. Identity and roles (built)

**Pinny has its own sign-in** (decided 2026-09-28: no paid identity
service; stronger layers such as second factors come later if the project
proves worth it). HTTPS ends at Caddy on the same instance, which forwards
to the app on `127.0.0.1`. Nothing else can reach the app.

* **Passwords** are at least 12 characters and are stored only as salted
  scrypt hashes (`pinny/viewer/passwords.py`) in `site.sqlite3`.
* **Nobody signs up.** An admin adds a member and gets a one-time
  **set-password link** (`/setup.html#token=...`, valid 72 hours, replaced
  by any newer link) to send them privately. The token rides in the URL
  fragment, which browsers never send to a server, so it stays out of logs.
  The first admin gets theirs from the server:
  `python -m pinny.viewer.members setup-link EMAIL`.
* **Signing in** (`POST /api/login`) checks the password and sets a session
  cookie: `__Host-pinny_session`, `Secure; HttpOnly; SameSite=Lax; Path=/`.
  Only a SHA-256 of the session token is stored, so a copy of the database
  (a backup) cannot be used to sign in. A session ends after 7 days without
  use, and after 30 days regardless.
* **Wrong passwords.** Every failure gives the same answer, whether the
  email is unknown, has no password yet, or the password is wrong, and
  takes the same time. 10 wrong passwords in a row lock that account for
  15 minutes (`account_locked` audit event). 30 failures from one client
  address in 15 minutes block that address for the rest of the window.
  The client address is the one Caddy reports (`X-Forwarded-For` is
  trusted only from `127.0.0.1`).
* **Forgot password:** an admin clicks *Reset password* on the Members panel
  (or runs `python -m pinny.viewer.members reset EMAIL`). That removes the
  password, signs the person out everywhere, and gives a new link.
* **Changing your password** (`POST /api/me/password`, needs the current one)
  signs out your other devices. Removing a member ends their sessions at
  once. `python -m pinny.viewer.members sign-out-all` ends every session.
* **Sign-out** (`POST /api/logout`) deletes the session on the server and
  clears the cookie.
* Every request looks the session up, then the member. A request with no
  valid session gets `401 unauthenticated`, and the page sends the
  browser to `/login.html`.

**Members.** Only members can sign in. The members table decides who is
in and with which role; a session whose member was removed in the same
instant gets `403 not_a_member`.

| Role | Can |
|---|---|
| `admin` | everything a reviewer can, plus: add, change and remove members; delete any document; read the audit log; (Step 7) train, promote and deactivate models |
| `reviewer` | upload, scan, batch scan, review pins, export reports, delete documents they uploaded |

Every route declares a minimum role (`public`, `reviewer` or `admin`). A
test fails if any route has none. `public` is only `GET /healthz`, the
sign-in routes (`/api/login`, `/api/logout`, `/api/setup/check`,
`/api/setup`) and the static page files.

The first admin comes from `PINNY_ADMIN_EMAILS` (comma-separated), added on
startup if missing and never removed by it. Further members are managed on
the Members panel or with `python -m pinny.viewer.members`.

**Local development** (`PINNY_ENV=development`, the default) has no gate:
every request is the local user, an admin whose email is
`$PINNY_REVIEWER`, else the OS user at `localhost`. Development mode
refuses to start on anything but a loopback address, and production mode
refuses development auth.

**Reviewer identity.** Review events record the signed-in member's email
(contracts section 5 `reviewer`). Scans and batches record who requested
them in their metadata (`requested_by`). `$PINNY_REVIEWER` is honoured only
in development and by the CLI.

## 2. Workspaces and storage (built, single workspace)

Three people share every document, so there is one workspace and no
per-object ownership check beyond roles. Each document records its
uploader (`uploaded_by`), which decides who may delete it. Adding
workspaces later means a `workspace_id` on documents, scans and batches and
a check on every lookup; the route table (section 3) is the single place
to add it.

Everything lives under one `PINNY_DATA_DIR` (required in production):

```
$PINNY_DATA_DIR/
  documents/<sha256>/source.pdf, version.json, pages/p<i>.png   render service
  pinny.sqlite3, crops/, exports/                               learning store
  site.sqlite3                                                  members, uploads, deletions, audit
  models/                                                       model registry
  tmp/                                                          upload staging
```

## 3. HTTP API (built)

The site is one origin, `PINNY_ORIGIN` (for example
`https://pinny.example.com`). All JSON. Errors are
`{"error": {"code", "message", "request_id"}}`; every response carries
`X-Request-Id`.

| Method and path | Role | Body / query | Notes |
|---|---|---|---|
| `GET /healthz` | public | | `{"ok": true, "version"}`, no data |
| `POST /api/login` | public | `{email, password}` | sets the session cookie; 401 `bad_login`, 429 `account_locked` / `too_many_attempts` |
| `POST /api/logout` | public | `{}` | ends the session, clears the cookie |
| `POST /api/setup/check` | public | `{token}` | `{email}`; 400 `bad_setup_link` |
| `POST /api/setup` | public | `{token, password}` | sets the password, signs in; 400 `weak_password` |
| `GET /api/me` | reviewer | | `{"email", "role", "env", "sign_in", "version"}` |
| `POST /api/me/password` | reviewer | `{current_password, new_password}` | signs out your other sessions |
| `GET /api/health` | reviewer | | as before, plus `version` |
| `GET /api/documents` | reviewer | | adds `uploaded_by` |
| `POST /api/documents?filename=` | reviewer | PDF bytes, `Content-Type: application/pdf` | streamed to disk; 413 over the limit |
| `DELETE /api/documents/{version}` | reviewer (own) / admin | | section 7 |
| `GET /api/documents/{version}/pages/{i}/frame` | reviewer | | |
| `GET /api/documents/{version}/pages/{i}/raster.png` | reviewer | | `Cache-Control: private` |
| `GET /api/documents/{version}/pages/{i}/scans` | reviewer | | |
| `GET /api/documents/{version}/batches` | reviewer | | |
| `GET /api/models` | reviewer | | |
| `POST /api/scans` | reviewer | as `docs/viewer.md` | |
| `GET /api/scans/{id}` | reviewer | | |
| `POST /api/scans/{id}/actions` | reviewer | as `docs/viewer.md` | reviewer = signed-in email |
| `GET /api/scans/{id}/report` | reviewer | | attachment |
| `POST /api/batches` | reviewer | as `docs/viewer.md` | |
| `GET /api/batches/{id}` | reviewer | | |
| `GET /api/batches/{id}/queue` | reviewer | `strategy`, `limit` | |
| `POST /api/batches/{id}/cancel` | reviewer | `{}` | |
| `POST /api/batches/{id}/resume` | reviewer | `{retry_failed?}` | |
| `GET /api/members` | admin | | `[{email, role, added_at, added_by, has_password}]` |
| `POST /api/members` | admin | `{email, role}` | add or change role; a member with no password yet also gets `setup: {email, setup_url, expires_at}` |
| `POST /api/members/setup-link` | admin | `{email, reset?}` | `{email, setup_url, expires_at}`; `reset: true` removes the password and ends their sessions (not for yourself) |
| `POST /api/members/remove` | admin | `{email}` | an admin cannot remove themself |
| `GET /api/audit?limit=` | admin | | newest first |

**Id validation at the edge.** `version` must be `sha256:` plus 64
lower-case hex; scan and batch ids must be UUIDs; page indexes are
non-negative integers. A malformed id reads as `404` (not found), exactly
like a well-formed id that does not exist, and the service is never
called with it.

**CSRF.** Every state-changing request (`POST`, `DELETE`) must carry an
`Origin` equal to `PINNY_ORIGIN` (in development: the request's own host)
and the right `Content-Type` (`application/json`, or `application/pdf` for
uploads). Otherwise `403 bad_origin` or `415 unsupported_media_type`.

**Headers on every response:** the CSP from plan section 3 item 6,
`Strict-Transport-Security` (production only), `Referrer-Policy:
no-referrer`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Cache-Control: no-store` on API JSON.

**Errors.** A `PinnyError` with a 4xx status keeps its code and message,
which are written for users. Any 5xx, and any other exception, becomes
`500 internal_error` with the request id; the detail goes only to the
server log.

## 4. Jobs and sandbox (built)

With jobs on (`PINNY_JOBS=sandbox` or `external`, required in production)
the web process never opens a PDF. It queues a job and waits for its
result:

| Kind | Pool | Does | Payload (ids only) |
|---|---|---|---|
| `ingest` | interactive | inspect and store an upload | `staging_id` (32 hex), `filename`, `document_id` |
| `render_page` | interactive | render a page to the PNG cache | `document_version`, `page_index` |
| `template` | interactive | crop a template, return its pixel sha256 | `document_version`, `page_index`, `box` |
| `scan` | scan | template matching on one page | `document_version`, `page_index`, `template {page_index, box, sha256}`, `settings` |
| `selftest` | interactive | sandbox probes for tests; never submitted by the web tier | `action` |

Training kinds (`build_dataset`, `train_verifier`, `train_detector`,
`benchmark`) join the table in Step 7, with the training pages that start
them.

**Queue.** `jobs.sqlite3`, states `queued` → `running` → `done` |
`failed` | `cancelled`. Workers claim the lowest priority number first,
then the oldest: single scans (priority 0) go ahead of batch pages
(10). Two pools, so an upload or a page image never waits behind a scan.
A render of the same page already queued or running is reused
(`dedupe_key`). Finished jobs are pruned after 7 days.

**Worker.** Claims a job, starts one child process for it, writes the
job to the child's stdin, heartbeats every 3 s, and kills the child's
process group at its wall-clock limit (`timeout`, 504) or when the job is
cancelled. A child that exits without an answer after a CPU kill is
`cpu_limit`; any other silent exit is `job_crashed`.

**Child** (`python -m pinny.jobs.child`), before it reads any file:

1. new user and network namespaces: no network, only a loopback
   interface;
2. `PR_SET_PDEATHSIG` (it dies with its worker; set after step 1 because
   a credential change clears it, and it exits if the worker is already
   gone) and `PR_SET_NO_NEW_PRIVS`;
3. limits: address space, CPU time, file size, 64 open files, no core
   dumps (section 5);
4. a minimal environment: no `PINNY_*`, cloud or other secrets, one
   thread per numeric library.

Every path is derived in the child from `PINNY_DATA_DIR` and a validated
id. Answers are one JSON line; only 4xx messages (written for users)
reach the client, and everything else becomes a generic message with the
detail in the job's log.

**Isolation is verified, not assumed.** In production the child refuses
to run (`sandbox_unavailable`) unless it has no network interface but
loopback. Ubuntu 24.04 restricts unprivileged user namespaces through
AppArmor, so on the EC2 instance the workers run as their own systemd
services with `PrivateNetwork=yes` (`PINNY_JOBS=external`;
`pinny-worker-<pool>@<profile>` in `deploy/systemd/`), and the child's
check passes whether or not its own `unshare` works. There the queue file
`$PINNY_DATA_DIR/jobs.sqlite3` is a link to `jobs/jobs.sqlite3`, so the
workers can write the queue without write access to the data directory
itself (SQLite follows the link and keeps `-wal`/`-shm` next to the target).

**Crash recovery.** A running job whose heartbeat is older than 30 s lost
its worker. Any worker requeues it (once), then fails it with
`worker_lost`. With `PINNY_JOBS=sandbox` the web process requeues
immediately on startup, since its own workers are the only ones. The
deployment stops worker services with SIGKILL (the instance is stopped
every evening), so an interrupted job is left `running` and requeued when
the workers start again, instead of being recorded as failed by the
worker's SIGTERM handling.

**Still in the web process:** decoding the PNG page images the jobs wrote
(for training crops and for the two learned-model scan modes, which also
run their models in-process). These images come from our own renderer,
not from the uploaded PDF. Moving the model modes into jobs belongs with
the training work in Step 7.

## 5. Limits (built unless noted)

| Limit | Value |
|---|---|
| Upload size | 200 MB, enforced while streaming |
| Pages per PDF | 500 |
| Raster size | 100 MP (render service) |
| JSON body | 1 MB |
| Web worker threads | 40 (Starlette/anyio default), not one per connection |
| Batch scans running at once | 1 (one page at a time) |
| Upload staging | removed on success and failure |
| Per-user quotas | not needed for 3 users |
| `ingest` job | 2 GB address space, 120 s CPU, 180 s wall |
| `render_page` job | 3 GB, 180 s CPU, 240 s wall |
| `template` job | 2 GB, 60 s CPU, 120 s wall |
| `scan` job | 4 GB, 300 s CPU, 360 s wall |
| Every job | 1 GB largest file, 64 open files, no network, 2 attempts if its worker dies |
| Workers (8 GB instance) | 1 interactive + 1 scan |

## 6. Audit events (built)

`site.sqlite3` table `audit(seq, at, actor, action, target, detail)`,
append-only (trigger). Actions: `member_added`, `member_role_changed`,
`member_removed`, `setup_link_issued`, `password_reset`, `password_set`,
`account_locked`, `all_sessions_ended`, `document_uploaded`,
`document_deleted`, `batch_started`. Review actions are already an append-only log in the
learning store (`review_events`) with the reviewer's email.

## 7. Deletion and retention (built)

`DELETE /api/documents/{version}` (uploader or admin):

1. removes `documents/<sha256>/` (the PDF, its metadata and cached page
   images) and drops the render cache;
2. deletes that version's training-crop files and marks their rows
   `document_deleted`, so they are never regenerated;
3. writes a tombstone (`deleted_documents`) and a `document_deleted` audit
   event.

Scans, pins and review events stay, because they are the labels; they
hold coordinates and scores, not drawing pixels. A deleted version reads
as `404` everywhere, and uploading the same bytes again creates it afresh.
Nothing else is deleted automatically. Backups (Step 5) keep 30 days, so a
deletion is complete everywhere after 30 days.

## 8. Threat model

| # (plan §2) | Threat | Fix | Status |
|---|---|---|---|
| 1 | Anyone with the URL sees drawings | Pinny's own sign-in (scrypt passwords, invite-only set-password links, hashed session tokens, lockout), members table, per-route roles | built |
| 2 | Global document list leaks across owners | one workspace of 3 trusted people; `uploaded_by` for deletion | built (single workspace) |
| 3 | Reviews under the server's OS account | signed-in email as reviewer | built |
| 4 | `http.server`: no TLS, slow clients, a thread per connection | uvicorn + Starlette, bounded thread pool, TLS and slow-client handling at Caddy, app listening only on 127.0.0.1 | built |
| 5 | Uploads held in memory | streamed to disk with the limit enforced while reading | built |
| 6 | Heavy work in web requests | rendering and template matching in job pools with limits | built |
| 7 | Untrusted PDFs parsed in the web process | ingest and rendering only in sandboxed children | built |
| 8 | CSRF | Origin and Content-Type checks | built |
| 9 | Missing security headers | CSP, HSTS, frame, referrer, nosniff | built |
| 10 | Internal errors reach clients | generic 500 with request id | built |
| 11 | Nothing can be deleted | delete endpoint, section 7 | built |
| 12 | Two default data dirs | one `PINNY_DATA_DIR` for the site | built |
| 13 | `git:unknown` versions in a deployed image | `PINNY_VERSION` baked in at build; required in production | built |
| 14 | CLI file-path arguments reachable from the web | no route takes a path; job payloads are ids validated in the child | built |
| 15 | Shared unsaved-edit queue across users | queue key includes the signed-in email | built |

Residual risks for this deployment: a member's email account being taken
over (mitigate with MFA on those mailboxes); a PDF parser exploit inside
a child can still read and write what the worker's Unix user can, so the
deployment runs the interactive and scan workers as their own user
(`pinny-<profile>-worker`) with write access only to `documents/`, `tmp/`
and the job queue (`jobs/`), and no read access to `site.sqlite3`,
`pinny.sqlite3`, `crops/`, `exports/` or `models/`; the training worker
needs the labels and models, so it runs as the web user (still without
network); the single EC2 instance as a single point of failure (mitigated
by daily weekday backups and weekly snapshots).

## 9. Seam between web and workers

`pinny/viewer/service.py` talks to the render service only through these
methods (`pinny/viewer/render_client.py`). `RenderService` implements them
in-process; `JobRenderClient` implements them with jobs and reads only
`version.json` and the PNG page cache:

```python
class RenderClient(Protocol):
    def ingest_pdf(self, source, *, original_filename=None, document_id=None) -> DocumentVersion: ...
    def get_version(self, document_version: str) -> DocumentVersion: ...
    def list_versions(self, document_id: str | None = None) -> list[DocumentVersion]: ...
    def page_frame(self, document_version: str, page_index: int) -> dict: ...
    def render_page(self, document_version: str, page_index: int) -> np.ndarray: ...
    def render_page_png(self, document_version: str, page_index: int) -> bytes: ...
    def crop_renderer(self, spec) -> bytes: ...
    def delete_version(self, document_version: str) -> None: ...
```

## 10. Configuration

| Variable | Development | Production |
|---|---|---|
| `PINNY_ENV` | `development` (default) | `production` |
| `PINNY_DATA_DIR` | optional | required |
| `PINNY_ORIGIN` | derived from the host | required, `https://...` |
| `PINNY_ADMIN_EMAILS` | optional | required for first start; each then needs `members setup-link` |
| `PINNY_VERSION` | from git | required (build-time) |
| `PINNY_JOBS` | `inprocess` (default) | `sandbox` (default) or `external`; `inprocess` is refused |
| Bind address | `127.0.0.1` only | `127.0.0.1`, behind Caddy on the same instance |

Production refuses to start if any required variable is missing. The
deployment keeps these in `/etc/pinny/<profile>.env` (plus `PINNY_PORT`,
the port the web unit listens on behind Caddy: 8001 production, 8002
staging) and writes `PINNY_VERSION` (`git:` and 12 hex digits) per
release (`docs/deployment.md`).
