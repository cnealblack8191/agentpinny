"""What each job kind does. Runs only inside the sandboxed child.

Payloads carry ids and small settings; every path is derived here from
``data_dir`` and a validated id, never taken from the payload.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_VERSION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STAGE_RE = re.compile(r"^[0-9a-f]{32}$")


class TaskError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = status


def staging_path(data_dir: Path, staging_id: str) -> Path:
    """Where the web tier stages an upload for the ingest job."""
    if not isinstance(staging_id, str) or not _STAGE_RE.match(staging_id):
        raise TaskError("invalid_job", "Bad staging id.")
    return Path(data_dir) / "tmp" / f"stage_{staging_id}.pdf"


def _version(p: Dict[str, Any]) -> str:
    v = p.get("document_version")
    if not isinstance(v, str) or not _VERSION_RE.match(v):
        raise TaskError("invalid_job", "Bad document version.")
    return v


def _page(p: Dict[str, Any], key: str = "page_index") -> int:
    i = p.get(key)
    if isinstance(i, bool) or not isinstance(i, int) or i < 0:
        raise TaskError("invalid_job", "Bad page index.")
    return i


def _box(b: Any):
    from pinny.detection import BoundingBox
    if not isinstance(b, dict) or any(isinstance(b.get(k), bool) or not isinstance(b.get(k), int)
                                      for k in ("x", "y", "width", "height")):
        raise TaskError("invalid_template_box", "template box needs whole-pixel x, y, width and height.")
    return BoundingBox(b["x"], b["y"], b["width"], b["height"])


def _render(data_dir: Path):
    from pinny.render import RenderService
    return RenderService(data_dir, cache_size=2)


def ingest(p: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    path = staging_path(data_dir, p.get("staging_id"))
    filename = p.get("filename")
    document_id = p.get("document_id")
    if filename is not None and not isinstance(filename, str):
        raise TaskError("invalid_job", "Bad filename.")
    if document_id is not None and not isinstance(document_id, str):
        raise TaskError("invalid_job", "Bad document id.")
    try:
        meta = _render(data_dir).ingest_pdf(path, original_filename=filename, document_id=document_id)
    finally:
        path.unlink(missing_ok=True)
    return meta.to_dict()


def render_page(p: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    rgb = _render(data_dir).render_page(_version(p), _page(p))  # writes the PNG cache
    return {"width": int(rgb.shape[1]), "height": int(rgb.shape[0])}


def _template(render, version: str, page_index: int, box_d: Any):
    from pinny.detection import Template
    box = _box(box_d)  # validate before touching the document
    tpl = Template.from_page_crop(render.render_page(version, page_index), box)
    return tpl, hashlib.sha256(tpl.image.tobytes()).hexdigest()


def template(p: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    _, sha = _template(_render(data_dir), _version(p), _page(p), p.get("box"))
    return {"box": p["box"], "sha256": sha}


def scan(p: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    from pinny.detection import OpenCVTemplateDetector, ScanSettings
    version, page_index = _version(p), _page(p)
    t = p.get("template")
    if not isinstance(t, dict):
        raise TaskError("invalid_job", "Bad template.")
    render = _render(data_dir)
    tpl, sha = _template(render, version, _page(t), t.get("box"))
    if t.get("sha256") and sha != t["sha256"]:
        raise TaskError("template_changed", "The template page no longer renders the same pixels; "
                        "draw the template again.", 409)
    s = dict(p.get("settings") or {})
    if "rotations" in s:
        s["rotations"] = tuple(s["rotations"])
    if "scales" in s:
        s["scales"] = tuple(s["scales"])
    if "search_region" in s and s["search_region"] is not None:
        s["search_region"] = _box(s["search_region"])
    settings = ScanSettings(**s)
    page = render.render_page(version, page_index)
    result = OpenCVTemplateDetector().detect(page, tpl, settings)
    candidates = []
    for c in result.candidates:
        cx, cy = c.center
        candidates.append({"box": c.box.to_dict(), "x": cx, "y": cy, "score": float(c.score),
                           "rotation": c.rotation, "source": "detector"})
    return {"detector": result.detector, "candidates": candidates, "truncated": bool(result.truncated),
            "warnings": list(result.warnings), "template_sha256": sha}


def selftest(p: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    """Probes for the sandbox tests. The web tier never submits these."""
    import socket
    action = p.get("action")
    if action == "ok":
        return {"ok": True}
    if action == "sleep":
        time.sleep(float(p.get("seconds", 60)))
        return {"slept": True}
    if action == "spin":  # lowers its own CPU limit so the test is quick
        import resource
        cpu = int(p.get("cpu_s", 2))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        while True:
            pass
    if action == "allocate":
        blob = bytearray(int(p.get("mb", 4096)) * 1024 * 1024)
        return {"allocated": len(blob)}
    if action == "network":
        try:
            socket.create_connection((p.get("host", "1.1.1.1"), 53), timeout=3).close()
            return {"network": True}
        except OSError as exc:
            return {"network": False, "error": type(exc).__name__}
    if action == "write":
        path = Path(data_dir) / "tmp" / "selftest.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        chunk = b"x" * 1024 * 1024
        with path.open("wb") as f:
            for _ in range(int(p.get("mb", 1))):
                f.write(chunk)
        return {"written": path.stat().st_size}
    if action == "env":
        import os
        return {"env": sorted(os.environ)}
    if action == "sleep_first":  # hangs on the first attempt only (worker-crash tests)
        marker = Path(data_dir) / "tmp" / "selftest.marker"
        if marker.exists():
            return {"attempt": "second"}
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
        time.sleep(float(p.get("seconds", 60)))
        return {"attempt": "first"}
    if action == "fail":
        raise TaskError("selftest_failed", "Expected failure.", 422)
    if action == "crash":
        raise RuntimeError("secret internal detail /srv/pinny")
    raise TaskError("invalid_job", "Unknown selftest action.")


_TASKS: Dict[str, Callable[[Dict[str, Any], Path], Any]] = {
    "ingest": ingest, "render_page": render_page, "template": template, "scan": scan,
    "selftest": selftest,
}


def run(kind: str, payload: Dict[str, Any], data_dir: str, *, job_id: Optional[str] = None,
        progress=None) -> Any:
    """Run one job. Training kinds (``pinny.jobs.train_tasks``) also need the
    job id, which names their output and progress directories, and report
    through ``progress`` (by default a ``Progress`` in the job's directory)."""
    from . import legend_tasks
    from .train_tasks import TRAIN_TASKS
    if kind in TRAIN_TASKS or kind == "scan_set":
        from .progress import Progress, job_dir
        try:
            directory = job_dir(data_dir, job_id)
        except ValueError:
            raise TaskError("invalid_job", "Bad job id.") from None
        if progress is None:
            progress = Progress(directory, data_dir=data_dir)
        if kind == "scan_set":
            return legend_tasks.scan_set(payload, Path(data_dir), progress)
        return TRAIN_TASKS[kind](payload, Path(data_dir), progress, job_id)
    if kind == "read_legend":
        return legend_tasks.read_legend(payload, Path(data_dir))
    return _TASKS[kind](payload, Path(data_dir))
