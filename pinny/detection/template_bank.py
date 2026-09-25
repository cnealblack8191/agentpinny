"""Template bank: a compact set of templates learned from reviewed crops.

A single user-drawn template misses variants of the same symbol (slightly
different scale, heavier line weight, a different CAD block). The bank
clusters the *tight* symbol crops of approved and added pins, plus the
user's original template, into at most ``max_templates`` medoids. Each
medoid is a real example image, so it can be passed straight to a
:class:`~pinny.detection.Detector` as a :class:`~pinny.detection.Template`.

Crops are compared at a common size (the original template's size when
given, otherwise the most common crop size) by centre-cropping or padding
with the crop's own background level, then by zero-mean normalized
cross-correlation (NCC). Distance is ``1 - NCC``. Clustering is a small,
deterministic k-medoids (PAM-style alternation) implemented in numpy; the
smallest ``k`` whose every member correlates with its medoid at
``min_similarity`` or better is chosen.

:class:`NegativeBank` holds rejected crops and answers "how much does this
crop look like something the reviewer rejected?".

Inputs are tight crops of the symbol (the detection box, not the contracts
§6 training crop with its 24 px margin). Use :func:`trim_margin` to cut the
margin back off an unclipped §6 machine crop.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .types import DetectionError, Template

__all__ = [
    "BankTemplate",
    "NegativeBank",
    "build_template_bank",
    "convert_channels",
    "fit_to_size",
    "k_medoids",
    "ncc",
    "pairwise_ncc",
    "rotate_quarter",
    "to_gray",
    "trim_margin",
]


def to_gray(image: np.ndarray) -> np.ndarray:
    """Grayscale uint8 copy using the detector's RGB(A) convention."""
    if not isinstance(image, np.ndarray) or image.ndim not in (2, 3) or image.size == 0:
        raise DetectionError("invalid_crop", "A crop must be a non-empty 2-D or 3-D numpy array.")
    img = image
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    if img.ndim == 2:
        gray = img
    elif img.shape[2] == 1:
        gray = img[:, :, 0]
    elif img.shape[2] == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    elif img.shape[2] == 4:
        gray = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
    else:
        raise DetectionError("invalid_crop", f"Unsupported crop shape {image.shape}.")
    return np.ascontiguousarray(gray)


def trim_margin(crop: np.ndarray, margin: int = 24) -> np.ndarray:
    """Remove a uniform ``margin`` from every side of an *unclipped* §6 crop."""
    h, w = crop.shape[:2]
    if margin < 0 or 2 * margin >= min(h, w):
        raise DetectionError(
            "invalid_crop", f"Cannot trim a {margin}px margin from a {w}x{h} crop."
        )
    return crop[margin : h - margin, margin : w - margin].copy() if margin else crop.copy()


def _background_level(gray: np.ndarray) -> int:
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    return int(np.median(border))


def fit_to_size(image: np.ndarray, size: Tuple[int, int], fill: Optional[int] = None) -> np.ndarray:
    """Centre-crop and/or pad a grayscale image to ``size = (height, width)``.

    Padding uses ``fill`` or, by default, the median of the image border
    (the local paper colour), so padding never adds line work.
    """
    gray = to_gray(image)
    th, tw = int(size[0]), int(size[1])
    if th <= 0 or tw <= 0:
        raise DetectionError("invalid_size", f"Target size must be positive, got {size}.")
    value = _background_level(gray) if fill is None else int(fill)
    h, w = gray.shape
    # Crop (centre) where too large.
    if h > th:
        y0 = (h - th) // 2
        gray = gray[y0 : y0 + th, :]
    if w > tw:
        x0 = (w - tw) // 2
        gray = gray[:, x0 : x0 + tw]
    h, w = gray.shape
    if (h, w) == (th, tw):
        return np.ascontiguousarray(gray)
    out = np.full((th, tw), value, dtype=np.uint8)
    y0, x0 = (th - h) // 2, (tw - w) // 2
    out[y0 : y0 + h, x0 : x0 + w] = gray
    return out


def _unit(gray: np.ndarray) -> Optional[np.ndarray]:
    v = gray.astype(np.float64).ravel()
    v = v - v.mean()
    n = np.linalg.norm(v)
    return None if n < 1e-9 else v / n


def ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean normalized cross-correlation of two images.

    Different sizes are reconciled by fitting ``b`` to ``a``'s size. Returns
    0.0 when either image is flat (correlation undefined).
    """
    ga = to_gray(a)
    gb = fit_to_size(b, ga.shape)
    ua, ub = _unit(ga), _unit(gb)
    if ua is None or ub is None:
        return 0.0
    return float(np.clip(ua @ ub, -1.0, 1.0))


def pairwise_ncc(images: Sequence[np.ndarray]) -> np.ndarray:
    """(N, N) NCC matrix of same-size grayscale images (flat images score 0)."""
    n = len(images)
    if n == 0:
        return np.zeros((0, 0))
    vecs = []
    for img in images:
        u = _unit(to_gray(img))
        vecs.append(np.zeros(to_gray(img).size) if u is None else u)
    m = np.stack(vecs)
    sim = np.clip(m @ m.T, -1.0, 1.0)
    return sim


def _assign(dist: np.ndarray, medoids: List[int]) -> np.ndarray:
    # Ties go to the earliest medoid in the list: deterministic.
    return np.asarray(medoids)[np.argmin(dist[:, medoids], axis=1)]


def k_medoids(
    dist: np.ndarray,
    k: int,
    fixed: Sequence[int] = (),
    max_iter: int = 100,
) -> Tuple[List[int], np.ndarray]:
    """Deterministic k-medoids on a precomputed distance matrix.

    Initialisation: ``fixed`` medoids first (they never move), then the most
    central point if none is fixed, then repeatedly the point farthest from
    its nearest medoid (ties -> lowest index). Then alternate assignment and
    per-cluster medoid update until stable. Returns ``(medoids, labels)``
    where ``labels[i]`` is the medoid index point ``i`` belongs to.
    """
    n = dist.shape[0]
    if n == 0:
        return [], np.empty(0, dtype=np.intp)
    k = max(1, min(int(k), n))
    fixed = [int(f) for f in fixed]
    medoids: List[int] = list(dict.fromkeys(fixed))[:k]
    if not medoids:
        medoids.append(int(np.argmin(dist.sum(axis=1))))
    while len(medoids) < k:
        nearest = dist[:, medoids].min(axis=1)
        nearest[medoids] = -1.0
        medoids.append(int(np.argmax(nearest)))
    fixed_set = set(fixed)
    for _ in range(max_iter):
        labels = _assign(dist, medoids)
        new = []
        for m in medoids:
            if m in fixed_set:
                new.append(m)
                continue
            members = np.flatnonzero(labels == m)
            cost = dist[np.ix_(members, members)].sum(axis=1)
            new.append(int(members[np.argmin(cost)]))
        if new == medoids:
            break
        medoids = new
    return medoids, _assign(dist, medoids)


@dataclass(frozen=True)
class BankTemplate:
    """One medoid of the template bank."""

    #: The medoid example exactly as supplied (native size and channels).
    image: np.ndarray
    #: Number of input crops (including the original) in this cluster.
    support: int
    #: Indices into the combined input list (original first, if given, then
    #: ``positives`` in order) of this cluster's members.
    member_indices: Tuple[int, ...]
    #: Index of the medoid in that combined list.
    source_index: int
    #: Lowest NCC of any member to this medoid (at the common size).
    min_member_similarity: float
    #: True if this medoid is the user's original template.
    is_original: bool = False

    def as_template(self, channels: Optional[int] = None) -> Template:
        """A :class:`Template`; ``channels`` (1, 3 or 4) converts grayscale to
        match the page raster's layout, which the detector requires."""
        img = self.image
        if channels is not None:
            img = convert_channels(img, channels)
        return Template(image=np.ascontiguousarray(img))


def convert_channels(image: np.ndarray, channels: int) -> np.ndarray:
    """Convert a uint8 image to 1, 3 (RGB) or 4 (RGBA) channels."""
    have = 1 if image.ndim == 2 else image.shape[2]
    if have == channels:
        return image
    gray = to_gray(image)
    if channels == 1:
        return gray
    if channels == 3:
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    if channels == 4:
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGBA)
    raise DetectionError("invalid_channels", f"channels must be 1, 3 or 4, got {channels}.")


def _common_size(images: Sequence[np.ndarray]) -> Tuple[int, int]:
    counts = Counter(img.shape[:2] for img in images)
    # Most common; ties -> larger area, then lexicographic, for determinism.
    return max(counts.items(), key=lambda kv: (kv[1], kv[0][0] * kv[0][1], kv[0]))[0]


def build_template_bank(
    positives: Sequence[np.ndarray],
    max_templates: int = 8,
    min_similarity: float = 0.9,
    *,
    original: Optional[np.ndarray] = None,
    size: Optional[Tuple[int, int]] = None,
) -> List[BankTemplate]:
    """Cluster positive symbol crops into at most ``max_templates`` medoids.

    ``original`` (the user's drawn template) is always kept as a medoid and
    listed first. Its size is the common comparison size unless ``size`` is
    given; otherwise the most common crop size is used. The smallest ``k``
    for which every crop has NCC >= ``min_similarity`` with its medoid is
    chosen (capped at ``max_templates``). Output order: original first, then
    by descending support, then by input index.
    """
    if max_templates < 1:
        raise DetectionError("invalid_settings", "max_templates must be >= 1.")
    if not (-1.0 <= min_similarity <= 1.0):
        raise DetectionError("invalid_settings", "min_similarity must be in [-1, 1].")
    images: List[np.ndarray] = ([original] if original is not None else []) + list(positives)
    for img in images:
        to_gray(img)  # validates
    if not images:
        return []
    if size is None:
        size = original.shape[:2] if original is not None else _common_size(images)
    norm = [fit_to_size(img, size) for img in images]
    sim = pairwise_ncc(norm)
    dist = 1.0 - sim
    fixed = [0] if original is not None else []

    medoids, labels = [], np.empty(0)
    k_lo = max(1, len(fixed))
    for k in range(k_lo, min(max_templates, len(images)) + 1):
        medoids, labels = k_medoids(dist, k, fixed=fixed)
        worst = min(sim[i, labels[i]] for i in range(len(images)))
        if worst >= min_similarity - 1e-12:
            break

    bank = []
    for m in medoids:
        members = tuple(int(i) for i in np.flatnonzero(labels == m))
        bank.append(
            BankTemplate(
                image=images[m],
                support=len(members),
                member_indices=members,
                source_index=int(m),
                min_member_similarity=float(min(sim[i, m] for i in members)),
                is_original=original is not None and m == 0,
            )
        )
    bank.sort(key=lambda b: (not b.is_original, -b.support, b.source_index))
    return bank


@dataclass
class NegativeBank:
    """Rejected crops, compared by NCC at every quarter turn.

    Construct with :meth:`from_crops`. ``max_items`` keeps the bank small by
    replacing it with k-medoid representatives of the rejected crops.
    """

    crops: List[np.ndarray] = field(default_factory=list)

    @classmethod
    def from_crops(
        cls, crops: Sequence[np.ndarray], max_items: int = 64, size: Optional[Tuple[int, int]] = None
    ) -> "NegativeBank":
        grays = [to_gray(c) for c in crops]
        if len(grays) <= max_items:
            return cls(crops=grays)
        size = size or _common_size(grays)
        norm = [fit_to_size(g, size) for g in grays]
        medoids, _ = k_medoids(1.0 - pairwise_ncc(norm), max_items)
        return cls(crops=[grays[m] for m in sorted(medoids)])

    def __len__(self) -> int:
        return len(self.crops)

    def max_similarity(self, crop: np.ndarray, rotations: Sequence[int] = (0, 90, 180, 270)) -> float:
        """Highest NCC between ``crop`` and any negative at any of
        ``rotations``; -1.0 for an empty bank."""
        if not self.crops:
            return -1.0
        gray = to_gray(crop)
        best = -1.0
        for neg in self.crops:
            for r in rotations:
                best = max(best, ncc(gray, rotate_quarter(neg, r)))
        return best


_CV_ROTATE = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def rotate_quarter(image: np.ndarray, rotation: int) -> np.ndarray:
    """Rotate clockwise by a quarter-turn multiple (y-down raster)."""
    rotation = int(rotation) % 360
    if rotation == 0:
        return image
    if rotation not in _CV_ROTATE:
        raise DetectionError("invalid_rotation", f"Unsupported rotation {rotation}.")
    return cv2.rotate(image, _CV_ROTATE[rotation])
