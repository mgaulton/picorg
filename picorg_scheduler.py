#!/usr/bin/env python3
"""Persistent, allow-listed PicOrg pipeline scheduler.

The scheduler is deliberately small: it orchestrates existing PicOrg scripts,
keeps a JSON status record for the LAN UI, and never accepts arbitrary shell
commands from the web layer.  Scheduled cycles are disabled by default and
high-confidence moves remain opt-in.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from identity_evidence_store import record_pipeline_run

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT_DIR / ".cache/picorg/scheduler.json"
DEFAULT_STATUS_PATH = ROOT_DIR / ".cache/picorg/scheduler-status.json"
DEFAULT_LOCK_PATH = Path("/tmp/picorg-scheduler-job.lock")
DEFAULT_DAEMON_LOCK_PATH = Path("/tmp/picorg-scheduler-daemon.lock")
DEFAULT_FACE_REBUILD_LOCK = Path(os.environ.get("PICORG_FACE_REBUILD_LOCK", "/tmp/picorg-face-rebuild.lock"))
MAX_OUTPUT_LINES = 80
JOB_NAMES = {"ingest", "name_audit", "reconcile_confirmed", "canonical_baseline", "evidence_health", "rebuild_faces", "full_pipeline", "refresh_matches", "refresh_ui", "cycle"}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 60,
    "run_ingest": True,
    "update_baseline": True,
    "check_evidence": True,
    # Periodic cycles are incremental by default.  A full face rebuild is an
    # explicit maintenance action because it can take hours and touches the
    # protected reference roots.
    "rebuild_faces": False,
    "migrate_confirmed": True,
    "apply_high_confidence": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(fallback)
    return payload if isinstance(payload, dict) else dict(fallback)


def normalize_config(payload: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(DEFAULT_CONFIG)
    if isinstance(payload, dict):
        result.update({key: payload[key] for key in DEFAULT_CONFIG if key in payload})
    try:
        result["interval_minutes"] = max(5, min(10080, int(result["interval_minutes"])) )
    except (TypeError, ValueError):
        result["interval_minutes"] = DEFAULT_CONFIG["interval_minutes"]
    for key in ("enabled", "run_ingest", "update_baseline", "check_evidence", "rebuild_faces", "migrate_confirmed", "apply_high_confidence"):
        result[key] = bool(result[key])
    return result


def read_config(path: Path) -> dict[str, Any]:
    return normalize_config(load_json(path, DEFAULT_CONFIG))


def read_status(path: Path) -> dict[str, Any]:
    return load_json(path, {"schema_version": 1, "running": False, "job": None, "output_tail": []})


def write_status(path: Path, **updates: Any) -> dict[str, Any]:
    state = read_status(path)
    state.update(updates)
    state["schema_version"] = 1
    state["updated_at"] = _now()
    _atomic_write(path, state)
    return state


def _base_env(config_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PICORG_SCHEDULER_CONFIG"] = str(config_path)
    env.setdefault("PICORG_UI_HOST", "0.0.0.0")
    env.setdefault("PICORG_EVIDENCE_DB", str(ROOT_DIR / ".cache/picorg/identity_evidence.sqlite3"))
    env.setdefault("PICORG_SCHEDULER_MANAGED", "1")
    # The systemd-managed review UI is refreshed explicitly after map work.
    # Prevent the legacy matcher script from trying to replace it via Screen.
    env["PICORG_RESTART_UI"] = "0"
    env["PICORG_UI_START"] = "0"
    env["LOCK_FILE"] = "/tmp/picorg-scheduler-runweb.lock"
    return env


def command_for(job: str, config: dict[str, Any]) -> list[str]:
    if job == "ingest":
        # Intake is coupled to the priority dedupe and precision-gated name
        # stage. Calling the remote mover alone bypasses that contract.
        return [str(ROOT_DIR / "run_name_org.sh"), "--ingest"]
    if job == "name_audit":
        return [str(ROOT_DIR / "picorg_manual.sh"), "dry-run"]
    if job == "reconcile_confirmed":
        return [str(ROOT_DIR / ".venv/bin/python"), str(ROOT_DIR / "reconcile_confirmed.py"), "--apply", "--allow-partial-health", "--evidence-db", str(ROOT_DIR / ".cache/picorg/identity_evidence.sqlite3")]
    if job == "canonical_baseline":
        return [str(ROOT_DIR / "build_canonical_face_baseline.sh")]
    if job == "evidence_health":
        return [
            str(ROOT_DIR / ".venv/bin/python"),
            str(ROOT_DIR / "identity_evidence_health.py"),
            "--db", str(ROOT_DIR / ".cache/picorg/identity_evidence.sqlite3"),
            "--output", str(ROOT_DIR / ".cache/picorg/evidence-health.json"),
            "--backup-dir", str(ROOT_DIR / ".cache/picorg/evidence-backups"),
            "--full",
        ]
    if job == "rebuild_faces":
        return [str(ROOT_DIR / "rebuild_face_data_recover.sh")]
    if job == "refresh_matches":
        command = [str(ROOT_DIR / "run_existing_face_db.sh")]
        if config.get("run_ingest"):
            command.append("--ingest")
        return command
    if job == "refresh_ui":
        return ["/usr/bin/systemctl", "restart", "picorg-review.service"]
    if job == "full_pipeline":
        if not config["rebuild_faces"]:
            return command_for("refresh_matches", config)
        command = [str(ROOT_DIR / "run_face_review_pipeline.sh"), "--no-ui"]
        if not config["run_ingest"]:
            command.append("--no-ingest")
        if not config["rebuild_faces"]:
            command.append("--reuse-face-db")
        if config["apply_high_confidence"]:
            command.append("--apply-high-confidence")
        return command
    raise ValueError(f"unsupported job: {job}")


def _append_output(lines: list[str], line: str) -> list[str]:
    value = line.strip()
    if not value:
        return lines
    lines.append(value[:500])
    return lines[-MAX_OUTPUT_LINES:]


def _progress_fields(line: str) -> dict[str, float | int]:
    """Extract bounded media progress from common PicOrg progress lines."""
    match = re.search(r"(?:processed|selected|embedded|folder)\s*[=:]\s*(\d+)\s*/\s*(\d+)", line, re.IGNORECASE)
    if not match:
        match = re.search(r"progress\s+[^()]*\((\d+)\s*/\s*(\d+)\)", line, re.IGNORECASE)
    if not match:
        return {}
    fields: dict[str, float | int] = {"completed": int(match.group(1)), "total": int(match.group(2))}
    rate = re.search(r"(?:rate|speed)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)", line, re.IGNORECASE)
    if rate:
        fields["rate_per_second"] = float(rate.group(1))
        if fields["rate_per_second"] > 0:
            fields["eta_seconds"] = max(0.0, (fields["total"] - fields["completed"]) / fields["rate_per_second"])
    return fields


def _run_command(job: str, command: list[str], config_path: Path, status_path: Path, env: dict[str, str]) -> int:
    started = datetime.now(timezone.utc)
    run_id = f"scheduler:{job}:{started.strftime('%Y%m%dT%H%M%S.%fZ')}"
    evidence_db = Path(env.get("PICORG_EVIDENCE_DB", str(ROOT_DIR / ".cache/picorg/identity_evidence.sqlite3")))
    output: list[str] = []
    def persist(status: str, stage: str, *, processed: int = 0, finished: bool = False, error: str | None = None, pid: int | None = None) -> None:
        try:
            record_pipeline_run(evidence_db, run_id=run_id, job=job, status=status, stage=stage, processed=processed, finished=finished, error=error, metadata={"pid": pid} if pid else {})
        except (OSError, RuntimeError, ValueError):
            # Scheduler status remains authoritative if the optional metadata
            # store is unavailable; never make a pipeline job fail only because
            # its telemetry database is offline.
            pass
    write_status(status_path, running=True, job=job, pid=None, started_at=started.isoformat(), finished_at=None, exit_code=None, stage="starting", output_tail=[])
    persist("running", "starting")
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        write_status(status_path, running=False, job=job, pid=None, finished_at=_now(), exit_code=127, stage="launch failed", error=str(exc), output_tail=[str(exc)])
        persist("error", "launch failed", finished=True, error=str(exc))
        return 127
    write_status(status_path, pid=process.pid, stage="running")
    persist("running", "running", pid=process.pid)
    last_write = 0.0
    processed = 0
    progress: dict[str, float | int] = {}
    try:
        heartbeat_seconds = max(5.0, float(os.environ.get("PICORG_SCHEDULER_HEARTBEAT_SECONDS", "30")))
    except ValueError:
        heartbeat_seconds = 30.0
    heartbeat_stop = threading.Event()
    heartbeat_state = {"stage": "running", "last_output": time.monotonic()}

    def heartbeat() -> None:
        while not heartbeat_stop.wait(heartbeat_seconds):
            if process.poll() is not None:
                return
            now = time.monotonic()
            silent = max(0, int(now - heartbeat_state["last_output"]))
            stage = str(heartbeat_state["stage"])
            message = f"{stage} | heartbeat: process alive, no output for {silent}s"
            print(f"[scheduler] {job}: {message}", flush=True)
            write_status(
                status_path,
                running=True,
                job=job,
                pid=process.pid,
                stage=message[:240],
                heartbeat=True,
                silent_seconds=silent,
                **progress,
            )

    heartbeat_thread = threading.Thread(target=heartbeat, name=f"picorg-{job}-heartbeat", daemon=True)
    heartbeat_thread.start()
    assert process.stdout is not None
    try:
        for line in process.stdout:
            processed += 1
            _append_output(output, line)
            progress.update(_progress_fields(line))
            now = time.monotonic()
            heartbeat_state["last_output"] = now
            heartbeat_state["stage"] = line.strip()[:240] or heartbeat_state["stage"]
            marker = line.lstrip()
            if now - last_write >= 1.0 or marker.startswith(("[", "error", "ERROR", "fatal", "FATAL")):
                stage = line.strip()[:240]
                write_status(status_path, running=True, job=job, pid=process.pid, stage=stage, output_tail=output, heartbeat=False, **progress)
                persist("running", stage, processed=processed, pid=process.pid)
                last_write = now
        exit_code = process.wait()
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
    write_status(status_path, running=False, job=job, pid=None, finished_at=_now(), exit_code=exit_code, stage="complete" if exit_code == 0 else "failed", output_tail=output, **progress)
    freshness_key = {
        "ingest": "last_intake_at",
        "refresh_matches": "last_map_refresh_at",
        "full_pipeline": "last_map_refresh_at",
    }.get(job)
    if exit_code == 0 and freshness_key:
        write_status(status_path, **{freshness_key: _now()})
    persist("complete" if exit_code == 0 else "error", "complete" if exit_code == 0 else "failed", processed=processed, finished=True, error=None if exit_code == 0 else f"exit {exit_code}")
    return exit_code


def _acquire(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _face_rebuild_active() -> bool:
    """Return whether another process currently owns the rebuild lock."""
    handle = _acquire(DEFAULT_FACE_REBUILD_LOCK)
    if handle is None:
        return True
    fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()
    return False


def run_job(job: str, config_path: Path, status_path: Path) -> int:
    config = read_config(config_path)
    lock = _acquire(DEFAULT_LOCK_PATH)
    if lock is None:
        # Do not overwrite the active job's status.  A second request should
        # report busy via its exit code while the UI continues to show the
        # owner, stage, and progress of the original job.
        return 2
    try:
        if _face_rebuild_active():
            write_status(
                status_path,
                running=False,
                job=None,
                stage="waiting for face rebuild",
                error="face rebuild is active; scheduled work deferred",
            )
            return 2
        code = _run_command(job, command_for(job, config), config_path, status_path, _base_env(config_path))
        if code == 0 and job in {"refresh_matches", "full_pipeline"}:
            return _run_command("refresh_ui", command_for("refresh_ui", config), config_path, status_path, _base_env(config_path))
        return code
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def run_cycle(config_path: Path, status_path: Path) -> int:
    config = read_config(config_path)
    lock = _acquire(DEFAULT_LOCK_PATH)
    if lock is None:
        # Preserve the active job's status when a duplicate cycle is requested.
        return 2
    try:
        if _face_rebuild_active():
            write_status(
                status_path,
                running=False,
                job=None,
                stage="waiting for face rebuild",
                error="face rebuild is active; scheduled cycle deferred",
            )
            return 2
        if not config["rebuild_faces"]:
            if config["run_ingest"]:
                code = _run_command("ingest", command_for("ingest", config), config_path, status_path, _base_env(config_path))
                if code:
                    return code
            if config["migrate_confirmed"]:
                code = _run_command("reconcile_confirmed", command_for("reconcile_confirmed", config), config_path, status_path, _base_env(config_path))
                if code:
                    return code
            if config["update_baseline"]:
                code = _run_command("canonical_baseline", command_for("canonical_baseline", config), config_path, status_path, _base_env(config_path))
                if code:
                    return code
            if config["check_evidence"]:
                code = _run_command("evidence_health", command_for("evidence_health", config), config_path, status_path, _base_env(config_path))
                if code:
                    return code
            # Intake already ran before confirmation/baseline stages. Keep
            # refresh_matches' standalone --ingest behavior, but suppress it
            # inside this cycle to avoid a second mover invocation.
            refresh_config = {**config, "run_ingest": False}
            code = _run_command("refresh_matches", command_for("refresh_matches", refresh_config), config_path, status_path, _base_env(config_path))
            if code:
                return code
            return _run_command("refresh_ui", command_for("refresh_ui", config), config_path, status_path, _base_env(config_path))
        if config["run_ingest"]:
            code = _run_command("ingest", command_for("ingest", config), config_path, status_path, _base_env(config_path))
            if code:
                return code
        if config["migrate_confirmed"]:
            code = _run_command("reconcile_confirmed", command_for("reconcile_confirmed", config), config_path, status_path, _base_env(config_path))
            if code:
                return code
        if config["update_baseline"]:
            code = _run_command("canonical_baseline", command_for("canonical_baseline", config), config_path, status_path, _base_env(config_path))
            if code:
                return code
        if config["check_evidence"]:
            code = _run_command("evidence_health", command_for("evidence_health", config), config_path, status_path, _base_env(config_path))
            if code:
                return code
        code = _run_command("full_pipeline", command_for("full_pipeline", {**config, "run_ingest": False}), config_path, status_path, _base_env(config_path))
        if code:
            return code
        return _run_command("refresh_ui", command_for("refresh_ui", config), config_path, status_path, _base_env(config_path))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def daemon(config_path: Path, status_path: Path) -> int:
    lock = _acquire(DEFAULT_DAEMON_LOCK_PATH)
    if lock is None:
        # Preserve the live daemon heartbeat instead of replacing it with a
        # misleading stopped state when a duplicate start is attempted.
        return 2
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        write_status(status_path, daemon_running=True, daemon_pid=os.getpid(), daemon_started_at=_now(), daemon_error=None)
        next_run = datetime.now(timezone.utc)
        while not stop:
            config = read_config(config_path)
            now = datetime.now(timezone.utc)
            current = read_status(status_path)
            if config["enabled"] and not current.get("running") and now >= next_run:
                run_cycle(config_path, status_path)
                next_run = datetime.now(timezone.utc) + timedelta(minutes=config["interval_minutes"])
            write_status(status_path, next_run_at=next_run.isoformat(), daemon_running=True, daemon_pid=os.getpid())
            time.sleep(5)
    finally:
        write_status(status_path, daemon_running=False, daemon_pid=None, daemon_stopped_at=_now())
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "cycle", "daemon", "status"])
    parser.add_argument("job", nargs="?", choices=sorted(JOB_NAMES - {"cycle"}))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("PICORG_SCHEDULER_CONFIG", DEFAULT_CONFIG_PATH)))
    parser.add_argument("--status", dest="status_path", type=Path, default=Path(os.environ.get("PICORG_SCHEDULER_STATUS", DEFAULT_STATUS_PATH)))
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps({"config": read_config(args.config), "status": read_status(args.status_path)}, sort_keys=True))
        return 0
    if args.command == "run":
        if not args.job:
            parser.error("run requires a job")
        return run_job(args.job, args.config, args.status_path)
    if args.command == "cycle":
        return run_cycle(args.config, args.status_path)
    return daemon(args.config, args.status_path)


if __name__ == "__main__":
    raise SystemExit(main())
