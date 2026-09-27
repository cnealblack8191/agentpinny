# Pinny training site: contract

Status: v1, 2026-09-27. This is the contract every training-site session
builds against. It follows the plan in `docs/training-site-plan.md`, whose
section 1 holds the server facts: one EC2 instance in the owner's AWS
account, Cloudflare Tunnel and Cloudflare Access in front, three users,
backups to S3. Anything here that contradicts `docs/contracts.md` sections
1-6 is a mistake; those still govern documents, coordinates, scans and
reviews.

Implementation status is marked per section: **built** (on
`claude/beautiful-brahmagupta-vqz21o`), or **planned** (Step 4 jobs and
sandbox, Step 5 deployment, Step 7 training pages).

## 1. Identity and roles (built)

**Sign-in happens at the gate, never in Pinny.** Cloudflare Access sits in
front of the site with an allow-list of email addresses and one-time email
codes. Pinny never sees a password.

On every request the gate adds the `Cf-Access-Jwt-Assertion` header, a
JWT signed by the Cloudflare team's keys. Pinny verifies it on every
request:

* algorithm `RS256`, key chosen by `kid` from
  `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (cached; an
  unknown `kid` triggers one refetch);
* `aud` contains the application's audience tag (`PINNY_CF_AUD`);
* `iss` is `https://<team>.cloudflareaccess.com` (`PINNY_CF_TEAM_DOMAIN`);
* `exp` and `nbf` hold, with 60 s leeway;
* the `email` claim is present. It is lower-cased and becomes the user id.

A request with no token, or a token that fails any check, gets
`401 unauthenticated`. The `CF_Authorization` cookie is not read; the
header is the only source.

**Members.** Passing the gate is not enough. Pinny keeps its own members
table, and a verified email that is not an active member gets
`403 not_a_member`. So an address added to Cloudflare by mistake still
sees nothing.

| Role | Can |
|---|---|
| `admin` | everything a reviewer can, plus: add, change and remove members; delete any document; read the audit log; (Step 7) train, promote and deactivate models |
| `reviewer` | upload, scan, batch scan, review pins, export reports, delete documents they uploaded |

Every route declares a minimum role (`public`, `reviewer` or `admin`). A
test fails if any route has none. `public` is only `GET /healthz`.

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
| `GET /api/me` | reviewer | | `{"email", "role", "sign_out_url"}` |
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
| `GET /api/members` | admin | | `[{email, role, added_at, added_by}]` |
| `POST /api/members` | admin | `{email, role}` | add or change role |
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

## 4. Jobs (planned, Step 4)

Today scans run on the web process's threads (single scans on the request
thread, batches on one background worker) and PDFs are opened by the web
process. Step 4 moves ingest, render, scan and training into sandboxed
worker subprocesses behind the `RenderClient` seam (section 9): a SQLite
job queue, one subprocess per job with memory, CPU-time and wall-clock
limits and no network, cancellation, and recovery of orphaned jobs on
restart. Payloads carry ids only, never client-supplied paths. Kinds:
`ingest`, `render_page`, `scan`, `batch_page`, `build_dataset`,
`train_verifier`, `train_detector`, `benchmark`.

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
| Per-user quotas | planned (Step 4); not needed for 3 users |

## 6. Audit events (built)

`site.sqlite3` table `audit(seq, at, actor, action, target, detail)`,
append-only (trigger). Actions: `member_added`, `member_role_changed`,
`member_removed`, `document_uploaded`, `document_deleted`,
`batch_started`. Review actions are already an append-only log in the
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
| 1 | Anyone with the URL sees drawings | Cloudflare Access + JWT verification + members table + per-route roles | built |
| 2 | Global document list leaks across owners | one workspace of 3 trusted people; `uploaded_by` for deletion | built (single workspace) |
| 3 | Reviews under the server's OS account | signed-in email as reviewer | built |
| 4 | `http.server`: no TLS, slow clients, a thread per connection | uvicorn + Starlette, bounded thread pool, TLS at Cloudflare, app bound to localhost | built |
| 5 | Uploads held in memory | streamed to disk with the limit enforced while reading | built |
| 6 | Heavy work in web requests | batches on one background worker; full move to workers | Step 4 |
| 7 | Untrusted PDFs parsed in the web process | sandboxed workers | Step 4 |
| 8 | CSRF | Origin and Content-Type checks | built |
| 9 | Missing security headers | CSP, HSTS, frame, referrer, nosniff | built |
| 10 | Internal errors reach clients | generic 500 with request id | built |
| 11 | Nothing can be deleted | delete endpoint, section 7 | built |
| 12 | Two default data dirs | one `PINNY_DATA_DIR` for the site | built |
| 13 | `git:unknown` versions in a deployed image | `PINNY_VERSION` baked in at build; required in production | built |
| 14 | CLI file-path arguments reachable from the web | no route takes a path; jobs take ids | built / Step 4 |
| 15 | Shared unsaved-edit queue across users | queue key includes the signed-in email | built |

Residual risks for this deployment: a member's email account being taken
over (mitigate with MFA on those mailboxes); a PDF parser exploit until
Step 4; the single EC2 instance as a single point of failure (mitigated by
nightly backups and weekly snapshots).

## 9. Seam between web and workers

`pinny/viewer/service.py` talks to the render service only through these
methods, which Step 4 re-implements as a job-backed client with the same
signatures:

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
| `PINNY_CF_TEAM_DOMAIN` | unused | required, e.g. `acme.cloudflareaccess.com` |
| `PINNY_CF_AUD` | unused | required (the Access application's AUD tag) |
| `PINNY_ADMIN_EMAILS` | optional | required for first start |
| `PINNY_VERSION` | from git | required (build-time) |
| Bind address | `127.0.0.1` only | `127.0.0.1` (cloudflared connects locally) |

Production refuses to start if any required variable is missing.
