"""Box mapping for rotated and upscaled OCR passes."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from pinny.ocr import OcrBox, OcrWord
from pinny.ocr.geometry import (
    merge_rotations, overlap_ratio, rotate_box, rotate_image, rotated_size, unrotate_box, unscale_box,
)

SIZE = (37, 23)  # (width, height), deliberately odd and non-square
BOXES = [OcrBox(0, 0, 1, 1), OcrBox(5, 3, 10, 4), OcrBox(30, 20, 7, 3), OcrBox(0, 0, 37, 23)]


@pytest.mark.parametrize("rotation,box", list(itertools.product((0, 90, 180, 270), BOXES)))
def test_rotate_box_matches_rotated_pixels(rotation, box):
    image = np.zeros((SIZE[1], SIZE[0]), dtype=np.uint8)
    image[box.y:box.y2, box.x:box.x2] = 255
    rotated = rotate_image(image, rotation)
    assert (rotated.shape[1], rotated.shape[0]) == rotated_size(SIZE, rotation)
    ys, xs = np.nonzero(rotated)
    expected = OcrBox(int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    assert rotate_box(box, SIZE, rotation) == expected


@pytest.mark.parametrize("rotation,box", list(itertools.product((0, 90, 180, 270), BOXES)))
def test_unrotate_inverts_rotate(rotation, box):
    assert unrotate_box(rotate_box(box, SIZE, rotation), SIZE, rotation) == box


def test_unscale_rounds_outwards_and_clamps():
    assert unscale_box(OcrBox(3, 3, 3, 3), 1.5, (100, 100)) == OcrBox(2, 2, 2, 2)
    assert unscale_box(OcrBox(0, 0, 150, 150), 1.5, (100, 100)) == OcrBox(0, 0, 100, 100)
    assert unscale_box(OcrBox(148, 148, 5, 5), 1.5, (100, 100)) == OcrBox(98, 98, 2, 2)
    assert unscale_box(OcrBox(4, 4, 2, 2), 1.0, (10, 10)) == OcrBox(4, 4, 2, 2)


def test_overlap_ratio():
    assert overlap_ratio(OcrBox(0, 0, 10, 10), OcrBox(10, 0, 5, 5)) == 0.0
    assert overlap_ratio(OcrBox(0, 0, 10, 10), OcrBox(2, 2, 3, 3)) == 1.0


def _w(text, box, conf, rotation):
    return OcrWord(text, box, conf, rotation=rotation)


def test_merge_keeps_the_stronger_reading_across_rotations():
    right = _w("FLOOR", OcrBox(100, 100, 20, 80), 95, 90)
    garbage = _w("I", OcrBox(102, 120, 10, 10), 96, 0)  # high confidence, but one char
    other = _w("PANEL", OcrBox(0, 0, 50, 20), 90, 0)
    merged = merge_rotations({0: [other, garbage], 90: [right]}, (0, 90, 180, 270), 0.5)
    assert merged == [other, right]


def test_merge_never_suppresses_within_a_rotation():
    a = _w("A1", OcrBox(0, 0, 20, 20), 90, 0)
    b = _w("B2", OcrBox(5, 5, 20, 20), 50, 0)
    assert merge_rotations({0: [a, b]}, (0,), 0.5) == [a, b]
