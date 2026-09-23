"""Local HTTP server for the viewer: JSON API plus the static front end in
``web/``. Standard library only; single-user, bound to localhost by default.

Routes (all JSON unless noted; errors are ``{"error": {"code", "message"}}``):

  GET  /api/health
  GET  /api/documents
  POST /api/documents?filename=..&document_id=..     body: PDF bytes
  GET  /api/documents/{version}/pages/{i}/frame
  GET  /api/documents/{version}/pages/{i}/raster.png  (image/png)
  GET  /api/documents/{version}/pages/{i}/scans
  POST /api/scans            {document_version, page_index, template_box, request_id, threshold?}
  GET  /api/scans/{scan_id}
  POST /api/scans/{scan_id}/actions  {action, request_id, pin_id?, x?, y?, expected_version?}
  GET  /api/scans/{scan_id}/report   (attachment)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ViewerError
from .service import ViewerService

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
MAX_JSON_BYTES = 1024 * 1024

_PAGE = r"/api/documents/(?P<v>[^/]+)/pages/(?P<i>\d+)"
_ROUTES = [
    ("GET", r"/api/health", "health"),
    ("GET", r"/api/documents", "documents"),
    ("POST", r"/api/documents", "upload"),
    ("GET", _PAGE + r"/frame", "frame"),
    ("GET", _PAGE + r"/raster\.png", "raster"),
    ("GET", _PAGE + r"/scans", "page_scans"),
    ("POST", r"/api/scans", "scan"),
    ("GET", r"/api/scans/(?P<s>[^/]+)", "scan_state"),
    ("POST", r"/api/scans/(?P<s>[^/]+)/actions", "act"),
    ("GET", r"/api/scans/(?P<s>[^/]+)/report", "report"),
]
_COMPILED = [(m, re.compile(p + r"$"), n) for m, p, n in _ROUTES]


class Handler(BaseHTTPRequestHandler):
    service: ViewerService  # set on the subclass by make_server
    web_root: Path = WEB_ROOT
    server_version = "PinnyViewer/1"

    def log_message(self, fmt, *args):  # quieter default logging
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path
        if not path.startswith("/api/"):
            if method == "GET":
                return self._static(path)
            return self._error(ViewerError("not_found", "Not found.", 404))
        for m, rx, name in _COMPILED:
            match = rx.match(path)
            if match and m == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                try:
                    return getattr(self, "h_" + name)(params, query)
                except ViewerError as exc:
                    return self._error(exc)
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "code", None)
                    if isinstance(code, str):  # a PinnyError from another module
                        return self._error(ViewerError(code, str(exc)))
                    traceback.print_exc()
                    return self._error(ViewerError("internal_error",
                                                   "Unexpected server error; see the server log.",
                                                   500))
        self._error(ViewerError("not_found", f"No route for {method} {path}.", 404))

    # ------------------------------------------------------------- handlers
    def h_health(self, p, q):
        self._json({"ok": True, "render_service":
                    getattr(self.service.render, "render_service", "foundation")})

    def h_documents(self, p, q):
        self._json({"documents": self.service.documents()})

    def h_upload(self, p, q):
        data = self._body(limit=None)
        self._json(self.service.upload(data, q.get("filename", "upload.pdf"),
                                       document_id=q.get("document_id") or None), 201)

    def h_frame(self, p, q):
        self._json(self.service.frame(p["v"], int(p["i"])))

    def h_raster(self, p, q):
        png = self.service.raster_png(p["v"], int(p["i"]))
        self._send(200, png, "image/png", extra={"Cache-Control": "private, max-age=3600"})

    def h_page_scans(self, p, q):
        self._json({"scans": self.service.page_scans(p["v"], int(p["i"]))})

    def h_scan(self, p, q):
        b = self._json_body()
        page_index = b.get("page_index")
        if isinstance(page_index, bool) or not isinstance(page_index, int):
            raise ViewerError("invalid_page", "page_index must be an integer.")
        if not isinstance(b.get("document_version"), str):
            raise ViewerError("invalid_document", "document_version is required.")
        self._json(self.service.scan(document_version=b["document_version"],
                                     page_index=page_index,
                                     template_box=b.get("template_box"),
                                     request_id=b.get("request_id"),
                                     threshold=b.get("threshold")), 201)

    def h_scan_state(self, p, q):
        self._json(self.service.scan_state(p["s"]))

    def h_act(self, p, q):
        self._json(self.service.act(p["s"], self._json_body()))

    def h_report(self, p, q):
        doc = self.service.report(p["s"])
        body = json.dumps(doc, indent=2, sort_keys=True).encode()
        self._send(200, body, "application/json", extra={
            "Content-Disposition": f'attachment; filename="pinny-report-{p["s"]}.json"'})

    # -------------------------------------------------------------- helpers
    def _body(self, limit: Optional[int]) -> bytes:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ViewerError("bad_request", "Invalid Content-Length.") from None
        if limit is not None and n > limit:
            raise ViewerError("body_too_large", "Request body is too large.", 413)
        from .render_stub import MAX_UPLOAD_BYTES
        if n > MAX_UPLOAD_BYTES:
            raise ViewerError("upload_too_large", "The file is too large.", 413)
        return self.rfile.read(n) if n > 0 else b""

    def _json_body(self) -> dict:
        raw = self._body(limit=MAX_JSON_BYTES)
        try:
            b = json.loads(raw or b"{}")
        except ValueError:
            raise ViewerError("bad_json", "Request body is not valid JSON.") from None
        if not isinstance(b, dict):
            raise ViewerError("bad_json", "Request body must be a JSON object.")
        return b

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json",
                   extra={"Cache-Control": "no-store"})

    def _error(self, exc: ViewerError) -> None:
        self._json({"error": {"code": exc.code, "message": exc.message}}, exc.status)

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else unquote(path).lstrip("/")
        root = self.web_root.resolve()
        target = (root / rel).resolve()
        if root not in target.parents and target != root or not target.is_file():
            return self._error(ViewerError("not_found", "Not found.", 404))
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js" or target.suffix == ".mjs":
            ctype = "text/javascript"
        self._send(200, target.read_bytes(), ctype, extra={"Cache-Control": "no-cache"})


def make_server(service: ViewerService, host: str = "127.0.0.1", port: int = 8765,
                verbose: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pinny.viewer",
                                 description="Pinny viewer and pin review (local).")
    ap.add_argument("--data-dir", help="defaults to $PINNY_DATA_DIR or ~/.local/share/pinny")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    service = ViewerService(args.data_dir)
    httpd = make_server(service, args.host, args.port, args.verbose)
    host, port = httpd.server_address[:2]
    print(f"Pinny viewer on http://{host}:{port}/  (data: {service.data_dir}, "
          f"render service: {getattr(service.render, 'render_service', 'foundation')})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        service.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
