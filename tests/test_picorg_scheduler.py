from __future__ import annotations

import json
from pathlib import Path

import picorg_scheduler as scheduler


def test_scheduler_defaults_are_safe_and_normalized():
    config = scheduler.normalize_config({"enabled": "yes", "interval_minutes": 1, "apply_high_confidence": True})
    assert config["enabled"] is True
    assert config["interval_minutes"] == 5
    assert config["apply_high_confidence"] is True
    assert config["migrate_confirmed"] is True
    assert config["update_baseline"] is True
    assert config["check_evidence"] is True


def test_full_pipeline_command_is_allowlisted_and_no_ui(tmp_path):
    command = scheduler.command_for("full_pipeline", {**scheduler.DEFAULT_CONFIG, "run_ingest": False, "rebuild_faces": True, "apply_high_confidence": False})
    assert command[0].endswith("run_face_review_pipeline.sh")
    assert "--no-ui" in command
    assert "--no-ingest" in command
    assert "--apply-high-confidence" not in command


def test_periodic_default_uses_incremental_existing_db_refresh():
    config = scheduler.normalize_config({})
    assert config["rebuild_faces"] is False
    command = scheduler.command_for("refresh_matches", config)
    assert command[0].endswith("run_existing_face_db.sh")
    assert "--ingest" in command


def test_refresh_ui_restarts_managed_service_and_scheduler_disables_nested_ui():
    command = scheduler.command_for("refresh_ui", scheduler.DEFAULT_CONFIG)
    assert command == ["/usr/bin/systemctl", "restart", "picorg-review.service"]
    env = scheduler._base_env(Path("/tmp/scheduler.json"))
    assert env["PICORG_RESTART_UI"] == "0"
    assert env["PICORG_UI_START"] == "0"
    assert env["LOCK_FILE"] == "/tmp/picorg-scheduler-runweb.lock"


def test_empty_scheduler_config_starts_with_safe_hourly_defaults():
    config = scheduler.normalize_config({})
    assert config["interval_minutes"] == 60
    assert config["enabled"] is False
    assert config["apply_high_confidence"] is False


def test_canonical_baseline_is_an_allowlisted_local_job():
    command = scheduler.command_for("canonical_baseline", scheduler.DEFAULT_CONFIG)
    assert command == ["/opt/picorg/build_canonical_face_baseline.sh"]


def test_evidence_health_is_an_allowlisted_full_integrity_job():
    command = scheduler.command_for("evidence_health", scheduler.DEFAULT_CONFIG)
    assert command[0].endswith(".venv/bin/python")
    assert command[1].endswith("identity_evidence_health.py")
    assert "--full" in command
    assert "--backup-dir" in command


def test_status_round_trip_is_atomic(tmp_path):
    status_path = tmp_path / "status.json"
    scheduler.write_status(status_path, running=True, job="ingest", output_tail=["stage"])
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["running"] is True
    assert payload["job"] == "ingest"
    assert payload["output_tail"] == ["stage"]


def test_duplicate_job_does_not_clobber_active_status(tmp_path, monkeypatch):
    lock_path = tmp_path / "job.lock"
    status_path = tmp_path / "status.json"
    scheduler.write_status(status_path, running=True, job="rebuild_faces", stage="coalescing references")
    lock = scheduler._acquire(lock_path)
    assert lock is not None
    monkeypatch.setattr(scheduler, "DEFAULT_LOCK_PATH", lock_path)
    try:
        assert scheduler.run_job("ingest", tmp_path / "config.json", status_path) == 2
    finally:
        lock.close()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["running"] is True
    assert payload["job"] == "rebuild_faces"
    assert payload["stage"] == "coalescing references"


def test_job_defers_while_face_rebuild_lock_is_held(tmp_path, monkeypatch):
    job_lock = tmp_path / "job.lock"
    rebuild_lock = tmp_path / "face-rebuild.lock"
    status_path = tmp_path / "status.json"
    held = scheduler._acquire(rebuild_lock)
    assert held is not None
    monkeypatch.setattr(scheduler, "DEFAULT_LOCK_PATH", job_lock)
    monkeypatch.setattr(scheduler, "DEFAULT_FACE_REBUILD_LOCK", rebuild_lock)
    try:
        assert scheduler.run_job("ingest", tmp_path / "config.json", status_path) == 2
    finally:
        held.close()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["stage"] == "waiting for face rebuild"
    assert payload["running"] is False
