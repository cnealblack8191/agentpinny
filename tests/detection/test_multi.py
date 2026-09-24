"""detect_multi: pooling, cross-template suppression, provenance and veto."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pytest

from pinny.detection import (
    BoundingBox,
    Candidate,
    DetectionError,
    DetectionResult,
    DetectionTimeout,
    OpenCVTemplateDetector,
    ScanSettings,
    Template,
)
from pinny.detection.multi import MultiDetectionResult, detect_multi
from pinny.detection.template_bank import NegativeBank


def receptacle() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (8, 16), (8, 24), 0, 2)
    cv2.line(g, (16, 16), (16, 24), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def lookalike() -> np.ndarray:
    g = np.full((36, 24), 255, dtype=np.uint8)
    cv2.circle(g, (12, 20), 9, 0, 2)
    cv2.line(g, (6, 20), (18, 20), 0, 2)
    cv2.line(g, (12, 0), (12, 10), 0, 2)
    return g


def place(page, img, x, y) -> BoundingBox:
    h, w = img.shape[:2]
    page[y : y + h, x : x + w] = np.minimum(page[y : y + h, x : x + w], img)
    return BoundingBox(x, y, w, h)


@dataclass(frozen=True)
class MirroredCandidate(Candidate):
    mirrored: bool = False


class FakeDetector:
    """Returns canned candidates per template, keyed by the template's first pixel."""

    name = "fake"

    def __init__(self, table):
        self.table = table
        self.calls = []

    def detect(self, page, template, settings=ScanSettings()):
        img = template.image if isinstance(template, Template) else template
        key = int(np.asarray(img).reshape(-1)[0])
        self.calls.append((key, img.shape, settings.max_runtime_seconds))
        return DetectionResult(
            candidates=tuple(self.table.get(key, ())),
            detector=self.name,
            threshold=settings.threshold,
            rotations_searched=tuple(settings.rotations),
            warnings=(f"w{key}",),
        )


def tmpl(value: int) -> np.ndarray:
    t = np.full((10, 10), 255, np.uint8)
    t[0, 0] = value
    return t


def cand(score, x, y, w=10, h=10, rotation=0, cls=Candidate, **kw):
    return cls(score=score, box=BoundingBox(x, y, w, h), rotation=rotation, **kw)


def test_pools_suppresses_across_templates_and_records_index():
    det = FakeDetector(
        {
            1: [cand(0.90, 0, 0), cand(0.85, 100, 0)],
            2: [cand(0.95, 1, 1), cand(0.88, 200, 0)],  # (1,1) duplicates (0,0)
        }
    )
    page = np.full((50, 300), 255, np.uint8)
    res = detect_multi(det, page, [tmpl(1), tmpl(2)], ScanSettings())
    assert isinstance(res, MultiDetectionResult)
    assert [(c.box.x, c.score, ti) for c, ti in res.pairs()] == [
        (1, 0.95, 1),
        (200, 0.88, 1),
        (100, 0.85, 0),
    ]
    assert len(res.per_template) == 2
    assert res.result.warnings == ("template 0: w1", "template 1: w2")
    d = res.to_dict()
    assert [c["template_index"] for c in d["candidates"]] == [1, 1, 0]
    assert d["templates_searched"] == 2


def test_exact_tie_goes_to_lower_template_index():
    det = FakeDetector({1: [cand(0.9, 5, 5)], 2: [cand(0.9, 5, 5)]})
    page = np.full((50, 50), 255, np.uint8)
    for _ in range(3):
        res = detect_multi(det, page, [tmpl(1), tmpl(2)])
        assert res.template_indices == (0,)


def test_max_candidates_truncates():
    det = FakeDetector({1: [cand(0.9 - i * 0.01, i * 20, 0) for i in range(5)]})
    page = np.full((50, 200), 255, np.uint8)
    res = detect_multi(det, page, [tmpl(1)], ScanSettings(max_candidates=3))
    assert len(res.candidates) == 3 and res.result.truncated


def test_converts_template_channels_to_page():
    det = FakeDetector({})
    rgb_page = np.full((50, 50, 3), 255, np.uint8)
    detect_multi(det, rgb_page, [tmpl(1)])
    assert det.calls[0][1] == (10, 10, 3)


def test_shared_runtime_budget():
    ticks = iter([0.0, 0.0, 5.0, 70.0])
    det = FakeDetector({})
    page = np.full((50, 50), 255, np.uint8)
    with pytest.raises(DetectionTimeout):
        detect_multi(
            det, page, [tmpl(1), tmpl(2), tmpl(3)], ScanSettings(max_runtime_seconds=60), clock=lambda: next(ticks)
        )
    assert [c[2] for c in det.calls] == [60.0, 55.0]


def test_rejects_empty_templates():
    with pytest.raises(DetectionError):
        detect_multi(FakeDetector({}), np.zeros((5, 5), np.uint8), [])


def test_negative_veto_drops_lookalike_keeps_receptacles():
    page = np.full((200, 300), 255, np.uint8)
    real = [place(page, receptacle(), x, 20) for x in (20, 120)]
    fake = place(page, lookalike(), 220, 20)
    rgb = cv2.cvtColor(page, cv2.COLOR_GRAY2RGB)
    det = OpenCVTemplateDetector()
    settings = ScanSettings(rotations=(0, 90), threshold=0.8)
    templates = [Template(cv2.cvtColor(receptacle(), cv2.COLOR_GRAY2RGB))]

    plain = detect_multi(det, rgb, templates, settings)
    assert (fake.x, fake.y) in {(c.box.x, c.box.y) for c in plain.candidates}

    # The reviewer rejected a look-alike elsewhere, drawn rotated.
    negatives = NegativeBank.from_crops([np.rot90(lookalike(), -1).copy()])
    vetoed = detect_multi(det, rgb, templates, settings, negatives=negatives)
    assert {(c.box.x, c.box.y) for c in vetoed.candidates} == {(b.x, b.y) for b in real}
    assert len(vetoed.vetoed) == 1
    v = vetoed.vetoed[0]
    assert (v.candidate.box.x, v.candidate.box.y) == (fake.x, fake.y)
    assert v.negative_similarity > v.positive_similarity
    assert vetoed.to_dict()["vetoed"][0]["template_index"] == 0

    # A huge margin disables the veto.
    lenient = detect_multi(det, rgb, templates, settings, negatives=negatives, veto_margin=1.0)
    assert len(lenient.candidates) == 3 and not lenient.vetoed


def test_tolerates_mirrored_candidates():
    page = np.full((80, 80), 255, np.uint8)
    glyph = receptacle()
    place(page, glyph[:, ::-1], 10, 10)
    c = cand(0.9, 10, 10, 24, 36, cls=MirroredCandidate, mirrored=True)
    det = FakeDetector({255: [c]})
    negatives = NegativeBank.from_crops([lookalike()])
    res = detect_multi(det, page, [glyph], negatives=negatives)
    assert res.candidates == (c,)
