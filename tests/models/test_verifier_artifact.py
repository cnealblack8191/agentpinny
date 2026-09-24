"""pinny.model v1 save/load: sha check, tamper rejection, weights_only (P5)."""

from __future__ import annotations

import io
import json

import pytest

torch = pytest.importorskip("torch")

from pinny.models import artifact  # noqa: E402
from pinny.models.verifier import Verifier, build_network  # noqa: E402

META = {"input": {"channels": 1, "crop_px": 96, "dpi": 200}, "dataset_id": "d" * 64,
        "synthetic_only": True, "train_config": {"seed": 0},
        "operating_point": {"threshold": 0.5, "chosen_on": "val"}, "metrics": {"val": {}, "test": {}}}


def save(tmp_path, **kw):
    torch.manual_seed(0)
    return artifact.save_artifact(tmp_path / "models", kind="verifier", arch="verifier-cnn-v1",
                                  state_dict=build_network().state_dict(), metadata=META, **kw)


def test_round_trip(tmp_path):
    out = save(tmp_path)
    meta = artifact.read_metadata(out)
    assert out.name == meta["model_id"]
    assert meta["weights_sha256"] == artifact.sha256_hex((out / "weights.pt").read_bytes())
    assert meta["code_version"].startswith("git:") and meta["created_at"].endswith("Z")
    v = Verifier.load(out)
    assert v.model_id == meta["model_id"] and v.threshold == 0.5


def test_artifacts_are_immutable(tmp_path):
    import datetime as dt

    when = dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
    out = save(tmp_path, created_at=when)
    assert out.name.startswith("verifier-20260924T120000Z-")
    with pytest.raises(artifact.ModelArtifactError) as e:
        save(tmp_path, created_at=when)
    assert e.value.code == "model_exists"
    assert [p.name for p in (tmp_path / "models").iterdir()] == [out.name]  # no temp dirs left


def test_tampered_weights_are_rejected(tmp_path):
    out = save(tmp_path)
    data = bytearray((out / "weights.pt").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (out / "weights.pt").write_bytes(bytes(data))
    with pytest.raises(artifact.ModelArtifactError) as e:
        Verifier.load(out)
    assert e.value.code == "model_weights_mismatch"


def test_weights_only_refuses_arbitrary_pickles(tmp_path):
    out = save(tmp_path)

    class Evil:
        def __reduce__(self):
            return (print, ("unpickled!",))

    buf = io.BytesIO()
    torch.save({"x": Evil()}, buf)
    (out / "weights.pt").write_bytes(buf.getvalue())
    meta = json.loads((out / "model.json").read_text())
    digest = artifact.sha256_hex(buf.getvalue())
    meta["weights_sha256"] = digest
    meta["model_id"] = meta["model_id"][:-8] + digest[:8]
    (out / "model.json").write_text(json.dumps(meta))
    with pytest.raises(artifact.ModelArtifactError) as e:
        artifact.load_artifact(out)
    assert e.value.code == "invalid_model_weights"


def test_metadata_validation(tmp_path):
    out = save(tmp_path)
    with pytest.raises(artifact.ModelArtifactError) as e:
        artifact.load_artifact(out, kind="detector")
    assert e.value.code == "wrong_model_kind"
    meta = json.loads((out / "model.json").read_text())
    meta["format_version"] = 2
    (out / "model.json").write_text(json.dumps(meta))
    with pytest.raises(artifact.ModelArtifactError) as e:
        artifact.read_metadata(out)
    assert e.value.code == "unsupported_model_format"
    with pytest.raises(artifact.ModelArtifactError) as e:
        artifact.read_metadata(tmp_path / "missing")
    assert e.value.code == "model_not_found"
    with pytest.raises(artifact.ModelArtifactError) as e:
        artifact.save_artifact(tmp_path / "m", kind="verifier", arch="a", state_dict={},
                               metadata={**META, "model_id": "x"})
    assert e.value.code == "reserved_metadata_key"
