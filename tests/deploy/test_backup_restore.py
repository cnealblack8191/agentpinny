"""deploy/bin/pinny-backup and pinny-restore against a data directory with
live WAL-mode databases (docs/deployment.md, "Backups" and "Restore")."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest
from tests.deploy.scripts import load

backup = load("pinny-backup")
restore = load("pinny-restore")


def wal_db(path: Path, rows: int) -> sqlite3.Connection:
    """A WAL database with uncheckpointed writes, left open like a running app."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")  # keep everything in the -wal file
    db.execute("CREATE TABLE IF NOT EXISTS t (i INTEGER PRIMARY KEY, v TEXT)")
    db.executemany("INSERT INTO t (v) VALUES (?)", [(f"row {i}",) for i in range(rows)])
    return db


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "srv" / "production"
    (d / "documents" / ("a" * 64) / "pages").mkdir(parents=True)
    (d / "documents" / ("a" * 64) / "source.pdf").write_bytes(b"%PDF-1.7\n" + os.urandom(5000))
    (d / "documents" / ("a" * 64) / "pages" / "p0.png").write_bytes(os.urandom(20000))
    (d / "models").mkdir()
    (d / "models" / "verifier.pt").write_bytes(os.urandom(3000))
    (d / "crops").mkdir()
    (d / "exports").mkdir()
    (d / "tmp").mkdir()
    (d / "tmp" / "upload_x.part").write_bytes(b"partial upload")
    (d / "jobs").mkdir()
    (d / "jobs.sqlite3").symlink_to("jobs/jobs.sqlite3")
    conns = [wal_db(d / "site.sqlite3", 50), wal_db(d / "pinny.sqlite3", 500), wal_db(d / "jobs.sqlite3", 20)]
    assert (d / "pinny.sqlite3-wal").stat().st_size > 0  # the data really is in the WAL
    assert (d / "jobs" / "jobs.sqlite3-wal").exists()  # SQLite followed the link
    yield d
    for c in conns:
        c.close()


def run_backup(data_dir: Path, tmp_path: Path, *extra: str) -> Path:
    dest = tmp_path / "bucket"
    rc = backup.main(["--profile", "production", "--data-dir", str(data_dir), "--dest", dest.as_uri(),
                      "--state-dir", str(tmp_path / "state"), *extra])
    assert rc == 0
    return dest


def test_backup_uses_online_copies_and_a_manifest(data_dir, tmp_path):
    dest = run_backup(data_dir, tmp_path)
    archives = sorted(dest.glob("*.tar.gz"))
    assert len(archives) == 1
    name = archives[0].name[:-len(".tar.gz")]
    assert (dest / f"{name}.manifest.json").is_file()
    sums = (dest / f"{name}.tar.gz.sha256").read_text().split()
    assert sums[0] == backup.sha256_file(archives[0]) and sums[1] == archives[0].name

    with tarfile.open(archives[0]) as tar:
        names = set(tar.getnames())
        manifest = json.load(tar.extractfile("MANIFEST.json"))
    assert "data/site.sqlite3" in names and "data/jobs/jobs.sqlite3" in names
    assert not any(n.endswith(("-wal", "-shm")) for n in names)  # snapshots are self-contained
    assert not any(n.startswith("data/tmp") for n in names)
    assert {d["path"] for d in manifest["databases"]} == {"site.sqlite3", "pinny.sqlite3", "jobs/jobs.sqlite3"}
    assert all(d["integrity"] == "ok" for d in manifest["databases"])
    assert {"path": "jobs.sqlite3", "target": "jobs/jobs.sqlite3"} in manifest["symlinks"]
    paths = {f["path"] for f in manifest["files"]}
    assert "models/verifier.pt" in paths and f"documents/{'a' * 64}/source.pdf" in paths

    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["uploaded"] and status["databases"] == 3
    assert not list((tmp_path / "state" / "work").glob("*.tar.gz"))  # removed after upload


def test_restore_verifies_and_keeps_every_row(data_dir, tmp_path, capsys):
    dest = run_backup(data_dir, tmp_path)
    archive = next(dest.glob("*.tar.gz"))
    target = tmp_path / "restored"
    assert restore.main([str(archive), "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert "archive sha256 ok" in out and "integrity_check ok" in out
    for name, rows in (("site.sqlite3", 50), ("pinny.sqlite3", 500), ("jobs.sqlite3", 20)):
        db = sqlite3.connect(target / name)
        assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == rows
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()
    assert os.readlink(target / "jobs.sqlite3") == "jobs/jobs.sqlite3"
    assert (target / "models" / "verifier.pt").read_bytes() == (data_dir / "models" / "verifier.pt").read_bytes()
    assert (target / "crops").is_dir() and not (target / "tmp").exists()


def test_restore_latest_from_a_file_destination(data_dir, tmp_path, monkeypatch):
    dest = run_backup(data_dir, tmp_path)
    monkeypatch.setenv("PINNY_BACKUP_DEST", dest.as_uri())
    target = tmp_path / "scratch" / "data"
    assert restore.main(["latest", "--profile", "production", "--target", str(target), "--cleanup"]) == 0
    assert not target.exists()  # --cleanup: verified, then deleted


def test_restore_refuses_a_non_empty_target_without_force(data_dir, tmp_path):
    dest = run_backup(data_dir, tmp_path)
    archive = next(dest.glob("*.tar.gz"))
    assert restore.main([str(archive), "--target", str(data_dir)]) == 2
    assert (data_dir / "site.sqlite3").exists()


def test_restore_detects_a_tampered_file(data_dir, tmp_path, capsys):
    dest = run_backup(data_dir, tmp_path)
    archive = next(dest.glob("*.tar.gz"))
    # Rewrite the archive with one document changed, keeping the manifest.
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(bad, "w:gz") as out:
        for m in src.getmembers():
            f = src.extractfile(m) if m.isfile() else None
            if m.name.endswith("source.pdf"):
                body = b"%PDF-1.7 tampered" + b"x" * (m.size - 17)
                f = io.BytesIO(body)
            out.addfile(m, f)
    assert restore.main([str(bad), "--target", str(tmp_path / "r")]) == 1
    assert "checksum mismatch" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


def test_restore_rejects_a_bad_archive_checksum(data_dir, tmp_path, capsys):
    dest = run_backup(data_dir, tmp_path)
    sums = next(dest.glob("*.sha256"))
    sums.write_text("0" * 64 + "  x.tar.gz\n")
    archive = next(dest.glob("*.tar.gz"))
    assert restore.main([str(archive), "--target", str(tmp_path / "r")]) == 1
    assert "checksum mismatch" in capsys.readouterr().err


def test_corrupt_database_fails_the_backup(tmp_path, capsys):
    d = tmp_path / "data"
    d.mkdir()
    (d / "broken.sqlite3").write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    rc = backup.main(["--data-dir", str(d), "--no-upload", "--state-dir", str(tmp_path / "state")])
    assert rc == 1
    assert "FAILED" in capsys.readouterr().err
    assert not (tmp_path / "state" / "status.json").exists()


def test_dbs_only_snapshot(data_dir, tmp_path):
    out = tmp_path / "pre-deploy"
    assert backup.main(["--data-dir", str(data_dir), "--dbs-only", "--out", str(out)]) == 0
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert len(manifest["databases"]) == 3
    db = sqlite3.connect(out / "pinny.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 500
    db.close()


def test_no_destination_is_an_error(data_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("PINNY_BACKUP_DEST", raising=False)
    monkeypatch.setenv("PINNY_BACKUP_BUCKET", "CHANGE_ME")
    assert backup.main(["--profile", "production", "--data-dir", str(data_dir),
                        "--state-dir", str(tmp_path / "s")]) == 2
