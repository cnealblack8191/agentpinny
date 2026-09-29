"""Legend jobs: read a drawing set's legend, and scan every sheet with it
(docs/legend-reader.md, docs/set-scanning.md). Run only inside the
sandboxed child (or in-process in development).

Payloads carry ids only. Every path is derived here from the data
directory, the document version and the set-scan run id::

    documents/<sha256>/source.pdf                         the drawing set
    documents/<sha256>/set_scans/<run_id>/legend.json     legend to scan with (written by the web tier)
    documents/<sha256>/set_scans/<run_id>/result.json     pinny.set_scan v1 (written here)
    documents/<sha256>/set_scans/<run_id>/packages.json   learned packages to use, by tag (web tier;
                                                          the files are copied into packages/)
    documents/<sha256>/set_scans/<run_id>/symbols.json    each legend symbol's raster template (written
                                                          here, as symbols/<entry_id>.png): what a
                                                          learned package of that symbol starts from

The child has no access to the learning store: the web process records
the result as one ordinary scan per sheet when the job has finished.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict

from .tasks import TaskError, _page, _version

_RUN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

TEMPLATE_HINT = ("Count with a template instead: open a sheet, choose Template box, draw a box around one "
                 "symbol and press Scan.")


def document_dir(data_dir: Path, version: str) -> Path:
    return Path(data_dir) / "documents" / version.split(":", 1)[1]


def run_dir(data_dir: Path, version: str, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_RE.match(run_id):
        raise TaskError("invalid_job", "Bad set-scan id.")
    return document_dir(data_dir, version) / "set_scans" / run_id


def _source(data_dir: Path, version: str) -> Path:
    d = document_dir(data_dir, version)
    if not (d / "version.json").is_file() or not (d / "source.pdf").is_file():
        raise TaskError("document_version_not_found", "That document does not exist.", 404)
    return d / "source.pdf"


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write next to ``path`` and rename. The directory must exist: it is
    never recreated, so a document deleted meanwhile stays deleted."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------ read_legend
def read_legend(p: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    """Find (or, with ``page_index``, read) the legend. Returns the
    ``pinny.legend`` v1 dict."""
    from pinny.legend import LegendError, LegendReader

    version = _version(p)
    page_index = None if p.get("page_index") is None else _page(p)
    path = _source(data_dir, version)
    reader = LegendReader()
    if page_index is None:
        found = reader.find(path)
        if not found:
            raise TaskError("legend_not_found",
                            "Pinny could not find a symbol legend in this drawing (a page with a heading such "
                            f"as LEGEND or SYMBOLS). {TEMPLATE_HINT}", 422)
        page_index = found[0].page_index
    try:
        legend = reader.read(path, page_index)
    except LegendError as exc:
        n = page_index + 1
        if exc.code == "raster_page":
            raise TaskError("legend_is_scanned",
                            f"The legend on page {n} is a scanned image, so Pinny can't read its text yet. "
                            f"{TEMPLATE_HINT}", 422) from None
        if exc.code == "page_index_out_of_range":
            raise TaskError("page_not_found", "That page is not in this drawing.", 404) from None
        if exc.code == "legend_not_found":
            raise TaskError("legend_not_found",
                            f"Pinny could not read a symbol legend on page {n}: it needs a heading such as "
                            "LEGEND or SYMBOLS with symbol rows under it (text drawn as lines can't be read). "
                            f"{TEMPLATE_HINT}", 422) from None
        raise TaskError("legend_unreadable", f"Pinny could not read the legend on page {n}. {TEMPLATE_HINT}",
                        422) from None
    return legend.to_dict()


# --------------------------------------------------------------- scan_set
_SCAN_MESSAGES = {
    "legend_mismatch": "This legend was read from a different drawing. Find the legend again.",
    "nothing_to_count": "The legend has no symbol set to be counted. Tick Count for at least one symbol.",
    "legend_needs_review": "Some legend symbols still need a look. Check them, then confirm the legend again.",
}


def _fill_signatures(path: Path, legend) -> None:
    """Symbols added by hand have no drawing signature yet; the look-alike
    check and the saved legend use it."""
    todo = [e for e in legend.entries if len(e.signatures) < len(e.symbol_boxes)]
    if not todo:
        return
    from pinny.legend.reader import _PageGeometry, symbol_signature
    from pinny.vector.content import open_pdf, read_page_content

    with open_pdf(path) as pdf:
        geom = _PageGeometry(read_page_content(pdf, legend.page_index))
    for e in todo:
        e.signatures = list(e.signatures) + [symbol_signature(geom, tuple(b))
                                             for b in e.symbol_boxes[len(e.signatures):]]


_ENTRY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PACKAGE_FILE_RE = re.compile(r"^[0-9a-f]{32}\.pinny$")


def _packages(rdir: Path) -> Dict[str, Any]:
    """The learned packages the web tier chose for this run, checked against
    the sha256 it recorded."""
    from pinny.model import ModelPackage, ModelPackageError

    try:
        listed = json.loads((rdir / "packages.json").read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise TaskError("packages_unreadable", "The learned examples for this scan could not be read. "
                        "Start the scan again.", 422) from None
    out: Dict[str, Any] = {}
    for tag, e in (listed.get("packages") or {}).items():
        f, sha = e.get("file"), e.get("sha256")
        if not isinstance(f, str) or not _PACKAGE_FILE_RE.match(f) or not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise TaskError("packages_unreadable", "The learned examples for this scan are malformed.", 422)
        fp = rdir / "packages" / f
        try:
            data = fp.read_bytes()
        except OSError:
            raise TaskError("packages_unreadable", "The learned examples for this scan are missing. "
                            "Start the scan again.", 409) from None
        if hashlib.sha256(data).hexdigest() != sha:
            raise TaskError("packages_changed", "The learned examples changed after the scan was started. "
                            "Start it again.", 409)
        try:
            out[str(tag)] = ModelPackage.load(str(fp))
        except ModelPackageError:
            raise TaskError("packages_unreadable", "The learned examples for this scan could not be read.", 422) from None
    return out


def _write_symbol_templates(rdir: Path, legend, templates: Dict[str, Any]) -> None:
    import cv2

    tags = {e.id: e.tag for e in legend.entries}
    sdir = rdir / "symbols"
    sdir.mkdir(exist_ok=True)
    listed = {}
    for entry_id, img in templates.items():
        if entry_id not in tags or not _ENTRY_RE.match(entry_id):
            continue
        ok, png = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if img.ndim == 3 else img)
        if ok:
            (sdir / f"{entry_id}.png").write_bytes(png.tobytes())
            listed[entry_id] = tags[entry_id]
    write_json_atomic(rdir / "symbols.json", {"symbols": listed})


def scan_set(p: Dict[str, Any], data_dir: Path, progress) -> Dict[str, Any]:
    from pinny.legend import Legend, LegendError
    from pinny.scan import ScanError, SetScanSettings
    from pinny.scan import scan_set as _scan_set

    version = _version(p)
    rdir = run_dir(data_dir, version, p.get("run_id"))
    want = p.get("legend_sha256")
    if not isinstance(want, str) or not _SHA_RE.match(want):
        raise TaskError("invalid_job", "Bad legend checksum.")
    path = _source(data_dir, version)
    try:
        raw = (rdir / "legend.json").read_bytes()
    except OSError:
        raise TaskError("legend_missing", "The legend for this scan is missing. Start the scan again.", 409) from None
    if hashlib.sha256(raw).hexdigest() != want:
        raise TaskError("legend_changed", "The legend changed after the scan was started. Start it again.", 409)
    try:
        legend = Legend.from_dict(json.loads(raw))
    except (LegendError, ValueError, KeyError, TypeError):
        raise TaskError("legend_unreadable", "The saved legend could not be read. Find the legend again.", 422) from None
    if legend.pdf_sha256 != version.split(":", 1)[1]:
        raise TaskError("legend_mismatch", _SCAN_MESSAGES["legend_mismatch"], 409)
    _fill_signatures(path, legend)

    def matched(page_index: int, done: int, total: int) -> None:
        progress.update(0.9 * done / max(total, 1), f"Searched sheet {page_index + 1} ({done} of {total}).")

    def finished(page_index: int, done: int, total: int) -> None:
        progress.update(0.9 + 0.1 * done / max(total, 1))

    packages = _packages(rdir)
    progress.update(0.0, "Scanning every sheet with the legend"
                    + (f" and the learned examples of {', '.join(sorted(packages))}." if packages else "."))
    try:
        res = _scan_set(path, legend, SetScanSettings(), progress=finished, matched=matched, packages=packages)
    except ScanError as exc:
        msg = _SCAN_MESSAGES.get(exc.code)
        if msg is None:
            raise TaskError("set_scan_failed", "The whole-set scan could not start.", 422) from None
        raise TaskError(exc.code, msg, 409) from None
    out = res.to_dict()
    # Signatures the scan filled in, so the web tier can keep them.
    out["legend_signatures"] = {e.id: list(e.signatures) for e in legend.entries}
    _write_symbol_templates(rdir, legend, res.symbol_templates)
    write_json_atomic(rdir / "result.json", out)
    progress.update(1.0, f"Done: {len(res.sheets)} sheet(s) in {res.elapsed_seconds:.1f} s.")
    return {"sheets": len(res.sheets), "counts": res.counts(), "elapsed_seconds": round(res.elapsed_seconds, 3),
            "warnings": len(res.warnings)}
