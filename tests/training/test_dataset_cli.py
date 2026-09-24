"""python -m pinny.training: the command table and the dataset commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pinny.training import __main__ as cli
from tests.training.conftest import pt_to_px

REPO = Path(__file__).resolve().parents[2]


def test_command_table_lists_dataset_commands(capsys):
    assert {"build-dataset", "synthesize", "dataset-info"} <= set(cli.COMMANDS)
    for target, help_ in cli.COMMANDS.values():
        assert target.count(":") == 1 and help_
    assert cli.main(["--help"]) == 0
    assert "build-dataset" in capsys.readouterr().out
    assert cli.main(["no-such-command"]) == 2


def test_build_dataset_then_dataset_info(world, capsys):
    v = world.add_document([(50.0, 60.0)])
    scan = world.add_scan(v, [pt_to_px(50.0, 60.0)])
    world.approve(scan, "det-0")

    assert cli.main(["build-dataset", "--data-dir", str(world.data_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["path"]).parent == world.data_dir / "datasets"
    assert out["synthetic"] is False
    total_pos = sum(out["counts"]["verifier"][s]["pos"] for s in ("train", "val", "test"))
    assert total_pos == 1

    assert cli.main(["dataset-info", out["dataset_id"], "--data-dir", str(world.data_dir)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["dataset_id"] == out["dataset_id"] and info["problems"] == []

    (Path(out["path"]) / "manifest.json").write_text("{}")
    assert cli.main(["dataset-info", out["path"]]) == 1  # PinnyError -> exit 1, message on stderr
    assert "unsupported_dataset" in capsys.readouterr().err


def test_synthesize_via_subprocess_without_torch(tmp_path):
    code = (
        "import sys; from pinny.training.__main__ import main; "
        f"rc = main(['synthesize', '--out-dir', {str(tmp_path / 'ds')!r}, '--documents', '2', "
        "'--pages-per-document', '1', '--page-size', '200x200']); "
        "assert 'torch' not in sys.modules, 'dataset commands must not import torch'; sys.exit(rc)"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["synthetic"] is True and not out["reused"]
    proc = subprocess.run([sys.executable, "-m", "pinny.training", "dataset-info", out["path"]], cwd=REPO,
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["problems"] == []
