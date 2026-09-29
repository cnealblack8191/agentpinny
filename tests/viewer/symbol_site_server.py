"""Server for tests/viewer/test_symbols_browser.mjs: the real site in
production mode (Pinny sign-in) with a drawing set scanned with its legend
and two sheets reviewed (receptacles approved, crossed-out ones rejected).
Training started from the Symbol types page runs in the development
in-process runner. Prints one JSON line: base URL, set-password tokens and
the document version.

    python tests/viewer/symbol_site_server.py <work dir>
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "viewer"))

from pinny.viewer import ViewerService  # noqa: E402
from pinny.viewer.server import Site, SiteServer  # noqa: E402
from pinny.viewer.settings import Settings  # noqa: E402
from tests.legend.pdfgen import Canvas, build, draw_legend  # noqa: E402
from tests.scan.test_learned import CROSSED, REAL, sheet  # noqa: E402
from tests.scan.test_scan import LAYOUT, LEGEND_ROWS  # noqa: E402
from tests.viewer.test_symbol_learning import SHIFTS, review, scan  # noqa: E402

ADMIN = "boss@example.com"
REVIEWER = "assistant@example.com"


def main() -> None:
    work = Path(sys.argv[1])
    data = work / "data"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    origin = f"http://127.0.0.1:{port}"
    settings = Settings(env="production", data_dir=data, origin=origin, admin_emails=(ADMIN,), version="e2e")
    svc = ViewerService(data)
    site = Site(svc, settings)
    site.sitedb.put_member(REVIEWER, "reviewer", actor="setup")
    links = {e: site.sitedb.issue_setup_link(e, actor="setup", now=time.time())[0] for e in (ADMIN, REVIEWER)}

    leg = Canvas()
    draw_legend(leg, LEGEND_ROWS, LAYOUT)
    pdf = work / "set.pdf"
    build([(leg, 0, None)] + [(sheet(REAL, CROSSED, sh), 0, None) for sh in SHIFTS], str(pdf))
    v = svc.upload(pdf.read_bytes(), "set.pdf")["document_version"]
    svc.legend.read(v)
    svc.legend.confirm(v, {})
    run = scan(svc, v)
    review(svc, run, {1, 2})

    httpd = SiteServer(site, "127.0.0.1", port)
    print(json.dumps({"base": origin, "links": links, "document_version": v,
                      "real": len(REAL), "crossed": len(CROSSED)}), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
