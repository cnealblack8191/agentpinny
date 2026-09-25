"""Second-stage verifiers: re-score detector candidates from reviewed crops.

The template matcher finds anything that correlates with the template; a
verifier learns from the reviewer's approved/added (positive) and rejected
(negative) crops which of those candidates are real receptacles. This lets
the matcher run at a lower, recall-oriented threshold while the verifier
restores precision.

* :class:`KnnVerifier` needs only numpy and OpenCV: HOG (numpy, in
  ``cv2.HOGDescriptor``'s layout, on a 64x64 resize) concatenated with a downsampled normalized-intensity
  vector, then a cosine k-nearest-neighbour vote.
* :class:`OnnxEmbeddingVerifier` is optional: the same vote over embeddings
  from a user-provided ONNX image model (e.g. an exported DINOv2 ViT-S/14).
  ``onnxruntime`` is imported lazily and no model is ever downloaded.

Crop geometry follows ``docs/contracts.md`` §6 for machine detections: the
detection box plus a 24 px margin on every side, clipped to the raster. Fit
on crops with that same geometry (the learning store's machine-detection
crops) so training and inference look alike.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

import cv2
import numpy as np

from .template_bank import rotate_quarter, to_gray
from .types import BoundingBox, DetectionError

__all__ = [
    "CROP_MARGIN_PX",
    "KnnVerifier",
    "OnnxEmbeddingVerifier",
    "Verifier",
    "crop_with_margin",
    "hog_descriptor",
    "hog_intensity_features",
    "margin_box",
]

#: Contracts §6: machine-detection training crops add this margin per side.
CROP_MARGIN_PX = 24


@runtime_checkable
class Verifier(Protocol):
    def score(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        """P(positive) in [0, 1] for each crop, as a float64 array."""
        ...


def margin_box(box: BoundingBox, page_shape: Tuple[int, ...], margin: int = CROP_MARGIN_PX) -> Tuple[int, int, int, int]:
    """``(x0, y0, x1, y1)`` (exclusive) of ``box`` grown by ``margin`` and
    clipped to a page of shape ``(H, W, ...)``."""
    h, w = int(page_shape[0]), int(page_shape[1])
    x0 = max(0, box.x - margin)
    y0 = max(0, box.y - margin)
    x1 = min(w, box.x2 + margin)
    y1 = min(h, box.y2 + margin)
    if x1 <= x0 or y1 <= y0:
        raise DetectionError("invalid_crop", f"Box {box.to_dict()} lies outside the {w}x{h} page.")
    return x0, y0, x1, y1


def crop_with_margin(page: np.ndarray, box: BoundingBox, margin: int = CROP_MARGIN_PX) -> np.ndarray:
    """Contracts §6 machine crop: ``box`` plus ``margin`` px, clipped."""
    x0, y0, x1, y1 = margin_box(box, page.shape, margin)
    return page[y0:y1, x0:x1]


_FEATURE_SIDE = 64
_INTENSITY_SIDE = 16
_CELL = 8
_BINS = 9


def hog_descriptor(gray64: np.ndarray) -> np.ndarray:
    """Dalal-Triggs HOG in numpy, same layout as ``cv2.HOGDescriptor((64, 64),
    (16, 16), (8, 8), (8, 8), 9)``: 8x8 px cells, 9 unsigned orientation bins
    with linear bin interpolation, 2x2-cell blocks at a one-cell stride,
    L2-Hys block normalization. 1764 values for a 64x64 input.

    Implemented here because some OpenCV 5 wheels no longer ship
    ``cv2.HOGDescriptor``; one implementation keeps features identical on
    every install.
    """
    img = gray64.astype(np.float64)
    gx = cv2.Sobel(img, cv2.CV_64F, 1, 0, ksize=1)
    gy = cv2.Sobel(img, cv2.CV_64F, 0, 1, ksize=1)
    mag = np.hypot(gx, gy)
    ang = np.rad2deg(np.arctan2(gy, gx)) % 180.0
    pos = ang / (180.0 / _BINS) - 0.5
    lo = np.floor(pos).astype(int)
    frac = pos - lo
    lo_bin, hi_bin = lo % _BINS, (lo + 1) % _BINS
    h, w = img.shape
    cy, cx = h // _CELL, w // _CELL
    cell_idx = (np.arange(h)[:, None] // _CELL) * cx + (np.arange(w)[None, :] // _CELL)
    hist = np.zeros((cy * cx, _BINS))
    np.add.at(hist, (cell_idx.ravel(), lo_bin.ravel()), (mag * (1 - frac)).ravel())
    np.add.at(hist, (cell_idx.ravel(), hi_bin.ravel()), (mag * frac).ravel())
    hist = hist.reshape(cy, cx, _BINS)
    blocks = []
    for by in range(cy - 1):
        for bx in range(cx - 1):
            v = hist[by : by + 2, bx : bx + 2].ravel()
            v = v / (np.linalg.norm(v) + 1e-6)
            v = np.minimum(v, 0.2)
            blocks.append(v / (np.linalg.norm(v) + 1e-6))
    return np.concatenate(blocks)


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def hog_intensity_features(crop: np.ndarray, intensity_weight: float = 0.5) -> np.ndarray:
    """Unit-length feature vector: L2(HOG) ++ w * L2(zero-mean 16x16 intensity)."""
    gray = to_gray(crop)
    resized = cv2.resize(gray, (_FEATURE_SIDE, _FEATURE_SIDE), interpolation=cv2.INTER_AREA)
    hog = hog_descriptor(resized)
    small = cv2.resize(gray, (_INTENSITY_SIDE, _INTENSITY_SIDE), interpolation=cv2.INTER_AREA)
    inten = small.astype(np.float64).ravel()
    inten = inten - inten.mean()
    return _l2(np.concatenate([_l2(hog), intensity_weight * _l2(inten)]))


class _KnnVote:
    """Cosine kNN, similarity-weighted vote with Laplace smoothing.

    ``p = (W_pos + a) / (W_pos + W_neg + 2a)`` over the ``k`` most similar
    labelled examples, where ``a`` is ``laplace`` and each neighbour's weight
    is ``exp((sim - sim_max) / temperature)`` (the nearest neighbour weighs
    1). Crops of one symbol family all have high cosine similarity (~0.95),
    so raw similarities would weigh neighbours almost equally; the
    temperature sharpens the vote towards the closest examples. With no
    evidence the estimate is 0.5.
    """

    def __init__(
        self,
        k: int = 7,
        laplace: float = 0.1,
        augment_rotations: bool = True,
        temperature: float = 0.01,
    ) -> None:
        if k < 1:
            raise DetectionError("invalid_settings", "k must be >= 1.")
        if laplace < 0:
            raise DetectionError("invalid_settings", "laplace must be >= 0.")
        self.k = int(k)
        if temperature <= 0:
            raise DetectionError("invalid_settings", "temperature must be > 0.")
        self.laplace = float(laplace)
        self.temperature = float(temperature)
        self.augment_rotations = augment_rotations
        self._feats: Optional[np.ndarray] = None
        self._labels: Optional[np.ndarray] = None

    # Subclasses implement.
    def _embed(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        raise NotImplementedError

    @property
    def fitted(self) -> bool:
        return self._feats is not None and len(self._feats) > 0

    def export_state(self) -> Tuple[np.ndarray, np.ndarray]:
        """The fitted examples as ``(features (N, D) float64, labels (N,)
        int8)``, for saving in a model package."""
        if not self.fitted:
            raise DetectionError("verifier_not_fitted", "Call fit() before export_state().")
        return self._feats.copy(), self._labels.copy()

    def load_state(self, features: np.ndarray, labels: np.ndarray) -> "_KnnVote":
        """Restore examples saved by :meth:`export_state` without refitting.
        Features must come from the same embedding (see ``FEATURE_VERSION``)."""
        feats = np.asarray(features, dtype=np.float64)
        labs = np.asarray(labels, dtype=np.int8)
        if feats.ndim != 2 or labs.shape != (feats.shape[0],) or feats.shape[0] == 0:
            raise DetectionError(
                "invalid_verifier_state",
                f"Verifier state needs features (N, D) and labels (N,), got {feats.shape} and {labs.shape}.",
            )
        if not np.isin(labs, (0, 1)).all():
            raise DetectionError("invalid_verifier_state", "Verifier labels must be 0 or 1.")
        if not np.isfinite(feats).all():
            raise DetectionError("invalid_verifier_state", "Verifier features must be finite.")
        self._feats, self._labels = feats, labs
        return self

    def fit(self, pos_crops: Sequence[np.ndarray], neg_crops: Sequence[np.ndarray] = ()) -> "_KnnVote":
        """Replace the labelled set. With ``augment_rotations`` each crop is
        also stored at 90/180/270 so candidates at any rotation match."""
        crops: List[np.ndarray] = []
        labels: List[int] = []
        turns = (0, 90, 180, 270) if self.augment_rotations else (0,)
        for label, group in ((1, pos_crops), (0, neg_crops)):
            for c in group:
                for r in turns:
                    crops.append(rotate_quarter(to_gray(c), r))
                    labels.append(label)
        if not crops:
            raise DetectionError("verifier_no_examples", "fit() needs at least one labelled crop.")
        self._feats = self._embed(crops)
        self._labels = np.asarray(labels, dtype=np.int8)
        return self

    def score(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        if not self.fitted:
            raise DetectionError(
                "verifier_not_fitted", "Call fit(pos_crops, neg_crops) before score()."
            )
        if len(crops) == 0:
            return np.zeros(0, dtype=np.float64)
        q = self._embed(list(crops))
        sims = q @ self._feats.T  # cosine: features are unit length
        k = min(self.k, sims.shape[1])
        # Deterministic top-k: sort by -similarity, ties by training index.
        idx = np.argsort(-sims, axis=1, kind="stable")[:, :k]
        top = np.take_along_axis(sims, idx, axis=1)
        w = np.exp((top - top[:, :1]) / self.temperature)
        lab = self._labels[idx]
        w_pos = (w * (lab == 1)).sum(axis=1)
        w_neg = (w * (lab == 0)).sum(axis=1)
        a = self.laplace
        denom = w_pos + w_neg + 2 * a
        p = np.where(denom > 0, (w_pos + a) / np.where(denom > 0, denom, 1.0), 0.5)
        return np.clip(p, 0.0, 1.0).astype(np.float64)

    def rerank(
        self, page: np.ndarray, candidates: Sequence[Any], crop_margin: int = CROP_MARGIN_PX
    ) -> List[Tuple[Any, float]]:
        """Score each candidate's §6 crop; return ``(candidate, p)`` sorted by
        descending p, then descending raw score, then top-to-bottom,
        left-to-right. Candidates need ``.box`` (and ideally ``.score``)."""
        cands = list(candidates)
        if not cands:
            return []
        crops = [crop_with_margin(page, c.box, crop_margin) for c in cands]
        ps = self.score(crops)
        order = sorted(
            range(len(cands)),
            key=lambda i: (-ps[i], -float(getattr(cands[i], "score", 0.0)), cands[i].box.y, cands[i].box.x, i),
        )
        return [(cands[i], float(ps[i])) for i in order]


class KnnVerifier(_KnnVote):
    """numpy/OpenCV-only kNN verifier over HOG + intensity features."""

    #: Identifies the feature extraction. Change it whenever
    #: :func:`hog_intensity_features` changes, so saved states are refused
    #: instead of being compared against incompatible features.
    FEATURE_VERSION = "hog64-intensity-v1"

    def __init__(
        self,
        k: int = 7,
        laplace: float = 0.1,
        augment_rotations: bool = True,
        intensity_weight: float = 0.5,
        temperature: float = 0.01,
    ) -> None:
        super().__init__(k=k, laplace=laplace, augment_rotations=augment_rotations, temperature=temperature)
        self.intensity_weight = float(intensity_weight)

    def _embed(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        return np.stack([hog_intensity_features(c, self.intensity_weight) for c in crops])


_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)


class OnnxEmbeddingVerifier(_KnnVote):
    """kNN verifier over embeddings from a user-provided ONNX image model.

    The model must take one float32 NCHW RGB input of ``input_size`` (224 for
    DINOv2 ViT-S/14) normalized with ``mean``/``std``. Its first output is
    used: ``(N, D)`` as is, ``(N, T, D)`` takes the CLS token ``[:, 0]``,
    ``(N, C, H, W)`` is mean-pooled. Embeddings are L2-normalized.

    Requires ``onnxruntime`` (imported lazily). Nothing is downloaded.
    """

    def __init__(
        self,
        model_path: "os.PathLike[str] | str | bytes",
        input_size: int = 224,
        mean: Sequence[float] = _IMAGENET_MEAN,
        std: Sequence[float] = _IMAGENET_STD,
        k: int = 7,
        laplace: float = 0.1,
        augment_rotations: bool = True,
        providers: Optional[Sequence[str]] = None,
        batch_size: int = 32,
        temperature: float = 0.05,
    ) -> None:
        super().__init__(k=k, laplace=laplace, augment_rotations=augment_rotations, temperature=temperature)
        try:
            import onnxruntime as ort  # noqa: WPS433 (lazy optional dependency)
        except ImportError as exc:
            raise DetectionError(
                "onnxruntime_missing",
                "OnnxEmbeddingVerifier needs the optional 'onnxruntime' package. Install it "
                "with 'pip install onnxruntime', or use KnnVerifier (numpy/OpenCV only).",
            ) from exc
        if isinstance(model_path, (bytes, bytearray)):
            # Serialized model (e.g. from a model package): no file needed.
            path = bytes(model_path)
        else:
            path = os.fspath(model_path)
        if isinstance(path, str) and not os.path.isfile(path):
            raise DetectionError(
                "model_not_found",
                f"ONNX model file {path!r} does not exist. Export an image-embedding model "
                "(e.g. DINOv2 ViT-S/14) to ONNX and pass its path; Pinny never downloads models.",
            )
        self.input_size = int(input_size)
        self.mean = np.asarray(mean, dtype=np.float32).reshape(1, 3, 1, 1)
        self.std = np.asarray(std, dtype=np.float32).reshape(1, 3, 1, 1)
        self.batch_size = max(1, int(batch_size))
        self._session = ort.InferenceSession(
            path, providers=list(providers) if providers else ["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

    def _preprocess(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        s = self.input_size
        batch = []
        for c in crops:
            rgb = cv2.cvtColor(to_gray(c), cv2.COLOR_GRAY2RGB)
            rgb = cv2.resize(rgb, (s, s), interpolation=cv2.INTER_AREA)
            batch.append(rgb.astype(np.float32).transpose(2, 0, 1) / 255.0)
        x = np.stack(batch)
        return ((x - self.mean) / self.std).astype(np.float32)

    def _embed(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        out = []
        for i in range(0, len(crops), self.batch_size):
            x = self._preprocess(crops[i : i + self.batch_size])
            y = np.asarray(self._session.run(None, {self._input_name: x})[0], dtype=np.float64)
            if y.ndim == 3:
                y = y[:, 0, :]
            elif y.ndim == 4:
                y = y.mean(axis=(2, 3))
            elif y.ndim != 2:
                y = y.reshape(len(x), -1)
            out.append(y)
        e = np.concatenate(out)
        n = np.linalg.norm(e, axis=1, keepdims=True)
        return e / np.where(n > 1e-12, n, 1.0)
