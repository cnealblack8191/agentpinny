"""HTTP layer for the viewer and training site: a Starlette app served by
uvicorn (docs/training-site.md section 3). ``ViewerService`` stays the
service layer; this module adds identity, roles, CSRF checks, security
headers, streamed uploads and safe errors.

Every route declares a minimum role in ``ROUTES``; ``tests/viewer/test_site.py``
fails if one does not.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pinny.render.service import DEFAULT_MAX_UPLOAD_BYTES

from .auth import PUBLIC, Authenticator, Identity
from .errors import ViewerError
from .service import ViewerService
from .training import TrainingService
from pinny.jobs.queue import JobQueue
from pinny.jobs.worker import WorkerPool

from .settings import JOBS_INPROCESS, JOBS_SANDBOX, ConfigError, Settings
from .sitedb import ADMIN, REVIEWER, SiteDB

_log = logging.getLogger("pinny.viewer")

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
MAX_JSON_BYTES = 1024 * 1024

_VERSION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_JOB_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # job ids: lower case
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

CSP = ("default-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")

# (method, path, handler, minimum role, body kind). Body kind is what a
# state-changing request must send: "json", "pdf" or None (no body).
ROUTES = [
    ("GET", "/healthz", "healthz", PUBLIC, None),
    ("POST", "/api/login", "login", PUBLIC, "json"),
    ("POST", "/api/logout", "logout", PUBLIC, "json"),
    ("POST", "/api/setup/check", "setup_check", PUBLIC, "json"),
    ("POST", "/api/setup", "setup", PUBLIC, "json"),
    ("GET", "/api/me", "me", REVIEWER, None),
    ("POST", "/api/me/password", "change_password", REVIEWER, "json"),
    ("GET", "/api/health", "health", REVIEWER, None),
    ("GET", "/api/documents", "documents", REVIEWER, None),
    ("POST", "/api/documents", "upload", REVIEWER, "pdf"),
    ("DELETE", "/api/documents/{v}", "delete_document", REVIEWER, None),
    ("GET", "/api/documents/{v}/pages/{i:int}/frame", "frame", REVIEWER, None),
    ("GET", "/api/documents/{v}/pages/{i:int}/raster.png", "raster", REVIEWER, None),
    ("GET", "/api/documents/{v}/pages/{i:int}/scans", "page_scans", REVIEWER, None),
    ("GET", "/api/documents/{v}/batches", "document_batches", REVIEWER, None),
    # Legend workflow (docs/training-site.md section 3): read and check the
    # legend, scan the whole set with it, count.
    ("GET", "/api/documents/{v}/legend", "legend", REVIEWER, None),
    ("POST", "/api/documents/{v}/legend/read", "legend_read", REVIEWER, "json"),
    ("POST", "/api/documents/{v}/legend/edit", "legend_edit", REVIEWER, "json"),
    ("POST", "/api/documents/{v}/legend/confirm", "legend_confirm", REVIEWER, "json"),
    ("POST", "/api/documents/{v}/legend/save-standard", "legend_save_standard", REVIEWER, "json"),
    ("GET", "/api/legend-library", "legend_library", REVIEWER, None),
    # Optional OCR (docs/ocr.md): an admin turns it on; reviewers read sheet info.
    ("GET", "/api/ocr", "ocr_state", REVIEWER, None),
    ("POST", "/api/ocr", "ocr_set", ADMIN, "json"),
    ("GET", "/api/documents/{v}/pages/{i:int}/sheet-info", "sheet_info", REVIEWER, None),
    ("POST", "/api/documents/{v}/pages/{i:int}/sheet-info", "sheet_info_read", REVIEWER, "json"),
    ("GET", "/api/documents/{v}/set-scans", "set_scans", REVIEWER, None),
    ("POST", "/api/documents/{v}/set-scans", "set_scan_start", REVIEWER, "json"),
    ("GET", "/api/documents/{v}/counts", "counts", REVIEWER, None),
    ("GET", "/api/documents/{v}/counts.csv", "counts_csv", REVIEWER, None),
    ("GET", "/api/models", "models", REVIEWER, None),
    ("POST", "/api/scans", "scan", REVIEWER, "json"),
    ("GET", "/api/scans/{s}", "scan_state", REVIEWER, None),
    ("POST", "/api/scans/{s}/actions", "act", REVIEWER, "json"),
    ("GET", "/api/scans/{s}/report", "report", REVIEWER, None),
    ("POST", "/api/batches", "batch_start", REVIEWER, "json"),
    ("GET", "/api/batches/{b}", "batch_state", REVIEWER, None),
    ("GET", "/api/batches/{b}/queue", "batch_queue", REVIEWER, None),
    ("POST", "/api/batches/{b}/cancel", "batch_cancel", REVIEWER, "json"),
    ("POST", "/api/batches/{b}/resume", "batch_resume", REVIEWER, "json"),
    ("GET", "/api/members", "members", ADMIN, None),
    ("POST", "/api/members", "member_put", ADMIN, "json"),
    ("POST", "/api/members/remove", "member_remove", ADMIN, "json"),
    ("POST", "/api/members/setup-link", "member_setup_link", ADMIN, "json"),
    ("GET", "/api/audit", "audit", ADMIN, None),
    # Training site (docs/training-site.md section 3): labelling progress for
    # reviewers; datasets, training, benchmarks and models for admins.
    ("GET", "/api/training/dashboard", "training_dashboard", REVIEWER, None),
    ("GET", "/api/training/queue", "training_queue", REVIEWER, None),
    ("POST", "/api/training/pages/complete", "training_page_complete", REVIEWER, "json"),
    ("GET", "/api/training/models", "training_models", REVIEWER, None),
    ("GET", "/api/training/datasets", "training_datasets", ADMIN, None),
    ("POST", "/api/training/datasets", "training_build_dataset", ADMIN, "json"),
    ("POST", "/api/training/train", "training_train", ADMIN, "json"),
    ("POST", "/api/training/benchmarks", "training_benchmark", ADMIN, "json"),
    ("GET", "/api/training/jobs", "training_jobs", ADMIN, None),
    ("GET", "/api/training/jobs/{j}", "training_job", ADMIN, None),
    ("POST", "/api/training/jobs/{j}/cancel", "training_cancel", ADMIN, "json"),
    ("POST", "/api/training/promote", "training_promote", ADMIN, "json"),
    ("POST", "/api/training/deactivate", "training_deactivate", ADMIN, "json"),
    # Learned package per symbol type (docs/set-scanning.md "Learning each symbol type").
    ("GET", "/api/training/symbols", "training_symbols", REVIEWER, None),
    ("POST", "/api/training/symbols/train", "training_symbol_train", ADMIN, "json"),
    ("POST", "/api/training/symbols/active", "training_symbol_active", ADMIN, "json"),
    ("GET", "/{path:path}", "static", PUBLIC, None),
]


class SecurityHeaders:
    """ASGI middleware: a request id and the security headers on every
    response, including errors and static files."""

    def __init__(self, app, production: bool) -> None:
        self.app = app
        self.production = production

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = rid
        is_api = scope["path"].startswith("/api/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"server",)]
                have = {k.lower() for k, _ in headers}
                add = [(b"x-request-id", rid.encode()),
                       (b"content-security-policy", CSP.encode()),
                       (b"referrer-policy", b"no-referrer"),
                       (b"x-content-type-options", b"nosniff"),
                       (b"x-frame-options", b"DENY")]
                if self.production:
                    add.append((b"strict-transport-security", b"max-age=31536000"))
                if is_api and b"cache-control" not in have:
                    add.append((b"cache-control", b"no-store"))
                message = dict(message, headers=headers + [h for h in add if h[0] not in have])
            await send(message)

        await self.app(scope, receive, send_with_headers)


class Site:
    def __init__(self, service: ViewerService, settings: Settings,
                 authenticator: Optional[Authenticator] = None, web_root: Path = WEB_ROOT) -> None:
        self.service = service
        self.settings = settings
        self.sitedb = SiteDB(settings.data_dir)
        self.sitedb.ensure_admins(settings.admin_emails)
        self.auth = authenticator or Authenticator(settings, self.sitedb)
        self.web_root = web_root
        self.training = TrainingService(service)
        self.app = Starlette(
            routes=[Route(path, self._endpoint(method, name, role, body), methods=[method], name=name)
                    for method, path, name, role, body in ROUTES],
            exception_handlers={404: self._not_found, 405: self._not_allowed})
        self.app.add_middleware(SecurityHeaders, production=settings.production)

    # --------------------------------------------------------------- plumbing
    def _endpoint(self, method: str, name: str, role: str, body: Optional[str]) -> Callable:
        handler = getattr(self, "h_" + name)

        async def endpoint(request: Request) -> Response:
            rid = request.scope.get("state", {}).get("request_id", "")
            try:
                ident = None
                if role != PUBLIC:
                    ident = await run_in_threadpool(self.auth.identify, request.headers)
                    if not ident.allows(role):
                        raise ViewerError("forbidden", "Only an admin can do that.", 403)
                if method in ("POST", "DELETE"):
                    self._check_csrf(request, body)
                self._check_ids(request.path_params)
                if asyncio.iscoroutinefunction(handler):
                    return await handler(request, ident)
                payload = await self._json_body(request) if body == "json" else None
                return await run_in_threadpool(handler, request, ident, payload)
            except Exception as exc:  # noqa: BLE001 - mapped to a safe response
                return self._error(exc, rid)

        return endpoint

    def _check_csrf(self, request: Request, body: Optional[str]) -> None:
        expected = self.settings.origin
        if not expected:  # development: the page's own origin
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.headers.get("origin") != expected:
            raise ViewerError("bad_origin", "This request did not come from the Pinny page.", 403)
        if body is not None:
            want = {"json": "application/json", "pdf": "application/pdf"}[body]
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != want:
                raise ViewerError("unsupported_media_type", f"Send this request as {want}.", 415)

    @staticmethod
    def _check_ids(params: dict) -> None:
        """Malformed ids read as not found, before the service is called."""
        if "v" in params and not _VERSION_RE.match(params["v"]):
            raise ViewerError("document_version_not_found", "That document does not exist.", 404)
        if "s" in params and not _UUID_RE.match(params["s"]):
            raise ViewerError("unknown_scan", "That scan does not exist.", 404)
        if "b" in params and not _UUID_RE.match(params["b"]):
            raise ViewerError("unknown_batch", "That batch does not exist.", 404)
        if "j" in params and not _JOB_RE.match(params["j"]):
            raise ViewerError("unknown_job", "That job does not exist.", 404)

    @staticmethod
    async def _read_limited(request: Request, limit: int, code: str, message: str) -> bytes:
        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            raise ViewerError("bad_request", "Invalid Content-Length.") from None
        if declared > limit:
            raise ViewerError(code, message, 413)
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                raise ViewerError(code, message, 413)
        return bytes(buf)

    async def _json_body(self, request: Request) -> dict:
        raw = await self._read_limited(request, MAX_JSON_BYTES, "body_too_large", "Request body is too large.")
        try:
            b = json.loads(raw or b"{}")
        except ValueError:
            raise ViewerError("bad_json", "Request body is not valid JSON.") from None
        if not isinstance(b, dict):
            raise ViewerError("bad_json", "Request body must be a JSON object.")
        return b

    def _error(self, exc: Exception, rid: str) -> JSONResponse:
        status = getattr(exc, "status", None)
        if not isinstance(status, int):
            status = getattr(exc, "http_status", None)
        code = getattr(exc, "code", None)
        if isinstance(code, str) and isinstance(status, int) and 400 <= status < 500:
            message = getattr(exc, "message", None) or str(exc)
        else:
            _log.exception("request %s failed", rid, exc_info=exc)
            status, code = 500, "internal_error"
            message = f"Something went wrong on the server (request {rid}). Please try again."
        return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}}, status)

    async def _not_found(self, request: Request, exc) -> JSONResponse:
        rid = request.scope.get("state", {}).get("request_id", "")
        return JSONResponse({"error": {"code": "not_found", "message": "Not found.", "request_id": rid}}, 404)

    async def _not_allowed(self, request: Request, exc) -> JSONResponse:
        rid = request.scope.get("state", {}).get("request_id", "")
        return JSONResponse({"error": {"code": "method_not_allowed", "message": "Method not allowed.",
                                       "request_id": rid}}, 405)

    @staticmethod
    def _json(obj: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(obj, status)

    # --------------------------------------------------------------- handlers
    def h_healthz(self, request, ident, body):
        return self._json({"ok": True, "version": self.settings.version})

    # ---------------------------------------------------------------- sign-in
    def _signed_in(self, ident: Identity, status: int = 200) -> JSONResponse:
        resp = self._json({"email": ident.email, "role": ident.role}, status)
        resp.set_cookie(self.auth.cookie_name, ident.session, max_age=self.auth.session_max_age,
                        **self.auth.cookie_attrs())
        return resp

    def _require_login(self) -> None:
        if not self.auth.enabled:
            raise ViewerError("no_sign_in", "This server runs in development mode without sign-in.", 404)

    def h_login(self, request, ident, b: dict):
        self._require_login()
        client = request.client.host if request.client else "unknown"
        return self._signed_in(self.auth.login(b.get("email"), b.get("password"), client))

    def h_logout(self, request, ident, b):
        self.auth.logout(request.headers)
        resp = self._json({"signed_out": True})
        resp.delete_cookie(self.auth.cookie_name, **self.auth.cookie_attrs())
        return resp

    def h_setup_check(self, request, ident, b: dict):
        self._require_login()
        return self._json({"email": self.auth.setup_email(b.get("token"))})

    def h_setup(self, request, ident, b: dict):
        self._require_login()
        return self._signed_in(self.auth.complete_setup(b.get("token"), b.get("password")))

    def h_change_password(self, request, ident: Identity, b: dict):
        self._require_login()
        self.auth.change_password(ident, b.get("current_password"), b.get("new_password"))
        return self._json({"changed": True})

    def h_me(self, request, ident: Identity, body):
        return self._json({"email": ident.email, "role": ident.role, "env": self.settings.env,
                           "sign_in": self.auth.enabled, "version": self.settings.version})

    def h_health(self, request, ident, body):
        return self._json({"ok": True, "version": self.settings.version,
                           "render_service": getattr(self.service.render, "render_service", "foundation")})

    def _doc_with_owner(self, d: dict) -> dict:
        return dict(d, uploaded_by=self.sitedb.uploaded_by(d["document_version"]))

    def h_documents(self, request, ident, body):
        return self._json({"documents": [self._doc_with_owner(d) for d in self.service.documents()]})

    async def h_upload(self, request: Request, ident: Identity) -> Response:
        limit = getattr(self.service.render, "max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES)
        too_big = f"The PDF is larger than the {limit // (1024 * 1024)} MB limit."
        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            raise ViewerError("bad_request", "Invalid Content-Length.") from None
        if declared > limit:
            raise ViewerError("upload_too_large", too_big, 413)
        tmp_dir = self.settings.data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix="upload_", suffix=".part")
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:  # streamed: never the whole file in memory
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise ViewerError("upload_too_large", too_big, 413)
                    out.write(chunk)
            filename = request.query_params.get("filename", "upload.pdf")
            doc = await run_in_threadpool(self.service.upload, tmp, filename,
                                          request.query_params.get("document_id") or None)
        finally:
            Path(tmp).unlink(missing_ok=True)
        await run_in_threadpool(self.sitedb.record_upload, doc["document_version"], ident.email,
                                doc.get("filename") or "")
        return self._json(self._doc_with_owner(doc), 201)

    def h_delete_document(self, request, ident: Identity, body):
        v = request.path_params["v"]
        owner = self.sitedb.uploaded_by(v)
        if ident.role != ADMIN and owner != ident.email:
            info = self.service.document_info(v)  # 404 first, so existence is not hidden from members
            raise ViewerError("forbidden", f"Only the person who uploaded {info['filename']} or an admin "
                              "can delete it.", 403)
        out = self.service.delete_document(v)
        self.sitedb.record_deletion(v, out.get("document_id"), out.get("filename"), ident.email)
        return self._json(out)

    def h_frame(self, request, ident, body):
        p = request.path_params
        return self._json(self.service.frame(p["v"], p["i"]))

    def h_raster(self, request, ident, body):
        p = request.path_params
        png = self.service.raster_png(p["v"], p["i"])
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    def h_page_scans(self, request, ident, body):
        p = request.path_params
        return self._json({"scans": self.service.page_scans(p["v"], p["i"])})

    def h_document_batches(self, request, ident, body):
        return self._json({"batches": self.service.document_batches(request.path_params["v"])})

    # ---------------------------------------------------------------- legend
    def h_legend(self, request, ident, body):
        return self._json(self.service.legend.state(request.path_params["v"]))

    def h_legend_read(self, request, ident: Identity, b: dict):
        v = request.path_params["v"]
        out = self.service.legend.read(v, b.get("page_index"), requested_by=ident.email)
        self.sitedb.audit(ident.email, "legend_read", v, {"page_index": out["legend"]["page_index"]})
        return self._json(out)

    def h_legend_edit(self, request, ident: Identity, b: dict):
        return self._json(self.service.legend.edit(request.path_params["v"], b, reviewer=ident.email))

    def h_legend_confirm(self, request, ident: Identity, b: dict):
        v = request.path_params["v"]
        out = self.service.legend.confirm(v, b, reviewer=ident.email)
        self.sitedb.audit(ident.email, "legend_confirmed", v, {"version": out["legend"]["version"]})
        return self._json(out)

    def h_legend_save_standard(self, request, ident: Identity, b: dict):
        return self._json(self.service.legend.save_standard(request.path_params["v"], b, reviewer=ident.email))

    def h_legend_library(self, request, ident, body):
        return self._json(self.service.legend.library())

    # ------------------------------------------------------------------ ocr
    def h_ocr_state(self, request, ident, body):
        return self._json(self.service.ocr.state())

    def h_ocr_set(self, request, ident: Identity, b: dict):
        out = self.service.ocr.set_engine(b.get("engine"))
        self.sitedb.audit(ident.email, "ocr_engine_set", None, {"engine": out["engine"]})
        return self._json(out)

    def h_sheet_info(self, request, ident, body):
        p = request.path_params
        return self._json({"sheet_info": self.service.ocr.saved(p["v"], p["i"]),
                           "ocr": {"enabled": self.service.ocr.enabled()}})

    def h_sheet_info_read(self, request, ident: Identity, b: dict):
        p = request.path_params
        out = self.service.ocr.read(p["v"], p["i"], b.get("region"), requested_by=ident.email)
        self.sitedb.audit(ident.email, "sheet_info_read", p["v"], {"page_index": p["i"]})
        return self._json({"sheet_info": out})

    def h_set_scans(self, request, ident, body):
        v = request.path_params["v"]
        self.service.document_info(v)  # 404s for an unknown version
        return self._json({"set_scans": self.service.legend.runs(v)})

    def h_set_scan_start(self, request, ident: Identity, b: dict):
        v = request.path_params["v"]
        out = self.service.legend.start_scan(v, b.get("request_id"), requested_by=ident.email)
        self.sitedb.audit(ident.email, "set_scan_started", out["run_id"],
                          {"document_version": v, "sheets": out["sheets_total"]})
        return self._json(out, 202)

    def h_counts(self, request, ident, body):
        return self._json(self.service.legend.counts(request.path_params["v"]))

    def h_counts_csv(self, request, ident, body):
        data, name = self.service.legend.counts_csv(request.path_params["v"])
        return Response(data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def h_models(self, request, ident, body):
        return self._json(self.service.models())

    def h_scan(self, request, ident: Identity, b: dict):
        mode = b.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise ViewerError("invalid_mode", "mode must be a string.")
        page_index = b.get("page_index")
        if isinstance(page_index, bool) or not isinstance(page_index, int):
            raise ViewerError("invalid_page", "page_index must be an integer.")
        if not isinstance(b.get("document_version"), str):
            raise ViewerError("invalid_document", "document_version is required.")
        return self._json(self.service.scan(document_version=b["document_version"], page_index=page_index,
                                            template_box=b.get("template_box"),
                                            request_id=b.get("request_id"), threshold=b.get("threshold"),
                                            mode=mode, model_threshold=b.get("model_threshold"),
                                            requested_by=ident.email), 201)

    def h_scan_state(self, request, ident, body):
        return self._json(self.service.scan_state(request.path_params["s"]))

    def h_act(self, request, ident: Identity, b: dict):
        reviewer = ident.email if self.auth.enabled else None
        return self._json(self.service.act(request.path_params["s"], b, reviewer=reviewer))

    def h_report(self, request, ident, body):
        s = request.path_params["s"]
        doc = self.service.report(s)
        return Response(json.dumps(doc, indent=2, sort_keys=True).encode(), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="pinny-report-{s}.json"'})

    def h_batch_start(self, request, ident: Identity, b: dict):
        mode = b.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise ViewerError("invalid_mode", "mode must be a string.")
        if not isinstance(b.get("document_version"), str):
            raise ViewerError("invalid_document", "document_version is required.")
        out = self.service.start_batch(document_version=b["document_version"],
                                       request_id=b.get("request_id"), template_box=b.get("template_box"),
                                       template_page_index=b.get("template_page_index"),
                                       page_indexes=b.get("page_indexes"), mode=mode,
                                       threshold=b.get("threshold"), model_threshold=b.get("model_threshold"),
                                       requested_by=ident.email)
        self.sitedb.audit(ident.email, "batch_started", out["batch_id"],
                          {"document_version": b["document_version"], "pages": out["page_counts"]["total"]})
        return self._json(out, 202)

    def h_batch_state(self, request, ident, body):
        return self._json(self.service.batch_state(request.path_params["b"]))

    def h_batch_queue(self, request, ident, body):
        limit = request.query_params.get("limit")
        if limit is not None:
            if not limit.isdigit():
                raise ViewerError("invalid_limit", "limit must be a non-negative integer.")
            limit = int(limit)
        return self._json(self.service.batch_queue(request.path_params["b"],
                                                   request.query_params.get("strategy"), limit))

    def h_batch_cancel(self, request, ident, b):
        return self._json(self.service.cancel_batch(request.path_params["b"]))

    def h_batch_resume(self, request, ident, b):
        retry = b.get("retry_failed", False)
        if not isinstance(retry, bool):
            raise ViewerError("invalid_retry", "retry_failed must be true or false.")
        return self._json(self.service.resume_batch(request.path_params["b"], retry_failed=retry))

    def _member_dict(self, m) -> dict:
        return dict(m.to_dict(), has_password=self.sitedb.has_password(m.email))

    def _setup_link(self, request, email: str, actor: str, reset: bool) -> dict:
        token, expires = self.sitedb.issue_setup_link(email, actor=actor, now=self.auth.clock(), reset=reset)
        origin = self.settings.origin or f"{request.url.scheme}://{request.headers.get('host', '')}"
        # The token rides in the fragment, which browsers never send to a
        # server, so it stays out of access logs and Referer headers.
        return {"email": email, "setup_url": f"{origin}/setup.html#token={token}",
                "expires_at": _dt.datetime.fromtimestamp(expires, _dt.timezone.utc).isoformat(timespec="seconds")}

    def h_members(self, request, ident, body):
        return self._json({"members": [self._member_dict(m) for m in self.sitedb.members()]})

    def h_member_put(self, request, ident: Identity, b: dict):
        try:
            m = self.sitedb.put_member(b.get("email"), b.get("role"), actor=ident.email)
        except ValueError as exc:
            raise ViewerError("invalid_member", str(exc)) from None
        out = self._member_dict(m)
        if self.auth.enabled and not out["has_password"]:
            out["setup"] = self._setup_link(request, m.email, ident.email, reset=False)
        return self._json(out)

    def h_member_setup_link(self, request, ident: Identity, b: dict):
        """A new one-time set-password link. ``reset: true`` also removes the
        current password and signs that member out everywhere."""
        self._require_login()
        reset = b.get("reset", False)
        if not isinstance(reset, bool):
            raise ViewerError("invalid_reset", "reset must be true or false.")
        email = b.get("email")
        if reset and isinstance(email, str) and email.strip().lower() == ident.email:
            raise ViewerError("invalid_member", "Change your own password from your account instead.")
        try:
            return self._json(self._setup_link(request, email, ident.email, reset=reset))
        except ValueError as exc:
            raise ViewerError("invalid_member", str(exc)) from None

    def h_member_remove(self, request, ident: Identity, b: dict):
        email = b.get("email")
        if isinstance(email, str) and email.strip().lower() == ident.email:
            raise ViewerError("invalid_member", "You cannot remove yourself.")
        try:
            removed = self.sitedb.remove_member(email, actor=ident.email)
        except ValueError as exc:
            raise ViewerError("invalid_member", str(exc)) from None
        if not removed:
            raise ViewerError("unknown_member", "That person is not a member.", 404)
        return self._json({"removed": email.strip().lower()})

    def h_audit(self, request, ident, body):
        limit = request.query_params.get("limit", "100")
        if not limit.isdigit():
            raise ViewerError("invalid_limit", "limit must be a non-negative integer.")
        return self._json({"events": self.sitedb.audit_log(min(int(limit), 1000))})

    # --------------------------------------------------------------- training
    @staticmethod
    def _limit(request, default: int, most: int) -> int:
        limit = request.query_params.get("limit", str(default))
        if not limit.isdigit():
            raise ViewerError("invalid_limit", "limit must be a non-negative integer.")
        return min(int(limit), most)

    def h_training_dashboard(self, request, ident, body):
        return self._json(self.training.dashboard())

    def h_training_queue(self, request, ident, body):
        return self._json(self.training.review_queue(self._limit(request, 50, 500)))

    def h_training_page_complete(self, request, ident: Identity, b: dict):
        return self._json(self.training.mark_page_reviewed(b.get("document_version"), b.get("page_index"),
                                                           reviewer=ident.email))

    def h_training_models(self, request, ident, body):
        return self._json(self.training.models())

    def h_training_datasets(self, request, ident, body):
        return self._json(self.training.datasets())

    def _started(self, ident: Identity, action: str, job: dict) -> JSONResponse:
        self.sitedb.audit(ident.email, action, job["job_id"], {"kind": job["kind"], "payload": job["payload"]})
        return self._json(job, 202)

    def h_training_build_dataset(self, request, ident: Identity, b: dict):
        return self._started(ident, "dataset_build_started", self.training.build_dataset(ident.email))

    def h_training_train(self, request, ident: Identity, b: dict):
        return self._started(ident, "training_started", self.training.train(b, ident.email))

    def h_training_benchmark(self, request, ident: Identity, b: dict):
        return self._started(ident, "benchmark_started", self.training.benchmark(b, ident.email))

    def h_training_jobs(self, request, ident, body):
        return self._json(self.training.list_jobs(self._limit(request, 50, 500)))

    def h_training_job(self, request, ident, body):
        return self._json(self.training.job(request.path_params["j"]))

    def h_training_cancel(self, request, ident: Identity, b: dict):
        out = self.training.cancel(request.path_params["j"])
        self.sitedb.audit(ident.email, "training_cancelled", out["job_id"], {"kind": out["kind"]})
        return self._json(out)

    def h_training_promote(self, request, ident: Identity, b: dict):
        out = self.training.promote(b, ident.email)
        self.sitedb.audit(ident.email, "model_promoted", out["model_id"],
                          {"kind": out["kind"], "benchmark_job_id": out["benchmark_job_id"],
                           "evidence_sha256": out["evidence_sha256"]})
        return self._json(out)

    def h_training_deactivate(self, request, ident: Identity, b: dict):
        out = self.training.deactivate(b)
        if out["deactivated"]:
            self.sitedb.audit(ident.email, "model_deactivated", out["deactivated"], {"kind": out["kind"]})
        return self._json(out)

    def h_training_symbols(self, request, ident, body):
        return self._json(self.training.symbols())

    def h_training_symbol_train(self, request, ident: Identity, b: dict):
        return self._started(ident, "symbol_training_started", self.training.train_symbol(b, ident.email))

    def h_training_symbol_active(self, request, ident: Identity, b: dict):
        out = self.training.set_symbol_active(b)
        self.sitedb.audit(ident.email, "symbol_model_switched", out["tag"], {"active": out["active"]})
        return self._json(out)

    def h_static(self, request, ident, body):
        path = request.path_params.get("path", "")
        if path.startswith("api/") or path == "api":
            raise ViewerError("not_found", f"No route for {request.method} /{path}.", 404)
        rel = path or "index.html"
        root = self.web_root.resolve()
        target = (root / rel).resolve()
        if (root not in target.parents and target != root) or not target.is_file():
            raise ViewerError("not_found", "Not found.", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".js", ".mjs"):
            ctype = "text/javascript"
        return Response(target.read_bytes(), media_type=ctype, headers={"Cache-Control": "no-cache"})


class SiteServer:
    """uvicorn serving a ``Site`` on an already bound socket, with the small
    interface the tests and ``main`` use (``serve_forever``, ``shutdown``)."""

    def __init__(self, site: Site, host: str, port: int, verbose: bool = False) -> None:
        self.site = site
        self.verbose = verbose
        self._sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(128)  # connections queue until uvicorn's loop starts accepting
        self.server_address = self._sock.getsockname()
        config = uvicorn.Config(site.app, log_level="info" if verbose else "warning",
                                access_log=verbose, server_header=False, timeout_keep_alive=5,
                                limit_concurrency=200, lifespan="off",
                                # Caddy on the same host: trust its client address, no one else's.
                                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
        self._server = uvicorn.Server(config)
        self._stopped = threading.Event()

    def serve_forever(self) -> None:
        try:
            self._server.run(sockets=[self._sock])
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._stopped.wait(10)

    def server_close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def make_server(service: ViewerService, host: str = "127.0.0.1", port: int = 8765,
                verbose: bool = False, *, settings: Optional[Settings] = None,
                authenticator: Optional[Authenticator] = None) -> SiteServer:
    settings = settings or Settings.from_env(data_dir=service.data_dir)
    settings.check_bind(host)
    return SiteServer(Site(service, settings, authenticator), host, port, verbose)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pinny.viewer",
                                 description="Pinny viewer and training site.")
    ap.add_argument("--data-dir", help="defaults to $PINNY_DATA_DIR or ~/.local/share/pinny")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.from_env(data_dir=args.data_dir)
        settings.check_bind(args.host)
    except ConfigError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2
    jobs, workers = None, None
    if settings.jobs != JOBS_INPROCESS:
        jobs = JobQueue(settings.data_dir)
        if settings.jobs == JOBS_SANDBOX:
            # Our workers are the only ones, and none is running yet: requeue
            # what a restart cut off. (External workers recover their own.)
            jobs.recover(stale_after=0)
            workers = WorkerPool(jobs, settings.data_dir, require_isolation=settings.production).start()
    service = ViewerService(settings.data_dir, version=settings.version, jobs=jobs)
    resumed = service.resume_interrupted_batches()
    if resumed:
        print(f"Resumed {len(resumed)} interrupted batch scan(s).", flush=True)
    resumed = service.legend.resume_interrupted()
    if resumed:
        print(f"Resumed {len(resumed)} interrupted whole-set scan(s).", flush=True)
    httpd = make_server(service, args.host, args.port, args.verbose, settings=settings)
    host, port = httpd.server_address[:2]
    print(f"Pinny viewer on http://{host}:{port}/  ({settings.env}, jobs: {settings.jobs}, "
          f"data: {service.data_dir}, version {settings.version})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        service.close()
        if workers is not None:
            workers.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
