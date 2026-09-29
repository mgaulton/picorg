from __future__ import annotations

import sqlite3

from identity_evidence_health import backup_database, check_database, version_is_wal_fixed


def create_db(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE media (sha256 TEXT PRIMARY KEY, current_path TEXT);
        CREATE TABLE face_observations (observation_id INTEGER PRIMARY KEY);
        CREATE TABLE face_markers (marker_key TEXT PRIMARY KEY);
        CREATE TABLE assignment_queue (assignment_id INTEGER PRIMARY KEY);
        CREATE TABLE pipeline_runs (run_id TEXT PRIMARY KEY);
        INSERT INTO media VALUES ('a', '/tmp/a.jpg');
        """
    )
    connection.commit()
    connection.close()


def test_version_gate_accepts_documented_fixes():
    assert version_is_wal_fixed("3.51.3")
    assert version_is_wal_fixed("3.50.7")
    assert version_is_wal_fixed("3.44.6")
    assert not version_is_wal_fixed("3.40.1")


def test_health_and_online_backup(tmp_path):
    database = tmp_path / "evidence.sqlite3"
    create_db(database)
    report = check_database(database, backup_dir=tmp_path / "backups", full_integrity=True)
    assert report["healthy"] is True
    assert report["checks"]["quick_check"]["ok"] is True
    assert report["checks"]["integrity_check"]["ok"] is True
    backup = report["backup"]
    assert backup
    assert (tmp_path / "backups").joinpath(str(backup).split("/")[-1]).is_file()

    connection = sqlite3.connect(backup)
    assert connection.execute("SELECT current_path FROM media").fetchone() == ("/tmp/a.jpg",)
    connection.close()


def test_missing_database_fails_closed(tmp_path):
    report = check_database(tmp_path / "missing.sqlite3")
    assert report["healthy"] is False
    assert report["error"] == "database_missing"
