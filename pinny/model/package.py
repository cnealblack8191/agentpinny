"""In-memory model package: build it, save it, load it, run it.

A :class:`ModelPackage` is everything the QC app needs to find one symbol
type on canonical page rasters without any training code or review data:

1. a template bank, matched with :func:`pinny.detection.multi.detect_multi`;
2. an optional negative bank that vetoes look-alikes;
3. an optional kNN verifier (HOG features, or embeddings from a bundled
   ONNX model) that scores each candidate's crop;
4. optional score calibration (isotonic or Platt) that turns the raw
   matching score into a probability;
5. a decision rule saying which of those numbers accepts a candidate.

It never changes a candidate's raw ``score`` (contracts §4). The extra
numbers are reported next to it.
"""

from __future__ import annotations

import dataclasses
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from pinny.detection import BoundingBox, Candidate, OpenCVTemplateDetector, ScanSettings
from pinny.detection.calibration import IsotonicCalibrator, PlattCalibrator
from pinny.detection.interface import Detector
from pinny.detection.multi import detect_multi
from pinny.detection.template_bank import NegativeBank, to_gray
from pinny.detection.types import DetectionError
from pinny.detection.verifier import CROP_MARGIN_PX, KnnVerifier, OnnxEmbeddingVerifier, crop_with_margin

from . import format as fmt
from .format import ModelPackageError

DECISION_METHODS = ("score", "verifier", "calibrated")
Calibrator = Union[IsotonicCalibrator, PlattCalibrator]
Verifier = Union[KnnVerifier, OnnxEmbeddingVerifier]

#: Scan settings that describe the machine, not the model. They are not
#: saved, so the QC app's own choices apply.
_MACHINE_SETTINGS = ("num_threads", "search_region")


@dataclass
class PackageTemplate:
    """One template of the bank: the image (uint8, grayscale or RGB, native
    size), how many reviewed examples it stands for, and whether it is the
    user's original exemplar."""

    image: np.ndarray
    support: int = 1
    is_original: bool = False


@dataclass(frozen=True)
class Decision:
    """How a candidate is accepted.

    * ``score``: raw matching score >= ``threshold`` (e.g. a suggested
      threshold from review history).
    * ``verifier``: verifier probability >= ``threshold``.
    * ``calibrated``: calibrated probability >= ``threshold``.
    """

    method: str = "score"
    threshold: float = 0.8

    def to_dict(self) -> dict:
        return {"method": self.method, "threshold": float(self.threshold)}


@dataclass(frozen=True)
class ModelDetection:
    candidate: Candidate
    template_index: int
    #: Verifier P(real symbol), if the package has a verifier.
    verifier_p: Optional[float]
    #: Calibrated P(real symbol) from the raw score, if calibrated.
    probability: Optional[float]
    accepted: bool


@dataclass(frozen=True)
class ModelResult:
    """Output of :meth:`ModelPackage.detect`. ``detections`` holds every
    candidate that survived matching and the veto, accepted ones first."""

    detections: Tuple[ModelDetection, ...]
    model: Dict[str, str]
    decision: Decision
    vetoed: int
    truncated: bool
    elapsed_seconds: float
    warnings: Tuple[str, ...] = ()

    @property
    def accepted(self) -> Tuple[ModelDetection, ...]:
        return tuple(d for d in self.detections if d.accepted)

    def to_dict(self, accepted_only: bool = False) -> dict:
        """JSON-serialisable result. Each detection follows the contracts §4
        record (``id``, ``box``, ``x``, ``y``, ``score``, ``rotation``,
        ``mirrored``, ``source``) plus ``template_index``, ``verifier_p``,
        ``probability`` and ``accepted``."""
        rows = []
        for i, d in enumerate(self.accepted if accepted_only else self.detections):
            c = d.candidate
            cx, cy = c.center
            rows.append(
                {
                    "id": f"det-{i}",
                    "box": c.box.to_dict(),
                    "x": float(cx),
                    "y": float(cy),
                    "score": float(c.score),
                    "rotation": int(c.rotation),
                    "mirrored": bool(getattr(c, "mirrored", False)),
                    "source": "model",
                    "template_index": d.template_index,
                    "verifier_p": d.verifier_p,
                    "probability": d.probability,
                    "accepted": d.accepted,
                }
            )
        return {
            "model": dict(self.model),
            "decision": self.decision.to_dict(),
            "detections": rows,
            "vetoed": self.vetoed,
            "truncated": self.truncated,
            "elapsed_seconds": self.elapsed_seconds,
            "warnings": list(self.warnings),
        }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _producer() -> dict:
    try:
        from importlib.metadata import version

        pinny_version = version("pinny")
    except Exception:  # not installed as a distribution yet
        pinny_version = "unknown"
    return {"pinny_version": pinny_version}


@dataclass
class ModelPackage:
    name: str
    version: str
    symbol_class: str
    templates: List[PackageTemplate]
    scan_settings: ScanSettings = field(default_factory=ScanSettings)
    negatives: List[np.ndarray] = field(default_factory=list)
    #: A candidate is vetoed when it resembles a negative more than its best
    #: template by more than this margin.
    veto_margin: float = 0.0
    verifier: Optional[Verifier] = None
    #: Required with an :class:`OnnxEmbeddingVerifier`: the ONNX model bytes
    #: to bundle (the verifier itself doesn't keep them).
    verifier_model_bytes: Optional[bytes] = None
    calibration: Optional[Calibrator] = None
    decision: Decision = field(default_factory=Decision)
    description: str = ""
    #: The page renderer the model was trained on (contracts §6). Pages from a
    #: different renderer may score differently.
    renderer_version: str = "unknown"
    #: Where the model came from: dataset manifest digest, store sequence,
    #: label counts, training documents. Free-form JSON.
    provenance: Dict[str, Any] = field(default_factory=dict)
    #: Held-out evaluation results (e.g. from ``pinny_eval score-corpus``):
    #: ``{"corpus_sha256": ..., "ap": ..., "f1": ..., "recall": ...}``.
    evaluation: Dict[str, Any] = field(default_factory=dict)
    model_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=_now)
    # Set by load(): the package file's sha256 and whether it was signed.
    package_sha256: Optional[str] = None
    signed: bool = False

    # ------------------------------------------------------------------
    # Validation and manifest

    def validate(self) -> None:
        def fail(message: str) -> None:
            raise ModelPackageError("invalid_model", message)

        for label, value in (("name", self.name), ("version", self.version), ("symbol_class", self.symbol_class)):
            if not isinstance(value, str) or not value.strip():
                fail(f"{label} must be a non-empty string.")
        if not self.templates:
            fail("A model needs at least one template.")
        for i, t in enumerate(self.templates):
            img = t.image
            if not isinstance(img, np.ndarray) or img.dtype != np.uint8 or img.ndim not in (2, 3):
                fail(f"Template {i} must be a uint8 (H, W) or (H, W, C) array.")
            if img.ndim == 3 and img.shape[2] not in (1, 3, 4):
                fail(f"Template {i} must have 1, 3 or 4 channels.")
            if int(t.support) < 1:
                fail(f"Template {i} support must be >= 1.")
        for i, n in enumerate(self.negatives):
            if not isinstance(n, np.ndarray) or n.dtype != np.uint8 or n.ndim not in (2, 3):
                fail(f"Negative {i} must be a uint8 image array.")
        if not math.isfinite(self.veto_margin):
            fail("veto_margin must be finite.")
        self.scan_settings.validate()
        if self.decision.method not in DECISION_METHODS:
            fail(f"decision.method must be one of {DECISION_METHODS}, got {self.decision.method!r}.")
        if not math.isfinite(self.decision.threshold):
            fail("decision.threshold must be finite.")
        if self.decision.method == "verifier":
            if self.verifier is None or not self.verifier.fitted:
                fail("decision.method 'verifier' needs a fitted verifier.")
            if not 0.0 <= self.decision.threshold <= 1.0:
                fail("A verifier decision threshold must be in [0, 1].")
        if self.decision.method == "calibrated":
            if self.calibration is None:
                fail("decision.method 'calibrated' needs a fitted calibration.")
            if not 0.0 <= self.decision.threshold <= 1.0:
                fail("A calibrated decision threshold must be in [0, 1].")
        if self.decision.method == "score" and self.decision.threshold < self.scan_settings.threshold:
            fail(
                f"decision.threshold {self.decision.threshold} is below the scan threshold "
                f"{self.scan_settings.threshold}; candidates below the scan threshold are never found."
            )
        if self.verifier is not None:
            if not self.verifier.fitted:
                fail("The verifier must be fitted before packaging.")
            if isinstance(self.verifier, OnnxEmbeddingVerifier) and not self.verifier_model_bytes:
                fail("An ONNX verifier needs verifier_model_bytes (the .onnx file contents).")
        if self.calibration is not None:
            if isinstance(self.calibration, IsotonicCalibrator) and self.calibration.x_ is None:
                fail("The isotonic calibration is not fitted.")
            if isinstance(self.calibration, PlattCalibrator) and self.calibration.a is None:
                fail("The Platt calibration is not fitted.")

    def identity(self) -> Dict[str, str]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "version": self.version,
            "symbol_class": self.symbol_class,
        }

    def _settings_dict(self) -> dict:
        d = self.scan_settings.to_dict()
        for key in _MACHINE_SETTINGS:
            d.pop(key, None)
        return d

    def _build(self) -> Tuple[dict, Dict[str, bytes]]:
        self.validate()
        files: Dict[str, bytes] = {}
        templates = []
        for i, t in enumerate(self.templates):
            name = f"templates/{i:03d}.png"
            files[name] = fmt.encode_png(t.image)
            templates.append(
                {
                    "file": name,
                    "width": int(t.image.shape[1]),
                    "height": int(t.image.shape[0]),
                    "channels": 1 if t.image.ndim == 2 else int(t.image.shape[2]),
                    "support": int(t.support),
                    "is_original": bool(t.is_original),
                }
            )
        negatives = None
        if self.negatives:
            names = []
            for i, n in enumerate(self.negatives):
                name = f"negatives/{i:03d}.png"
                files[name] = fmt.encode_png(n)
                names.append(name)
            negatives = {"files": names, "veto_margin": float(self.veto_margin)}

        verifier = None
        if self.verifier is not None:
            feats, labels = self.verifier.export_state()
            files["verifier/features.npy"] = fmt.encode_npy(feats.astype(np.float32))
            files["verifier/labels.npy"] = fmt.encode_npy(labels.astype(np.int8))
            v = self.verifier
            params = {
                "k": v.k,
                "laplace": v.laplace,
                "temperature": v.temperature,
                "augment_rotations": bool(v.augment_rotations),
            }
            if isinstance(v, OnnxEmbeddingVerifier):
                files["verifier/embedder.onnx"] = bytes(self.verifier_model_bytes)
                params.update(
                    input_size=v.input_size,
                    mean=[float(x) for x in v.mean.ravel()],
                    std=[float(x) for x in v.std.ravel()],
                    batch_size=v.batch_size,
                )
                verifier = {"type": "onnx-embedding", "model": "verifier/embedder.onnx"}
            else:
                params["intensity_weight"] = v.intensity_weight
                verifier = {"type": "knn-hog", "feature_version": KnnVerifier.FEATURE_VERSION}
            verifier.update(
                params=params,
                features="verifier/features.npy",
                labels="verifier/labels.npy",
                crop_margin_px=CROP_MARGIN_PX,
                n_positive=int((labels == 1).sum()),
                n_negative=int((labels == 0).sum()),
            )

        calibration = None
        if isinstance(self.calibration, IsotonicCalibrator):
            calibration = {
                "type": "isotonic",
                "x": [float(x) for x in self.calibration.x_],
                "y": [float(y) for y in self.calibration.y_],
            }
        elif isinstance(self.calibration, PlattCalibrator):
            calibration = {"type": "platt", "a": float(self.calibration.a), "b": float(self.calibration.b)}

        manifest = {
            "format": fmt.FORMAT,
            "format_version": fmt.FORMAT_VERSION,
            **self.identity(),
            "description": self.description,
            "created_at": self.created_at,
            "contracts_version": fmt.CONTRACTS_VERSION,
            "input": {
                "space": "canonical_raster_px",
                "dpi": 200,
                "pixel_format": "RGB uint8",
                "renderer_version": self.renderer_version,
            },
            "scan_settings": self._settings_dict(),
            "templates": templates,
            "negatives": negatives,
            "verifier": verifier,
            "calibration": calibration,
            "decision": self.decision.to_dict(),
            "provenance": self.provenance,
            "evaluation": self.evaluation,
            "requires": {
                "python": ["numpy", "opencv-python-headless"],
                "optional": ["onnxruntime"] if isinstance(self.verifier, OnnxEmbeddingVerifier) else [],
            },
            "producer": _producer(),
        }
        return manifest, files

    def manifest(self) -> dict:
        """The manifest that :meth:`save` would write (without the file
        table)."""
        return self._build()[0]

    # ------------------------------------------------------------------
    # Save / load

    def save(self, path: str, signing_key: Optional[bytes] = None) -> str:
        """Write the package; return its sha256 digest. With ``signing_key``,
        add an HMAC-SHA256 signature the QC app can check with the same key."""
        manifest, files = self._build()
        digest = fmt.write_archive(str(path), manifest, files, signing_key)
        self.package_sha256 = digest
        self.signed = signing_key is not None
        return digest

    @classmethod
    def load(
        cls,
        path: str,
        verify_key: Optional[bytes] = None,
        onnx_providers: Optional[Sequence[str]] = None,
    ) -> "ModelPackage":
        """Read, integrity-check and reconstruct a package.

        With ``verify_key`` the package must be signed with that key; an
        unsigned or differently signed package is refused. Without it the
        signature is not checked (file integrity always is).
        """
        manifest, manifest_bytes, signature, files, digest = fmt.read_archive(str(path))
        if verify_key is not None:
            fmt.verify_signature(manifest_bytes, signature, verify_key)
        try:
            pkg = cls._from_manifest(manifest, files, onnx_providers)
        except (KeyError, TypeError, IndexError) as exc:
            raise ModelPackageError("corrupt_package", f"manifest.json is missing or has a bad field: {exc!r}") from exc
        except DetectionError as exc:
            raise ModelPackageError(exc.code, str(exc)) from exc
        pkg.package_sha256 = digest
        pkg.signed = signature is not None
        pkg.validate()
        return pkg

    @classmethod
    def _from_manifest(
        cls, m: dict, files: Dict[str, bytes], onnx_providers: Optional[Sequence[str]]
    ) -> "ModelPackage":
        def file(name: str) -> bytes:
            if name not in files:
                raise ModelPackageError("corrupt_package", f"manifest.json refers to missing file {name!r}.")
            return files[name]

        templates = [
            PackageTemplate(
                image=fmt.decode_png(file(t["file"]), t["file"]),
                support=int(t.get("support", 1)),
                is_original=bool(t.get("is_original", False)),
            )
            for t in m["templates"]
        ]
        negatives: List[np.ndarray] = []
        veto_margin = 0.0
        if m.get("negatives"):
            negatives = [fmt.decode_png(file(n), n) for n in m["negatives"]["files"]]
            veto_margin = float(m["negatives"].get("veto_margin", 0.0))

        verifier: Optional[Verifier] = None
        model_bytes: Optional[bytes] = None
        vm = m.get("verifier")
        if vm:
            p = vm["params"]
            if vm["type"] == "knn-hog":
                if vm.get("feature_version") != KnnVerifier.FEATURE_VERSION:
                    raise ModelPackageError(
                        "incompatible_model",
                        f"The verifier was built with features {vm.get('feature_version')!r}; this Pinny "
                        f"computes {KnnVerifier.FEATURE_VERSION!r}. Rebuild the model with this version.",
                    )
                verifier = KnnVerifier(
                    k=p["k"],
                    laplace=p["laplace"],
                    augment_rotations=p["augment_rotations"],
                    intensity_weight=p["intensity_weight"],
                    temperature=p["temperature"],
                )
            elif vm["type"] == "onnx-embedding":
                model_bytes = file(vm["model"])
                verifier = OnnxEmbeddingVerifier(
                    model_bytes,
                    input_size=p["input_size"],
                    mean=p["mean"],
                    std=p["std"],
                    k=p["k"],
                    laplace=p["laplace"],
                    augment_rotations=p["augment_rotations"],
                    providers=onnx_providers,
                    batch_size=p.get("batch_size", 32),
                    temperature=p["temperature"],
                )
            else:
                raise ModelPackageError(
                    "unsupported_component", f"Unknown verifier type {vm['type']!r}; upgrade Pinny."
                )
            feats = fmt.decode_npy(file(vm["features"]), vm["features"])
            labels = fmt.decode_npy(file(vm["labels"]), vm["labels"])
            verifier.load_state(feats, labels)

        calibration: Optional[Calibrator] = None
        cm = m.get("calibration")
        if cm:
            if cm["type"] == "isotonic":
                calibration = IsotonicCalibrator()
                calibration.x_ = np.asarray(cm["x"], dtype=np.float64)
                calibration.y_ = np.asarray(cm["y"], dtype=np.float64)
                if calibration.x_.shape != calibration.y_.shape or calibration.x_.size == 0:
                    raise ModelPackageError("corrupt_package", "Isotonic calibration x and y don't match.")
            elif cm["type"] == "platt":
                calibration = PlattCalibrator()
                calibration.a, calibration.b = float(cm["a"]), float(cm["b"])
            else:
                raise ModelPackageError(
                    "unsupported_component", f"Unknown calibration type {cm['type']!r}; upgrade Pinny."
                )

        d = m["decision"]
        return cls(
            name=m["name"],
            version=m["version"],
            symbol_class=m["symbol_class"],
            templates=templates,
            scan_settings=ScanSettings.from_dict(m["scan_settings"]),
            negatives=negatives,
            veto_margin=veto_margin,
            verifier=verifier,
            verifier_model_bytes=model_bytes,
            calibration=calibration,
            decision=Decision(method=d["method"], threshold=float(d["threshold"])),
            description=m.get("description", ""),
            renderer_version=m.get("input", {}).get("renderer_version", "unknown"),
            provenance=m.get("provenance") or {},
            evaluation=m.get("evaluation") or {},
            model_id=m["model_id"],
            created_at=m["created_at"],
        )

    # ------------------------------------------------------------------
    # Inference

    def detect(
        self,
        page: np.ndarray,
        *,
        search_region: Optional[BoundingBox] = None,
        max_runtime_seconds: Optional[float] = None,
        num_threads: Optional[int] = None,
        detector: Optional[Detector] = None,
    ) -> ModelResult:
        """Find this model's symbol on one canonical page raster (RGB uint8,
        contracts §2). Coordinates are canonical page pixels."""
        start = time.monotonic()
        if not isinstance(page, np.ndarray) or page.dtype != np.uint8 or page.ndim not in (2, 3):
            raise ModelPackageError("invalid_page", "The page must be a uint8 (H, W) or (H, W, C) raster.")
        overrides: Dict[str, Any] = {}
        if search_region is not None:
            overrides["search_region"] = search_region
        if max_runtime_seconds is not None:
            overrides["max_runtime_seconds"] = max_runtime_seconds
        if num_threads is not None:
            overrides["num_threads"] = num_threads
        settings = dataclasses.replace(self.scan_settings, **overrides)
        negatives = NegativeBank(crops=[to_gray(n) for n in self.negatives]) if self.negatives else None
        multi = detect_multi(
            detector or OpenCVTemplateDetector(),
            page,
            [t.image for t in self.templates],
            settings,
            negatives,
            veto_margin=self.veto_margin,
        )
        pairs = multi.pairs()
        cands = [c for c, _ in pairs]
        vps: List[Optional[float]] = [None] * len(cands)
        if self.verifier is not None and cands:
            scores = self.verifier.score([crop_with_margin(page, c.box, CROP_MARGIN_PX) for c in cands])
            vps = [float(s) for s in scores]
        probs: List[Optional[float]] = [None] * len(cands)
        if self.calibration is not None and cands:
            probs = [float(p) for p in self.calibration.predict([c.score for c in cands])]

        method, thr = self.decision.method, self.decision.threshold
        rows = []
        for (c, ti), vp, pr in zip(pairs, vps, probs):
            value = {"score": c.score, "verifier": vp, "calibrated": pr}[method]
            rows.append(ModelDetection(c, ti, vp, pr, bool(value is not None and value >= thr)))
        key = {"score": lambda d: d.candidate.score, "verifier": lambda d: d.verifier_p,
               "calibrated": lambda d: d.probability}[method]
        rows.sort(key=lambda d: (not d.accepted, -key(d), -d.candidate.score, d.candidate.box.y, d.candidate.box.x))
        return ModelResult(
            detections=tuple(rows),
            model=self.identity(),
            decision=self.decision,
            vetoed=len(multi.vetoed),
            truncated=multi.result.truncated,
            elapsed_seconds=time.monotonic() - start,
            warnings=multi.result.warnings,
        )
