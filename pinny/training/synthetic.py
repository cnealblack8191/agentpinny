"""Labelled synthetic pages and datasets (docs/phase2-contracts.md P4).

Each synthetic *document* is a small vector PDF written here (stdlib only)
and rendered through the real :class:`pinny.render.RenderService`, so the
pixels go through the same canonical 200 DPI pipeline as real drawings.

A page holds:

* receptacles: the glyph from ``tests/factory.py`` (``receptacle_ops``, a
  circle with two parallel bars), at one of the 4 quarter rotations and a
  0.9-1.1 scale jitter. Their centres are the ground-truth points;
* distractors: circles without bars, outlined and filled squares, text,
  and wall lines, kept clear of the receptacles so the labels stay exact;
* noise: gaussian noise and dark speckles added to the grayscale raster.

``synthesize_dataset`` writes a normal ``pinny.dataset`` v1 dataset with
``source.synthetic = true``. Its scores are never real-drawing accuracy.
Same arguments give the same ``dataset_id`` and byte-identical files.
"""

from __future__ import annotations

import math
import os
import random
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from pinny.render import CANONICAL_DPI, RenderService

from .dataset import (
    DEFAULT_FRACTIONS,
    SPLITS,
    DatasetResult,
    DatasetWriter,
    check_fractions,
    crop_centered,
    sha256_hex,
    split_for,
    to_gray,
)

GENERATOR = "pinny.training.synthetic"
GENERATOR_VERSION = 1
GLYPH_RADIUS_PT = 6.0  # tests/factory.py receptacle_ops default
PX_PER_PT = CANONICAL_DPI / 72
_NAMESPACE = uuid.UUID("5d0f3f36-7a55-4c55-9a53-1f0d2b1c9e11")
_WORDS = ("GFCI", "WP", "20A", "A-12", "B-3", "TYP", "EXIT", "RM 104", "CLG", "3'-6\"", "J-BOX", "NTS")


@dataclass(frozen=True)
class SynthParams:
    page_width_pt: float = 612.0
    page_height_pt: float = 792.0
    receptacles: tuple[int, int] = (4, 12)  # inclusive range per page
    circles: tuple[int, int] = (2, 8)
    squares: tuple[int, int] = (2, 8)
    texts: tuple[int, int] = (3, 10)
    walls: tuple[int, int] = (3, 8)
    scale: tuple[float, float] = (0.9, 1.1)
    noise_sigma: float = 6.0  # gray levels
    speckle_fraction: float = 0.0005
    background_negatives: int = 4  # random empty-area verifier negatives per page


@dataclass
class SyntheticPage:
    """Ground truth for one generated page, in canonical px (y down)."""

    content: str  # PDF content-stream operators
    points: list[dict[str, Any]] = field(default_factory=list)  # {"x", "y", "rotation", "scale"}
    distractors: list[dict[str, Any]] = field(default_factory=list)  # {"kind", "x", "y"}


# ------------------------------------------------------------------ glyphs


def receptacle_ops(cx: float, cy: float, r: float = GLYPH_RADIUS_PT) -> str:
    """Same operators as ``tests.factory.receptacle_ops`` (a test checks they match)."""
    k = 0.5523 * r
    circle = (
        f"{cx + r:.3f} {cy:.3f} m "
        f"{cx + r:.3f} {cy + k:.3f} {cx + k:.3f} {cy + r:.3f} {cx:.3f} {cy + r:.3f} c "
        f"{cx - k:.3f} {cy + r:.3f} {cx - r:.3f} {cy + k:.3f} {cx - r:.3f} {cy:.3f} c "
        f"{cx - r:.3f} {cy - k:.3f} {cx - k:.3f} {cy - r:.3f} {cx:.3f} {cy - r:.3f} c "
        f"{cx + k:.3f} {cy - r:.3f} {cx + r:.3f} {cy - k:.3f} {cx + r:.3f} {cy:.3f} c S "
    )
    bars = (
        f"{cx - r * 0.35:.3f} {cy - r * 0.6:.3f} m {cx - r * 0.35:.3f} {cy + r * 0.6:.3f} l S "
        f"{cx + r * 0.35:.3f} {cy - r * 0.6:.3f} m {cx + r * 0.35:.3f} {cy + r * 0.6:.3f} l S "
    )
    return circle + bars


def circle_ops(cx: float, cy: float, r: float) -> str:
    """The receptacle's circle with no bars (the hardest distractor)."""
    return receptacle_ops(cx, cy, r).split(" S ")[0] + " S "


def _placed(ops: str, cx: float, cy: float, rotation: int, scale: float) -> str:
    """``ops`` drawn around (0, 0), turned ``rotation`` degrees clockwise on the page, at (cx, cy)."""
    t = math.radians(rotation)
    c, s = round(math.cos(t)) * scale, round(math.sin(t)) * scale
    # PDF y is up, so a clockwise visual turn is x' = c*x + s*y, y' = -s*x + c*y.
    return f"q {c:.4f} {-s:.4f} {s:.4f} {c:.4f} {cx:.3f} {cy:.3f} cm {ops}Q "


def _text_ops(x: float, y: float, text: str, size: float, vertical: bool) -> str:
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    m = "0 1 -1 0" if vertical else "1 0 0 1"
    return f"BT /F1 {size:.1f} Tf {m} {x:.3f} {y:.3f} Tm ({safe}) Tj ET "


# --------------------------------------------------------------- page model


def generate_page(rng: random.Random, params: SynthParams = SynthParams()) -> SyntheticPage:
    """Lay out one page. Coordinates in the result are canonical px (y down)."""
    W, H = params.page_width_pt, params.page_height_pt
    margin = 3 * GLYPH_RADIUS_PT
    ops: list[str] = []
    page = SyntheticPage(content="")
    taken: list[tuple[float, float, float]] = []  # (x, y, radius) in pt, y up

    def free_spot(radius: float, tries: int = 200) -> tuple[float, float] | None:
        for _ in range(tries):
            x, y = rng.uniform(margin, W - margin), rng.uniform(margin, H - margin)
            if all(math.hypot(x - tx, y - ty) >= radius + tr for tx, ty, tr in taken):
                return x, y
        return None

    def to_px(x: float, y: float) -> tuple[float, float]:
        return x * PX_PER_PT, (H - y) * PX_PER_PT

    # Wall lines first: long thick strokes. Glyphs avoid them via a corridor check.
    walls: list[tuple[float, float, float, float, float]] = []
    for _ in range(rng.randint(*params.walls)):
        width = rng.uniform(2.0, 5.0)
        if rng.random() < 0.5:
            y = rng.uniform(margin, H - margin)
            x0, x1 = sorted((rng.uniform(0, W), rng.uniform(0, W)))
            walls.append((x0, y, x1, y, width))
        else:
            x = rng.uniform(margin, W - margin)
            y0, y1 = sorted((rng.uniform(0, H), rng.uniform(0, H)))
            walls.append((x, y0, x, y1, width))
    for x0, y0, x1, y1, width in walls:
        ops.append(f"{width:.2f} w {x0:.3f} {y0:.3f} m {x1:.3f} {y1:.3f} l S 1 w ")

    def clear_of_walls(x: float, y: float, radius: float) -> bool:
        for x0, y0, x1, y1, width in walls:
            dx = max(x0 - x, 0.0, x - x1)
            dy = max(y0 - y, 0.0, y - y1)
            if math.hypot(dx, dy) < radius + width:
                return False
        return True

    def place(radius: float) -> tuple[float, float] | None:
        for _ in range(50):
            spot = free_spot(radius)
            if spot is None:
                return None
            if clear_of_walls(*spot, radius):
                taken.append((*spot, radius))
                return spot
        return None

    keep_out = 2.5 * GLYPH_RADIUS_PT * params.scale[1]
    for _ in range(rng.randint(*params.receptacles)):
        spot = place(keep_out)
        if spot is None:
            continue
        rotation = rng.choice((0, 90, 180, 270))
        scale = rng.uniform(*params.scale)
        ops.append(_placed(receptacle_ops(0.0, 0.0), *spot, rotation, scale))
        px, py = to_px(*spot)
        page.points.append({"x": round(px, 3), "y": round(py, 3), "rotation": rotation, "scale": round(scale, 4)})

    for _ in range(rng.randint(*params.circles)):
        spot = place(keep_out)
        if spot:
            r = GLYPH_RADIUS_PT * rng.uniform(*params.scale)
            ops.append(circle_ops(*spot, r))
            page.distractors.append({"kind": "circle", **dict(zip("xy", map(lambda v: round(v, 3), to_px(*spot))))})
    for _ in range(rng.randint(*params.squares)):
        spot = place(keep_out)
        if spot:
            side = rng.uniform(6.0, 14.0)
            op = "f" if rng.random() < 0.5 else "S"
            ops.append(f"{spot[0] - side / 2:.3f} {spot[1] - side / 2:.3f} {side:.3f} {side:.3f} re {op} ")
            page.distractors.append({"kind": "square", **dict(zip("xy", map(lambda v: round(v, 3), to_px(*spot))))})
    for _ in range(rng.randint(*params.texts)):
        word = rng.choice(_WORDS)
        size = rng.uniform(6.0, 10.0)
        extent = 0.6 * size * len(word)
        spot = place(max(extent, size) / 2 + GLYPH_RADIUS_PT)
        if spot:
            vertical = rng.random() < 0.25
            # Start the baseline so the word is roughly centred on the spot.
            dx, dy = (size / 3, -extent / 2) if vertical else (-extent / 2, -size / 3)
            ops.append(_text_ops(spot[0] + dx, spot[1] + dy, word, size, vertical))
            page.distractors.append({"kind": "text", **dict(zip("xy", map(lambda v: round(v, 3), to_px(*spot))))})

    page.content = "0 G 0 g 1 w " + "".join(ops)
    return page


def build_pdf(pages: Sequence[str], width_pt: float, height_pt: float) -> bytes:
    """A minimal, uncompressed, deterministic PDF: one content stream per page, Helvetica as /F1."""
    n = len(pages)
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Count %d /Kids [%s] >>" % (n, " ".join(f"{4 + 2 * i} 0 R" for i in range(n)))).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, content in enumerate(pages):
        data = content.encode("ascii")
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:.3f} {height_pt:.3f}] "
                     f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>").encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def add_noise(gray: np.ndarray, rng: np.random.Generator, sigma: float, speckle_fraction: float) -> np.ndarray:
    out = gray.astype(np.float32)
    if sigma > 0:
        out += rng.normal(0.0, sigma, size=gray.shape).astype(np.float32)
    if speckle_fraction > 0:
        n = int(gray.size * speckle_fraction)
        ys, xs = rng.integers(0, gray.shape[0], n), rng.integers(0, gray.shape[1], n)
        out[ys, xs] = rng.uniform(0, 128, n).astype(np.float32)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


# ----------------------------------------------------------------- dataset


def _document_ids(n: int, seed: int, fractions: dict[str, float]) -> list[str]:
    """Deterministic uuid5 document ids whose hash splits match the fractions as closely as possible.

    The split is still exactly ``split_for(document_id)``; ids are just chosen
    so that a small synthetic set has documents in every non-empty split.
    """
    want = {s: 0 for s in SPLITS}
    for i in range(n):  # largest-remainder quotas, filled in order
        s = max(SPLITS, key=lambda k: fractions[k] * (i + 1) - want[k])
        want[s] += 1
    ids, j = [], 0
    targets = [s for s in SPLITS for _ in range(want[s])]
    for target in targets:
        while True:
            cand = str(uuid.uuid5(_NAMESPACE, f"{seed}:{j}"))
            j += 1
            if split_for(cand, fractions) == target:
                ids.append(cand)
                break
    return ids


def synthesize_dataset(datasets_root: os.PathLike | str, *, documents: int = 6, pages_per_document: int = 2,
                       seed: int = 0, params: SynthParams = SynthParams(),
                       fractions: dict[str, float] = DEFAULT_FRACTIONS,
                       render: RenderService | None = None, created_at: str | None = None) -> DatasetResult:
    """Generate, render and write a synthetic ``pinny.dataset`` v1 dataset.

    ``render`` defaults to a throwaway RenderService in a temporary
    directory, so synthetic PDFs never show up in the user's document list.
    ``created_at`` defaults to ``$SOURCE_DATE_EPOCH`` (or the epoch) so the
    build is reproducible.
    """
    if documents < 1 or pages_per_document < 1:
        raise ValueError("documents and pages_per_document must be >= 1")
    fractions = check_fractions(fractions)
    if created_at is None:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        from datetime import UTC, datetime

        created_at = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = {"synthetic": True, "export_sha256": None, "store_schema_version": None,
              "generator": {"name": GENERATOR, "version": GENERATOR_VERSION, "seed": seed,
                            "documents": documents, "pages_per_document": pages_per_document,
                            "params": asdict(params)}}
    tmp = None
    if render is None:
        tmp = tempfile.TemporaryDirectory(prefix="pinny-synth-")
        render = RenderService(Path(tmp.name))
    writer = DatasetWriter(datasets_root, source=source, fractions=fractions, seed=0, created_at=created_at)
    try:
        for d, document_id in enumerate(_document_ids(documents, seed, fractions)):
            rng = random.Random(f"{seed}:doc:{d}")
            pages = [generate_page(rng, params) for _ in range(pages_per_document)]
            pdf = build_pdf([p.content for p in pages], params.page_width_pt, params.page_height_pt)
            version = render.ingest_pdf(pdf, original_filename=f"synthetic-{seed}-{d}.pdf",
                                        document_id=document_id)
            for i, page in enumerate(pages):
                _write_page(writer, render, version, i, page, params,
                            np.random.default_rng([seed, d, i]), random.Random(f"{seed}:neg:{d}:{i}"))
        return writer.finish()
    except BaseException:
        writer.abort()
        raise
    finally:
        if tmp is not None:
            tmp.cleanup()


def _write_page(writer: DatasetWriter, render: RenderService, version, page_index: int, page: SyntheticPage,
                params: SynthParams, np_rng: np.random.Generator, rng: random.Random) -> None:
    info = version.pages[page_index]
    gray = to_gray(np.asarray(render.render_page(version.document_version, page_index)))
    gray = add_noise(gray, np_rng, params.noise_sigma, params.speckle_fraction)
    page_id = info.canonical_page_id
    document_id = version.document_id

    samples: list[tuple[str, int, float, float]] = [("pos", 1, p["x"], p["y"]) for p in page.points]
    samples += [(d["kind"], 0, d["x"], d["y"]) for d in page.distractors if d["kind"] in ("circle", "square", "text")]
    # Background negatives: random spots well away from every receptacle.
    h, w = gray.shape
    min_px = 3 * GLYPH_RADIUS_PT * PX_PER_PT
    for _ in range(params.background_negatives):
        for _ in range(100):
            x, y = rng.uniform(0, w), rng.uniform(0, h)
            if all(math.hypot(x - p["x"], y - p["y"]) >= min_px for p in page.points):
                samples.append(("background", 0, round(x, 3), round(y, 3)))
                break
    for n, (kind, label, x, y) in enumerate(samples):
        example_id = f"synthetic/{page_id}/{n}-{kind}"
        writer.add_verifier(sample_id=sha256_hex(example_id)[:24], label=label, crop=crop_centered(gray, x, y),
                            canonical_page_id=page_id, document_id=document_id, x=x, y=y,
                            source_example_id=example_id)
    writer.add_detector(page_key=sha256_hex(page_id)[:24], gray=gray, canonical_page_id=page_id,
                        document_id=document_id, points=[(p["x"], p["y"]) for p in page.points])
