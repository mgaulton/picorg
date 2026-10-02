#!/usr/bin/env python3
"""Local review UI for unmatched image clusters.

Explicit image assignments are stored in a separate JSON ledger and moved into
the configured canonical identity tree; protected source roots are rejected.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import signal
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import picorg_sorter as sorter
import picorg_scheduler as scheduler
from run_artifact_store import resolve_current as resolve_current_run
from identity_evidence_store import (
    list_assignment_queue as list_durable_assignment_queue,
    queue_assignment as queue_durable_assignment,
    update_assignment_status as update_durable_assignment_status,
)

LOGGER = logging.getLogger(__name__)


DEFAULT_AUDIT_ROOT = Path(tempfile.gettempdir()) / "picorg_sorted_audit"
DEFAULT_CURRENT_RUN_POINTER = Path(os.environ.get("PICORG_CURRENT_RUN_POINTER", "/opt/picorg/.cache/picorg/current-run.json"))
DEFAULT_DECISIONS = Path("/opt/picorg/review_decisions.json")
DEFAULT_IMAGE_DECISIONS = Path("/opt/picorg/review_image_decisions.json")
DEFAULT_REVIEW_IDENTITIES = Path("/opt/picorg/review_identities.json")
DEFAULT_FACE_MARKERS = Path("/opt/picorg/identity_face_markers.json")
DEFAULT_MOVE_HISTORY = Path("/opt/picorg/review_move_history.json")
DEFAULT_REVIEW_LEDGER = Path("/opt/picorg/review_decision_ledger.jsonl")
DEFAULT_PENDING_ASSIGNMENTS = Path("/opt/picorg/.cache/picorg/pending-review-assignments.json")
DEFAULT_EVIDENCE_DB = Path(os.environ.get("PICORG_EVIDENCE_DB", "/opt/picorg/.cache/picorg/identity_evidence.sqlite3"))
DEFAULT_EVIDENCE_HEALTH = Path(os.environ.get("PICORG_EVIDENCE_HEALTH", "/opt/picorg/.cache/picorg/evidence-health.json"))
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".mp4", ".mov"}
FAMILIES = {"linked", "manual", "metadaily", "redditdaily", "reddit_follow", "reddit_subreddit", "pscrape", "review"}
BASELINE_IDENTITY_FAMILIES = {"linked", "manual", "metadaily", "redditdaily", "reddit_follow", "review"}
DECISION_STATUSES = {"pending", "confirmed", "rejected", "needs-evidence"}
DEFAULT_REGISTRY = Path("/opt/picorg/project_registry.json")
# LAN exposure is an explicit deployment requirement; override HOST for local-only use.
DEFAULT_HOST = "0.0.0.0"  # nosec B104
DEFAULT_PORT = 8787  # matches /opt/service_configurations.json (Readarr retired)
DEFAULT_OVERRIDES = Path("/opt/picorg/review_overrides.json")
DEFAULT_REVIEW_DEST_ROOT = Path(os.environ.get("PICORG_REVIEW_DEST_ROOT", str(sorter.DEST_ROOT)))
DEFAULT_FACE_REBUILD_LOCK = Path(os.environ.get("PICORG_FACE_REBUILD_LOCK", "/tmp/picorg-face-rebuild.lock"))
DEFAULT_PIPELINE_LOCK = Path(os.environ.get("PICORG_UI_PIPELINE_LOCK", "/tmp/picorg-pipeline.lock"))
DEFAULT_REPAIR_LEDGER = Path(os.environ.get("PICORG_REPAIR_LEDGER", "/opt/picorg/.cache/picorg/repair_ledger.jsonl"))
DEFAULT_REBUILD_LOG = Path(os.environ.get("PICORG_FACE_RECOVERY_LOG", "/tmp/picorg-face-rebuild-recovery.log"))
DEFAULT_SCHEDULER_CONFIG = Path(os.environ.get("PICORG_SCHEDULER_CONFIG", str(scheduler.DEFAULT_CONFIG_PATH)))
DEFAULT_SCHEDULER_STATUS = Path(os.environ.get("PICORG_SCHEDULER_STATUS", str(scheduler.DEFAULT_STATUS_PATH)))
DEFAULT_IDENTITY_PICKER_SETTINGS = Path(os.environ.get(
    "PICORG_IDENTITY_PICKER_SETTINGS", "/opt/picorg/.cache/picorg/identity-picker-settings.json"
))
DEFAULT_RECENT_CHOICES = Path(os.environ.get(
    "PICORG_RECENT_CHOICES", "/opt/picorg/.cache/picorg/recent-choices.json"
))
DEFAULT_IDENTITY_RECONCILIATION_DECISIONS = Path(os.environ.get(
    "PICORG_IDENTITY_RECONCILIATION_DECISIONS",
    "/opt/picorg/.cache/picorg/identity-reconciliation-review.json",
))
DEFAULT_IDENTITY_PICKER_PREFIXES = ("fbhottie", "redhottie", "reddcutie", "frecklehottie", "gothbaddie", "twins")
DEFAULT_ASSORTED_ROOT = Path(os.environ.get("PICORG_ASSORTED_ROOT", "/mnt/assorted"))
DEFAULT_ASSORTED_ASSOCIATIONS = Path(
    os.environ.get("PICORG_ASSORTED_ASSOCIATIONS", "/opt/picorg/.cache/picorg/assorted-folder-associations.json")
)
DEFAULT_MANUAL_GROUPS = Path(os.environ.get("PICORG_MANUAL_GROUPS", "/opt/picorg/manual_groups.json"))
DEFAULT_MANUAL_GROUP_ACTIONS = Path(os.environ.get(
    "PICORG_MANUAL_GROUP_ACTIONS", "/opt/picorg/.cache/picorg/manual-group-actions.json"
))
SCHEDULER_SCRIPT = Path(__file__).with_name("picorg_scheduler.py")
DEFAULT_TRUSTED_CIDRS = (
    "192.168.2.0/24,"
    "127.0.0.0/8,"
    "169.254.0.0/16,"
    "::1/128,"
    "fe80::/10"
)


def parse_trusted_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse trusted LAN CIDRs; invalid entries fail closed and are logged."""
    networks = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            LOGGER.warning("ignoring invalid PICORG_UI_TRUSTED_CIDRS entry: %s", value)
    return tuple(networks)


def face_rebuild_is_active(lock_path: Path = DEFAULT_FACE_REBUILD_LOCK) -> bool:
    """Return whether the face rebuild lock is currently held by another process."""
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        LOGGER.warning("unable to inspect face rebuild lock: %s", lock_path)
        return True
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def pipeline_lock_is_active(lock_path: Path | None = None) -> bool:
    """Return whether a pipeline job owns the shared pipeline lock.

    Direct unit-test/library users do not opt into pipeline awareness unless
    ``PICORG_UI_PIPELINE_LOCK`` is set.  The production ``runweb.sh`` launcher
    always sets it, avoiding accidental lock coupling for embedded callers.
    """
    if lock_path is None:
        raw = os.environ.get("PICORG_UI_PIPELINE_LOCK")
        if not raw:
            return False
        lock_path = Path(raw)
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        LOGGER.warning("unable to inspect pipeline lock: %s", lock_path)
        return True
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _rebuild_stage(command: str) -> tuple[int, str]:
    """Map a rebuild command line to a safe, operator-facing stage label."""
    stages = (
        (10, "coalescing references", "coalesce_face_references.py"),
        (20, "extracting face embeddings", "rebuild_face_database.py"),
        (20, "extracting face embeddings", "picorg_face_database.py"),
        (30, "validating face database", "verify_face_database.py"),
        (40, "building exemplar gallery", "select_reference_gallery.py"),
        (50, "orchestrating rebuild", "rebuild_face_data.sh"),
        (60, "recovery wrapper", "rebuild_face_data_recover.sh"),
    )
    for priority, label, marker in stages:
        if marker in command:
            return priority, label
    return 90, "rebuild in progress"


def _process_elapsed_seconds(pid: int) -> float | None:
    """Read Linux process start ticks without invoking a shell command."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        stat_tail = stat_text.rsplit(") ", 1)[1].split()
        start_ticks = int(stat_tail[19])
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return max(0.0, uptime - start_ticks / clock_ticks)
    except (OSError, IndexError, ValueError, TypeError):
        return None


def _rebuild_process_snapshot() -> dict[str, Any]:
    """Return bounded process metadata; never expose command lines or paths."""
    processes: list[dict[str, Any]] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            command = entry.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        try:
            command_parts = shlex.split(command)
        except ValueError:
            continue
        rebuild_markers = {
            "rebuild_face_data.sh",
            "rebuild_face_data_recover.sh",
            "coalesce_face_references.py",
            "rebuild_face_database.py",
            "picorg_face_database.py",
            "verify_face_database.py",
            "select_reference_gallery.py",
        }
        if not any(Path(part).name in rebuild_markers for part in command_parts):
            continue
        priority, stage = _rebuild_stage(command)
        item: dict[str, Any] = {"pid": int(entry.name), "stage": stage, "priority": priority}
        elapsed = _process_elapsed_seconds(int(entry.name))
        if elapsed is not None:
            item["elapsed_seconds"] = round(elapsed, 1)
        processes.append(item)
    processes.sort(key=lambda item: (item["priority"], item["pid"]))
    active = processes[0] if processes else None
    if active is None:
        return {"process": None, "process_count": 0}
    return {"process": {key: value for key, value in active.items() if key != "priority"}, "process_count": len(processes)}


def _rebuild_progress_snapshot(path: Path, *, running: bool = False) -> dict[str, str]:
    """Return bounded rebuild progress or the latest terminal outcome.

    Terminal messages are reduced to safe categories so source paths from
    recovery logs are never exposed through the LAN UI.
    """
    try:
        data = path.read_bytes()[-65536:]
    except OSError:
        return {}
    lines = data.decode("utf-8", "replace").splitlines()
    if running:
        for raw_line in reversed(lines):
            if not raw_line.startswith("[health]"):
                continue
            progress = raw_line.split(" progress=", 1)[1].strip() if " progress=" in raw_line else "heartbeat active"
            if progress:
                return {"message": progress[:240], "outcome": "running"}
    else:
        for raw_line in reversed(lines):
            if "source root timed out after" in raw_line:
                return {"message": "last rebuild failed: source root timeout", "outcome": "failed", "reason": "source root timeout"}
            if raw_line.startswith("error: rebuild failed") or "rebuild failed after" in raw_line:
                return {"message": "last rebuild failed; previous database was restored", "outcome": "failed", "reason": "rebuild failed"}
            if "[recovery] rebuild completed successfully" in raw_line:
                return {"message": "last rebuild completed successfully", "outcome": "success"}
    return {}


def _scheduler_pid_alive(pid: Any) -> bool:
    try:
        numeric = int(pid)
        command = Path(f"/proc/{numeric}/cmdline").read_bytes().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        return False
    return "picorg_scheduler.py" in command


def _scheduler_snapshot(config_path: Path, status_path: Path) -> dict[str, Any]:
    config = scheduler.read_config(config_path)
    status = scheduler.read_status(status_path)
    job_pid = status.get("pid")
    daemon_pid = status.get("daemon_pid")
    return {
        "config": config,
        "status": status,
        "running": bool(status.get("running") and _scheduler_pid_alive(job_pid)),
        "daemon_running": bool(status.get("daemon_running") and _scheduler_pid_alive(daemon_pid)),
    }


def _launch_scheduler_process(command: str, job: str | None, config_path: Path, status_path: Path) -> dict[str, Any]:
    snapshot = _scheduler_snapshot(config_path, status_path)
    if command == "daemon" and snapshot["daemon_running"]:
        raise RuntimeError("scheduler daemon is already running")
    if command in {"run", "cycle"} and snapshot["running"]:
        raise RuntimeError("a PicOrg job is already running")
    args = [sys.executable, str(SCHEDULER_SCRIPT), command]
    if command == "run":
        if not job or job not in (scheduler.JOB_NAMES - {"cycle"}):
            raise ValueError("unsupported scheduler job")
        args.append(job)
    args.extend(["--config", str(config_path), "--status", str(status_path)])
    process = subprocess.Popen(
        args,
        cwd=SCHEDULER_SCRIPT.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"pid": process.pid, "command": command, "job": job}


def _repair_ledger_summary(path: Path, cache: dict[str, Any]) -> dict[str, Any]:
    """Incrementally summarize the append-only repair ledger for polling clients."""
    def cached_latest_update() -> str | None:
        latest = cache.get("latest_timestamp")
        if isinstance(latest, (int, float)):
            return datetime.fromtimestamp(latest, timezone.utc).isoformat()
        return None

    try:
        stat = path.stat()
    except OSError:
        return {"available": False, "records": 0, "status_counts": {}, "latest_update": cached_latest_update()}
    key = (stat.st_dev, stat.st_ino)
    if cache.get("key") != key or stat.st_size < int(cache.get("offset", 0)):
        cache.clear()
        cache.update({"key": key, "offset": 0, "records": 0, "status_counts": Counter(), "latest_timestamp": None})
    try:
        with path.open("rb") as handle:
            handle.seek(int(cache.get("offset", 0)))
            chunk = handle.read()
    except OSError:
        return {"available": False, "records": int(cache.get("records", 0)), "status_counts": dict(cache.get("status_counts", {})), "latest_update": cached_latest_update()}
    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        return {"available": True, "records": int(cache.get("records", 0)), "status_counts": dict(cache.get("status_counts", {})), "latest_update": cached_latest_update()}
    complete = chunk[: last_newline + 1]
    cache["offset"] = int(cache.get("offset", 0)) + len(complete)
    counts: Counter = cache["status_counts"]
    for line in complete.splitlines():
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        cache["records"] = int(cache.get("records", 0)) + 1
        status = str(record.get("status") or "unknown")
        counts[status] += 1
        timestamp = record.get("timestamp")
        if isinstance(timestamp, (int, float)):
            latest = cache.get("latest_timestamp")
            if latest is None or timestamp > latest:
                cache["latest_timestamp"] = timestamp
    latest_iso = None
    if isinstance(cache.get("latest_timestamp"), (int, float)):
        latest_iso = datetime.fromtimestamp(cache["latest_timestamp"], timezone.utc).isoformat()
    return {
        "available": True,
        "records": int(cache.get("records", 0)),
        "status_counts": dict(counts),
        "latest_update": latest_iso,
    }


def iter_media_files(root: Path) -> Iterable[Path]:
    """Yield readable media below the sorted identity tree without failing on one bad file."""
    if not root.is_dir():
        return
    try:
        candidates = root.rglob("*")
    except OSError:
        return
    for path in candidates:
        try:
            if path.suffix.lower() in MEDIA_EXTENSIONS and path.is_file() and not path.is_symlink():
                yield path.resolve()
        except OSError:
            continue


def latest_audit(audit_root: Path = DEFAULT_AUDIT_ROOT) -> Path:
    current = resolve_current_run(DEFAULT_CURRENT_RUN_POINTER)
    # A caller may provide an isolated audit directory (tests, an offline
    # export, or a recovery bundle).  Never let the global current-run pointer
    # escape that scope and silently select an unrelated production audit.
    try:
        current_in_scope = current is not None and (
            current.resolve().parent == audit_root.resolve()
            or audit_root.resolve() in current.resolve().parents
        )
    except OSError:
        current_in_scope = False
    if current_in_scope:
        return current
    # Derived face/preflight/cluster reports are JSON too, but are not valid
    # name-audit inputs.  The standalone UI must select a primary audit just as
    # runweb.sh does when no explicit --audit is supplied.
    derived_suffixes = (".preflight.json", ".face-clusters.json", ".reconciled.json", ".clusters.json")
    candidates = [
        path for path in audit_root.glob("*.json")
        if not path.name.endswith(derived_suffixes)
    ]
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            load_audit(path)
            return path
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    raise FileNotFoundError(f"No primary audit JSON files found under {audit_root}")


def load_audit(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError(f"Invalid audit payload: {path}")
    return payload


def _cluster_key(result: Dict[str, Any], scan_roots: set[Path]) -> str:
    # Face matches stay authoritative; named folders only organize records
    # that do not have a face-cluster assignment.
    face_cluster_id = str(result.get("face_cluster_id") or "").strip()
    if face_cluster_id:
        return f"face:{face_cluster_id}"
    path = str(result.get("path") or "").strip()
    folder = Path(path).parent if path else None
    containing_roots = [root for root in scan_roots if folder and (folder == root or root in folder.parents)]
    scan_root = max(containing_roots, key=lambda item: len(item.parts), default=None)
    if folder and folder.name and scan_root and folder != scan_root:
        return f"folder:{folder.as_posix()}"
    # Root-level intake files retain the previous title grouping.
    title = str(result.get("title") or Path(path).stem)
    base = sorter.gallery_base_title(title)
    return sorter.normalize_key(base) or sorter.normalize_key(title) or "unlabeled"


def _cluster_id(key: str, paths: Iterable[str]) -> str:
    material = "|".join([key, *sorted(paths)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _cluster_purity_flags(cluster: Dict[str, Any]) -> List[str]:
    """Return reasons a cluster should not be bulk-confirmed without review."""
    flags: List[str] = []
    if int(cluster.get("count") or 0) >= 100:
        flags.append("large_cluster")
    if not cluster.get("expected_identities"):
        flags.append("no_expected_identity")
    face_labels = {str(label) for label in cluster.get("face_cluster_labels") or [] if str(label)}
    if not face_labels:
        flags.append("no_face_labels")
    elif len(face_labels) > 1:
        flags.append("multiple_face_clusters")
    if len(cluster.get("families") or []) > 1:
        flags.append("multiple_source_families")
    return flags


def _cluster_quality_rank(cluster: Dict[str, Any]) -> int:
    flags = set(_cluster_purity_flags(cluster))
    if flags.intersection({"multiple_face_clusters", "multiple_source_families", "no_face_labels"}):
        return 0
    if flags.intersection({"large_cluster", "no_expected_identity"}):
        return 1
    return 2


def build_clusters(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    roots = {
        Path(str(result.get("source_root")))
        for result in payload["results"]
        if isinstance(result, dict) and result.get("source_root")
    }
    scan_roots = {
        root for root in roots
        if not any(other != root and other in root.parents for other in roots)
    }
    for result in payload["results"]:
        if not isinstance(result, dict) or result.get("canonical"):
            continue
        path = str(result.get("path") or "")
        if not path:
            continue
        grouped.setdefault(_cluster_key(result, scan_roots), []).append(result)

    clusters: List[Dict[str, Any]] = []
    for key, results in grouped.items():
        paths = [str(item["path"]) for item in results]
        title = Path(key[len("folder:"):]).name if key.startswith("folder:") else str(results[0].get("title") or Path(paths[0]).stem)
        cluster = {
                "cluster_id": _cluster_id(key, paths),
                "key": key,
                "title": title,
                "count": len(results),
                "paths": paths,
                "sample_paths": paths[:12],
                "expected_identities": sorted(
                    {str(item["expected_identity"]) for item in results if item.get("expected_identity")}
                ),
                "source_roots": sorted({str(item.get("source_root") or "") for item in results}),
                "families": sorted({str(item.get("source_family") or "unknown") for item in results}),
                "face_cluster_labels": sorted(
                    {str(item["cluster_label"]) for item in results if item.get("cluster_label")}
                ),
                "face_link_scores": {
                    str(item["path"]): float(item["face_link_similarity"])
                    for item in results
                    if item.get("face_link_similarity") is not None
                },
                "review_methods": sorted(
                    {str(item["review_method"]) for item in results if item.get("review_method")}
                ),
            }
        cluster["quality_rank"] = _cluster_quality_rank(cluster)
        cluster["quality_label"] = {2: "Strong", 1: "Review", 0: "Mixed evidence"}[cluster["quality_rank"]]
        clusters.append(cluster)
    return sorted(clusters, key=lambda item: (-item["count"], -item["quality_rank"], item["title"].casefold()))


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _append_review_ledger(path: Path, event: Dict[str, Any]) -> None:
    """Append an auditable review event without rewriting prior decisions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

def _read_deleted_media_path_keys(path: Path) -> set[str]:
    """Return normalized paths with a successful permanent-delete ledger event."""
    deleted: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                media_path = str(event.get("path") or "")
                if event.get("event") == "media_deleted" and media_path:
                    deleted.add(_image_decision_key(media_path))
    except OSError:
        pass
    return deleted


def load_cluster_index(audit_path: Path, cache_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load the cluster index from a cache, rebuilding only when the audit changes."""
    cache_path = cache_path or audit_path.with_suffix(".clusters.json")
    stamp = audit_path.stat()
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("schema_version") == 4 and cached.get("audit") == {"mtime_ns": stamp.st_mtime_ns, "size": stamp.st_size}:
            clusters = cached.get("clusters")
            if isinstance(clusters, list) and all(isinstance(item, dict) and "paths" in item for item in clusters):
                return clusters
    except (OSError, json.JSONDecodeError):
        pass
    clusters = build_clusters(load_audit(audit_path))
    _atomic_json_write(cache_path, {"schema_version": 4, "audit": {"mtime_ns": stamp.st_mtime_ns, "size": stamp.st_size}, "clusters": clusters})
    return clusters


def load_preflight_statuses(path: Path | None) -> Dict[str, str]:
    if not path or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(item["path"]): str(item["status"]) for item in payload.get("records", []) if isinstance(item, dict) and item.get("path") and item.get("status")}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def load_preflight_records(path: Path | None) -> List[Dict[str, Any]]:
    """Load bounded, explainable preflight records for the needs-attention queue."""
    if not path or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records", []) if isinstance(payload, dict) else []
        return [item for item in records if isinstance(item, dict) and item.get("path") and item.get("status")]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def build_attention_queue(
    payload: Dict[str, Any],
    preflight_records: List[Dict[str, Any]] | None = None,
    purity_payload: Dict[str, Any] | None = None,
    limit: int = 2000,
) -> Dict[str, Any]:
    """Build report-only attention items; never treats them as non-matches."""
    items: List[Dict[str, Any]] = []
    counts: Counter[str] = Counter()

    def add(category: str, *, path: str | None = None, status: str | None = None, reason: str = "", count: int = 1) -> None:
        counts[category] += count
        if len(items) >= limit:
            return
        item_id = hashlib.sha256(f"{category}|{path or ''}|{reason}".encode("utf-8")).hexdigest()[:16]
        items.append({"id": item_id, "category": category, "path": path, "status": status, "reason": reason, "count": count})

    preflight_labels = {
        "corrupt": "unreadable",
        "missing": "missing",
        "oversized": "oversized",
        "unsupported_extension": "unsupported",
        "skipped": "skipped",
    }
    for record in preflight_records or []:
        raw_status = str(record.get("status") or "").strip().lower()
        category = preflight_labels.get(raw_status)
        if category:
            add(category, path=str(record.get("path")), status=raw_status, reason=str(record.get("reason") or "preflight excluded"))

    report = payload.get("report") or {}
    for error in report.get("error_samples") or []:
        if isinstance(error, dict):
            add("unreadable", path=str(error.get("path") or "") or None, status="error", reason=str(error.get("message") or "decoder error"))
    for key, category, reason in (
        ("multi_face_deferred", "deferred", "multiple faces or ambiguous face assignment"),
        ("no_face", "no-face", "no usable face detected"),
        ("low_quality", "low-quality", "face quality below extraction gate"),
    ):
        count = int(report.get(key) or 0)
        if count:
            add(category, status="deferred", reason=reason, count=count)

    if purity_payload:
        conflict_count = int(purity_payload.get("conflicting_labels") or 0)
        if conflict_count:
            add("conflict", status="conflicting", reason="confirmed labels disagree within evidence", count=conflict_count)
        resolver = purity_payload.get("resolver") or {}
        for key, category, reason in (
            ("missing", "missing", "confirmed marker did not resolve to the active audit"),
            ("ambiguous", "conflict", "marker basename matched more than one path"),
        ):
            count = int(resolver.get(key) or 0)
            if count:
                add(category, status=key, reason=reason, count=count)

    return {"items": items, "counts": dict(sorted(counts.items())), "total": sum(counts.values()), "truncated": sum(counts.values()) > len(items)}


def filter_clusters_by_preflight(clusters: List[Dict[str, Any]], statuses: Dict[str, str]) -> int:
    """Remove paths known to be invalid before they reach the review UI."""
    if not statuses:
        return 0
    hidden = 0
    valid_statuses = {"candidate"}
    for cluster in clusters:
        original = list(cluster.get("paths", []))
        cluster["paths"] = [path for path in original if statuses.get(path, "candidate") in valid_statuses]
        hidden += len(original) - len(cluster["paths"])
        cluster["count"] = len(cluster["paths"])
        cluster["sample_paths"] = cluster["paths"][:12]
    clusters[:] = [cluster for cluster in clusters if cluster["paths"]]
    return hidden


def _read_overrides(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"moves": {}, "removed": []}
    return {"moves": payload.get("moves", {}), "removed": payload.get("removed", [])}


def _apply_overrides(clusters: List[Dict[str, Any]], overrides: Dict[str, Any]) -> None:
    by_id = {item["cluster_id"]: item for item in clusters}
    moves = {str(path): str(target) for path, target in (overrides.get("moves") or {}).items() if str(target) in by_id}
    removed = {str(path) for path in (overrides.get("removed") or [])}
    for cluster in clusters:
        cluster["paths"] = [path for path in cluster.get("paths", []) if path not in removed and moves.get(path, cluster["cluster_id"]) == cluster["cluster_id"]]
    for path, target in moves.items():
        if path not in by_id[target]["paths"]:
            by_id[target]["paths"].append(path)
    for cluster in clusters:
        cluster["paths"] = sorted(set(cluster["paths"]))
        cluster["count"] = len(cluster["paths"])
        cluster["sample_paths"] = cluster["paths"][:12]


def _read_decisions(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
    return {str(item["cluster_id"]): item for item in decisions if isinstance(item, dict) and item.get("cluster_id")}


def _read_image_decisions(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
    return {str(item["key"]): item for item in decisions if isinstance(item, dict) and item.get("key")}


def _image_decision_key(path: str) -> str:
    return f"image:{hashlib.sha256(path.encode('utf-8')).hexdigest()[:24]}"


def _lexical_path(path: str) -> str:
    """Normalize a path without touching the filesystem.

    Review reads must remain responsive when a protected FUSE/NTFS source is
    offline.  ``Path.resolve()`` performs filesystem lookups and can block in
    the kernel; decision joins only need stable lexical path comparison.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _aggregate_identity_options(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge repeated records for one canonical without hiding alias collisions."""
    if not rows:
        return []
    grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        family = str(row.get("family") or "").strip()
        canonical = str(row.get("canonical") or "").strip()
        key = sorter.normalize_key(canonical)
        if key:
            grouped.setdefault((family, key), []).append(row)
    result: List[Dict[str, Any]] = []
    for members in grouped.values():
        # Keep catalog order for duplicate records of the same canonical, while
        # leaving distinct identities selectable even when aliases overlap.
        chosen = members[0]
        canonical = str(chosen.get("canonical") or "")
        aliases_out = {
            str(name)
            for item in members
            for name in [*(item.get("aliases") or []), item.get("canonical") or ""]
            if str(name) and str(name) != canonical
        }
        source_aliases: Dict[str, set[str]] = {}
        provenance: set[str] = set()
        for item in members:
            for source, names in (item.get("source_aliases") or {}).items():
                source_aliases.setdefault(str(source), set()).update(str(name) for name in names if str(name))
            provenance.update(str(value) for value in item.get("provenance", []) if str(value))
        result.append({
            "canonical": chosen.get("canonical"),
            "family": chosen.get("family"),
            "aliases": sorted(aliases_out, key=str.casefold),
            "source_aliases": {source: sorted(names, key=str.casefold) for source, names in sorted(source_aliases.items())},
            "provenance": sorted(provenance),
            "source": "canonical",
        })
    return sorted(result, key=lambda item: (str(item.get("family") or "").casefold(), str(item.get("canonical") or "").casefold()))


def _write_decisions(path: Path, decisions: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "decisions": list(decisions.values())}
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_image_decisions(path: Path, decisions: Dict[str, Dict[str, Any]]) -> None:
    _atomic_json_write(path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "decisions": list(decisions.values())})


def _read_pending_assignments(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    assignments = payload.get("assignments", {}) if isinstance(payload, dict) else {}
    if isinstance(assignments, list):
        return {str(item["key"]): item for item in assignments if isinstance(item, dict) and item.get("key")}
    return {str(key): value for key, value in assignments.items() if isinstance(value, dict)}


def _write_pending_assignments(path: Path, assignments: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        _atomic_json_write(
            path,
            {
                "schema_version": 1,
                "updated": datetime.now(timezone.utc).isoformat(),
                "assignments": assignments,
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _update_pending_assignments(
    path: Path,
    updater: Callable[[Dict[str, Dict[str, Any]]], None],
) -> Dict[str, Dict[str, Any]]:
    """Read/update/write the pending queue while holding an OS-level lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        assignments = _read_pending_assignments(path)
        updater(assignments)
        _atomic_json_write(
            path,
            {
                "schema_version": 1,
                "updated": datetime.now(timezone.utc).isoformat(),
                "assignments": assignments,
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return assignments


def _read_move_history(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    operations = payload.get("operations", []) if isinstance(payload, dict) else []
    return [item for item in operations if isinstance(item, dict) and item.get("id")]


def _write_move_history(path: Path, operations: List[Dict[str, Any]]) -> None:
    _atomic_json_write(path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "operations": operations[-500:]})


def _record_move_operation(path: Path, operations: List[Dict[str, Any]], move_result: Dict[str, Any], identity: str, family: str) -> Optional[str]:
    moved = [item for item in move_result.get("moved", []) if item.get("source") and item.get("destination")]
    if not moved:
        return None
    operation_id = uuid.uuid4().hex
    operations.append({"id": operation_id, "identity": identity, "family": family, "saved_at": datetime.now(timezone.utc).isoformat(), "moves": moved, "undone": False})
    _write_move_history(path, operations)
    return operation_id


def _record_move_audit(ledger_path: Path, move_id: Optional[str], move_result: Dict[str, Any], identity: str, family: str) -> None:
    """Make the durable move ID visible beside the review decision event."""
    if not move_id:
        return
    _append_review_ledger(
        ledger_path,
        {
            "event": "move_operation",
            "move_id": move_id,
            "identity": identity,
            "family": family,
            "moved": move_result.get("moved", []),
            "errors": move_result.get("errors", []),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _read_face_markers(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    markers = payload.get("markers", []) if isinstance(payload, dict) else []
    return {str(item["key"]): item for item in markers if isinstance(item, dict) and item.get("key")}


def _record_face_markers(
    path: Path,
    markers: Dict[str, Dict[str, Any]],
    paths: List[str],
    identity: str,
    family: str,
    status: str,
    *,
    persist: bool = True,
) -> None:
    saved_at = datetime.now(timezone.utc).isoformat()
    for raw_path in paths:
        marker_path = Path(raw_path)
        try:
            fingerprint = sorter.file_sha256(marker_path)
        except OSError:
            fingerprint = None
        key = _image_decision_key(raw_path)
        markers[key] = {"key": key, "path": raw_path, "identity": identity, "family": family, "status": status, "sha256": fingerprint, "source": "review_ui", "saved_at": saved_at}
    if persist:
        _atomic_json_write(path, {"schema_version": 1, "updated": saved_at, "markers": list(markers.values())})


def _relink_path_records(
    moves: List[Dict[str, str]], records: Dict[str, Dict[str, Any]], *, preserve_source_path: bool = False
) -> int:
    """Move decision/marker records along with successfully moved media.

    Review records are keyed by path because the audit and UI need to join
    them cheaply.  A confirmed assignment changes that path, so leaving the
    record at its source key would make the marker stale and exclude it from
    the next reference-gallery build.
    """
    relinked = 0
    for move in moves:
        source = str(move.get("source") or "")
        destination = str(move.get("destination") or "")
        if not source or not destination:
            continue
        source_lexical = _lexical_path(source)
        record_key = None
        record = None
        for key, candidate in list(records.items()):
            candidate_path = str(candidate.get("path") or "")
            canonical_path = str(candidate.get("canonical_path") or "")
            if (
                key == _image_decision_key(source)
                or (candidate_path and _lexical_path(candidate_path) == source_lexical)
                or (canonical_path and _lexical_path(canonical_path) == source_lexical)
            ):
                record_key = key
                record = candidate
                break
        if record is None:
            continue
        new_key = _image_decision_key(destination)
        if record_key != new_key:
            records.pop(record_key, None)
        record["key"] = new_key
        if preserve_source_path:
            # Keep the audit path in the image-decision payload for older UI
            # clients; canonical_path identifies where the media now lives.
            record["canonical_path"] = destination
        else:
            record["path"] = destination
        records[new_key] = record
        relinked += 1
    return relinked


def _valid_identity(canonical: str) -> bool:
    return bool(canonical and len(canonical) <= 120 and not any(char in canonical for char in "\\/\r\n\x00") and canonical not in {".", ".."})


def _read_manual_groups(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw_groups = payload.get("groups", []) if isinstance(payload, dict) else []
    groups: Dict[str, Dict[str, Any]] = {}
    for item in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        key = sorter.normalize_key(name)
        paths = item.get("paths") or []
        if _valid_identity(name) and key and isinstance(paths, list):
            groups[key] = {
                "name": name,
                "paths": sorted({str(value) for value in paths if isinstance(value, str) and value}),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
    return groups


def _read_manual_group_actions(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    actions = payload.get("actions", []) if isinstance(payload, dict) else []
    return [action for action in actions if isinstance(action, dict)] if isinstance(actions, list) else []


def _write_manual_group_actions(path: Path, actions: List[Dict[str, Any]]) -> None:
    updated_at = datetime.now(timezone.utc).isoformat()
    _atomic_json_write(path, {"schema_version": 1, "updated_at": updated_at, "actions": actions[-1000:]})


ASSORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _read_assorted_associations(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("associations", []) if isinstance(payload, dict) else []
    return {
        _lexical_path(str(item.get("folder"))): item
        for item in records
        if isinstance(item, dict) and item.get("folder") and item.get("identity")
    }


def _assorted_folder_is_candidate(name: str) -> bool:
    """Keep person-like leaf labels while hiding obvious archive containers."""
    if not name or name.startswith(("@", "#")):
        return False
    try:
        return not sorter.is_generic_identity_token(name)
    except (AttributeError, TypeError):
        return True


def _scan_assorted_folders(root: Path, associations: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a bounded, read-only inventory of person-like assorted folders."""
    if not root.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for current, directories, filenames in walker:
            directories[:] = [name for name in directories if not name.startswith((".",))]
            media_count = sum(
                1 for name in filenames if Path(name).suffix.lower() in ASSORTED_IMAGE_EXTENSIONS
            )
            if not media_count:
                continue
            folder = Path(current)
            if folder == root or not _assorted_folder_is_candidate(folder.name):
                continue
            lexical = _lexical_path(str(folder))
            record = {
                "folder": str(folder),
                "label": folder.name,
                "media_count": media_count,
                "associated": associations.get(lexical),
            }
            records.append(record)
            if len(records) >= 2000:
                break
    except OSError as exc:
        LOGGER.warning("assorted folder scan failed: %s", exc)
    return sorted(records, key=lambda item: (-int(item["media_count"]), str(item["folder"]).casefold()))


def _assorted_folder_path(raw_path: str, root: Path) -> Optional[Path]:
    """Validate an assorted folder lexically and reject symlink escapes."""
    requested = Path(_lexical_path(raw_path))
    root_lexical = Path(_lexical_path(str(root)))
    if requested == root_lexical or root_lexical not in requested.parents:
        return None
    try:
        if requested.is_symlink() or not requested.is_dir():
            return None
        resolved_root = root.resolve()
        resolved_requested = requested.resolve()
        if resolved_root not in resolved_requested.parents:
            return None
    except OSError:
        return None
    return requested


def move_review_paths(paths: List[str], identity: str, family: str, allowed_roots: set[Path], destination_root: Path) -> Dict[str, Any]:
    """Move explicitly reviewed files into the canonical identity folder."""
    target_dir = sorter.destination_for(sorter.Identity(identity, family, ()), destination_root)
    protected = {root.resolve() for root in sorter.PROTECTED_SOURCE_ROOTS}
    resolved_allowed_roots = set()
    for root in allowed_roots:
        try:
            resolved_allowed_roots.add(root.resolve())
        except OSError:
            continue
    moved: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for raw_path in paths:
        source = Path(raw_path).resolve()
        try:
            if not any(source == root or root in source.parents for root in resolved_allowed_roots):
                raise ValueError("source is outside the audit roots")
            if any(source == root or root in source.parents for root in protected):
                raise ValueError("protected source roots cannot be moved")
            if not source.is_file():
                raise FileNotFoundError("source file is missing or unreadable")
            target = sorter.resolve_target_path(target_dir, source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved.append({"source": str(source), "destination": str(target)})
        except (OSError, ValueError) as exc:
            errors.append({"source": str(source), "error": str(exc)})
    return {"moved": moved, "errors": errors}


def export_confirmed_decisions(decisions_path: Path, registry_path: Path = DEFAULT_REGISTRY) -> int:
    """Promote only confirmed UI decisions into the project registry."""
    decisions = _read_decisions(decisions_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = payload.setdefault("entries", [])
    existing = {(str(item.get("family")), sorter.normalize_key(str(item.get("canonical")))): item
                for item in entries if isinstance(item, dict)}
    promoted = 0
    for decision in decisions.values():
        if decision.get("status") != "confirmed":
            continue
        canonical = str(decision.get("identity") or "").strip()
        family = str(decision.get("family") or "review").strip()
        if not canonical or family not in (FAMILIES - {"review"}):
            continue
        aliases = [str(alias).strip() for alias in decision.get("aliases", []) if str(alias).strip()]
        key = (family, sorter.normalize_key(canonical))
        entry = existing.get(key)
        if entry is None:
            entry = {"family": family, "canonical": canonical, "aliases": [], "notes": ""}
            entries.append(entry)
            existing[key] = entry
        entry["aliases"] = sorted(set(entry.get("aliases", [])) | set(aliases))
        note = str(decision.get("notes") or "").strip()
        if note and note not in str(entry.get("notes") or ""):
            entry["notes"] = (str(entry.get("notes") or "").rstrip() + " [review-ui] " + note).strip()
        promoted += 1
    if promoted == 0:
        return 0
    fd, temp_name = tempfile.mkstemp(prefix=f".{registry_path.name}.", dir=str(registry_path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, registry_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return promoted


def add_project_registry_identity(
    canonical: str,
    family: str,
    aliases: Iterable[str] = (),
    notes: str = "",
    registry_path: Path = DEFAULT_REGISTRY,
) -> bool:
    """Persist a UI-created non-provisional identity to the local overlay."""
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("project identity registry must be a JSON object")
    entries = payload.setdefault("entries", [])
    if not isinstance(entries, list):
        raise ValueError("project identity registry entries must be a list")
    canonical_key = sorter.normalize_key(canonical)
    alias_values = {str(alias).strip() for alias in aliases if str(alias).strip()} | {canonical}
    entry = next((
        item for item in entries
        if isinstance(item, dict)
        and str(item.get("family") or family) == family
        and sorter.normalize_key(str(item.get("canonical") or "")) == canonical_key
    ), None)
    for item in entries:
        if not isinstance(item, dict) or item is entry:
            continue
        target = str(item.get("canonical") or "").strip()
        names = {target, *(str(value).strip() for value in item.get("aliases", []) if str(value).strip())}
        if any(sorter.normalize_key(name) == canonical_key for name in names):
            raise ValueError(f"identity {canonical!r} conflicts with registry identity {target!r}")
        for alias in alias_values:
            alias_key = sorter.normalize_key(alias)
            if alias_key and any(sorter.normalize_key(name) == alias_key for name in names):
                raise ValueError(f"alias {alias!r} already belongs to registry identity {target!r}")
    created = entry is None
    if entry is None:
        entry = {"family": family, "canonical": canonical, "aliases": [], "notes": ""}
        entries.append(entry)
    entry["aliases"] = sorted({str(value).strip() for value in entry.get("aliases", []) if str(value).strip()} | alias_values)
    note = str(notes or "").strip()
    if note and note not in str(entry.get("notes") or ""):
        entry["notes"] = (str(entry.get("notes") or "").rstrip() + " [review-ui] " + note).strip()
    _atomic_json_write(registry_path, payload)
    return created


def _public_cluster(cluster: Dict[str, Any], decision: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = {key: value for key, value in cluster.items() if key not in {"paths", "face_link_scores"}}
    result["decision"] = decision
    result["purity_flags"] = _cluster_purity_flags(cluster)
    result["requires_image_review"] = bool(result["purity_flags"])
    return result


def _normalize_identity_picker_prefixes(values: Any) -> List[str]:
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError("prefixes must be a list with at most 50 values")
    prefixes: List[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("each picker prefix must be text")
        value = raw.strip().casefold()
        if not value:
            continue
        if len(value) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
            raise ValueError("prefixes may contain only letters, numbers, underscores, and hyphens")
        if value not in prefixes:
            prefixes.append(value)
    if not prefixes:
        raise ValueError("add at least one identity prefix")
    return prefixes


def _read_identity_picker_prefixes(path: Path) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_identity_picker_prefixes(payload.get("prefixes"))
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        return list(DEFAULT_IDENTITY_PICKER_PREFIXES)


def _read_recent_choices(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    identities: List[Dict[str, str]] = []
    seen_identities: set[str] = set()
    raw_identities = payload.get("identities", [])
    for item in raw_identities if isinstance(raw_identities, list) else []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical") or "").strip()[:256]
        key = canonical.casefold()
        if not key or key in seen_identities:
            continue
        seen_identities.add(key)
        family = str(item.get("family") or "review")
        identities.append({"canonical": canonical, "family": family if family in FAMILIES else "review"})
        if len(identities) == 20:
            break
    groups: List[str] = []
    seen_groups: set[str] = set()
    raw_groups = payload.get("groups", [])
    for value in raw_groups if isinstance(raw_groups, list) else []:
        name = str(value or "").strip()[:128]
        key = name.casefold()
        if not key or key in seen_groups:
            continue
        seen_groups.add(key)
        groups.append(name)
        if len(groups) == 20:
            break
    return {"identities": identities, "groups": groups}


def _identity_reconciliation_snapshot(registry_path: Path, decisions_path: Path) -> Dict[str, Any]:
    """Read unmatched local manual names and confirmed shared identities, without editing either registry."""
    try:
        local_registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        local_registry = {}
    try:
        shared_registry = json.loads(sorter.METADAILY_IDENTITY_ALIASES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        shared_registry = {}
    try:
        saved = json.loads(decisions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    saved_decisions = saved.get("decisions", {}) if isinstance(saved, dict) else {}
    if not isinstance(saved_decisions, dict):
        saved_decisions = {}

    def strings(value: Any) -> List[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        if isinstance(value, dict):
            return [text for item in value.values() for text in strings(item)]
        return []

    shared_rows: List[Dict[str, Any]] = []
    shared_keys: set[str] = set()
    for item in shared_registry.get("identities", []) if isinstance(shared_registry, dict) else []:
        if not isinstance(item, dict) or str(item.get("status") or "").casefold() != "confirmed":
            continue
        canonical = str(item.get("id") or "").strip()
        if not canonical:
            continue
        aliases = set(strings(item.get("display_names")))
        aliases.update(strings(item.get("search_terms")))
        aliases.update(strings(item.get("source_only_terms")))
        aliases.update(strings(item.get("hashtags")))
        aliases.update(strings(item.get("reddit")))
        aliases.update(strings(item.get("sources")))
        aliases.update(strings(item.get("metadaily_collection_sources")))
        for value in (item.get("primary_folder"), item.get("metadaily_user_group")):
            aliases.update(strings(value))
        all_names = {canonical, *aliases}
        shared_keys.update(sorter.normalize_key(value) for value in all_names if sorter.normalize_key(value))
        shared_rows.append({
            "id": canonical,
            "primary_folder": str(item.get("primary_folder") or ""),
            "display_names": sorted(set(strings(item.get("display_names"))), key=str.casefold),
            "aliases": sorted(aliases, key=str.casefold),
            "sources": sorted(set(strings(item.get("sources"))), key=str.casefold),
        })
    manuals: List[Dict[str, Any]] = []
    for item in local_registry.get("entries", []) if isinstance(local_registry, dict) else []:
        if not isinstance(item, dict) or str(item.get("family") or "").casefold() != "manual":
            continue
        canonical = str(item.get("canonical") or "").strip()
        aliases = sorted(set(strings(item.get("aliases"))), key=str.casefold)
        if not canonical:
            continue
        if any(sorter.normalize_key(value) in shared_keys for value in [canonical, *aliases]):
            continue
        manuals.append({
            "canonical": canonical,
            "aliases": aliases,
            "notes": str(item.get("notes") or ""),
            "decision": saved_decisions.get(canonical),
        })
    manuals.sort(key=lambda item: item["canonical"].casefold())
    shared_rows.sort(key=lambda item: item["id"].casefold())
    return {"manual_entries": manuals, "shared_identities": shared_rows, "decisions": saved_decisions}


def create_app(
    audit_path: Path,
    decisions_path: Path = DEFAULT_DECISIONS,
    overrides_path: Path = DEFAULT_OVERRIDES,
    image_decisions_path: Path = DEFAULT_IMAGE_DECISIONS,
    review_identities_path: Path = DEFAULT_REVIEW_IDENTITIES,
    ui_token: str | None = None,
    preflight_path: Path | None = None,
    face_markers_path: Path = DEFAULT_FACE_MARKERS,
    move_history_path: Path = DEFAULT_MOVE_HISTORY,
    ledger_path: Path | None = None,
    allow_unauthenticated_writes: bool | None = None,
    face_rebuild_lock_path: Path | None = None,
    pipeline_lock_path: Path | None = None,
    repair_ledger_path: Path | None = None,
    rebuild_log_path: Path | None = None,
    pending_assignments_path: Path | None = None,
    evidence_db_path: Path | None = None,
    evidence_health_path: Path | None = None,
    scheduler_config_path: Path | None = None,
    scheduler_status_path: Path | None = None,
    assorted_root: Path | None = None,
    assorted_associations_path: Path | None = None,
    registry_path: Path = DEFAULT_REGISTRY,
    manual_groups_path: Path = DEFAULT_MANUAL_GROUPS,
    manual_group_actions_path: Path | None = None,
    identity_picker_settings_path: Path | None = None,
    recent_choices_path: Path | None = None,
    identity_reconciliation_decisions_path: Path | None = None,
) -> Flask:
    face_rebuild_lock_path = face_rebuild_lock_path or Path(os.environ.get("PICORG_FACE_REBUILD_LOCK", str(DEFAULT_FACE_REBUILD_LOCK)))
    if pipeline_lock_path is None:
        raw_pipeline_lock = os.environ.get("PICORG_UI_PIPELINE_LOCK")
        pipeline_lock_path = Path(raw_pipeline_lock) if raw_pipeline_lock else None
    repair_ledger_path = repair_ledger_path or Path(os.environ.get("PICORG_REPAIR_LEDGER", str(DEFAULT_REPAIR_LEDGER)))
    rebuild_log_path = rebuild_log_path or Path(os.environ.get("PICORG_FACE_RECOVERY_LOG", str(DEFAULT_REBUILD_LOG)))
    pending_assignments_path = pending_assignments_path or Path(os.environ.get("PICORG_PENDING_ASSIGNMENTS", str(DEFAULT_PENDING_ASSIGNMENTS)))
    evidence_db_path = evidence_db_path or Path(os.environ.get("PICORG_EVIDENCE_DB", str(DEFAULT_EVIDENCE_DB)))
    evidence_health_path = evidence_health_path or Path(os.environ.get("PICORG_EVIDENCE_HEALTH", str(DEFAULT_EVIDENCE_HEALTH)))
    scheduler_config_path = scheduler_config_path or Path(os.environ.get("PICORG_SCHEDULER_CONFIG", str(DEFAULT_SCHEDULER_CONFIG)))
    scheduler_status_path = scheduler_status_path or Path(os.environ.get("PICORG_SCHEDULER_STATUS", str(DEFAULT_SCHEDULER_STATUS)))
    identity_picker_settings_path = identity_picker_settings_path or DEFAULT_IDENTITY_PICKER_SETTINGS
    recent_choices_path = recent_choices_path or DEFAULT_RECENT_CHOICES
    recent_choices_lock = threading.Lock()
    identity_reconciliation_decisions_path = identity_reconciliation_decisions_path or DEFAULT_IDENTITY_RECONCILIATION_DECISIONS
    identity_reconciliation_lock = threading.Lock()
    assorted_root = assorted_root or DEFAULT_ASSORTED_ROOT
    assorted_associations_path = assorted_associations_path or DEFAULT_ASSORTED_ASSOCIATIONS
    ledger_path = ledger_path or decisions_path.with_name("review_decision_ledger.jsonl")
    payload = load_audit(audit_path)
    current_pointer_path = Path(os.environ.get("PICORG_CURRENT_RUN_POINTER", str(DEFAULT_CURRENT_RUN_POINTER)))
    current_audit = resolve_current_run(current_pointer_path)
    current_run_paths: set[Path] = set()
    current_run_published_at = None
    try:
        pointer_payload = json.loads(current_pointer_path.read_text(encoding="utf-8"))
        current_run_published_at = pointer_payload.get("published_at") if isinstance(pointer_payload, dict) else None
        for pointer_key in ("audit", "reconciled_audit", "face_audit", "identity_matches"):
            pointer_item = pointer_payload.get(pointer_key) if isinstance(pointer_payload, dict) else None
            pointer_path = pointer_item.get("path") if isinstance(pointer_item, dict) else pointer_item
            if pointer_path:
                current_run_paths.add(Path(str(pointer_path)).resolve())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        current_run_paths = set()
    current_run_status = {
        "pointer": str(current_pointer_path),
        # A review UI normally opens the reconciled audit, while the pointer's
        # primary ``audit`` field remains the name-match input.  Treat any
        # artifact published by that same run as current.
        "is_current": bool(
            current_audit
            and (
                audit_path.resolve(strict=False) == current_audit.resolve(strict=False)
                or audit_path.resolve(strict=False) in current_run_paths
            )
        ),
        "audit": str(current_audit) if current_audit else None,
        "published_at": current_run_published_at,
    }
    clusters = load_cluster_index(audit_path)
    if preflight_path is None:
        stem = audit_path.name.split(".reconciled", 1)[0]
        preflight_path = audit_path.with_name(f"{stem}.preflight.json")
    preflight_records = load_preflight_records(preflight_path)
    hidden_preflight = filter_clusters_by_preflight(
        clusters, {str(item["path"]): str(item["status"]) for item in preflight_records}
    )
    purity_path = audit_path.with_name(f"{audit_path.stem}.cluster-purity.json")
    try:
        purity_payload = json.loads(purity_path.read_text(encoding="utf-8")) if purity_path.is_file() else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        purity_payload = None
    attention_queue = build_attention_queue(payload, preflight_records, purity_payload)
    known_paths = {path for cluster in clusters for path in cluster.get("paths", [])}
    _apply_overrides(clusters, _read_overrides(overrides_path))
    by_id = {cluster["cluster_id"]: cluster for cluster in clusters}
    decisions = _read_decisions(decisions_path)
    image_decisions = _read_image_decisions(image_decisions_path)
    pending_assignments = _read_pending_assignments(pending_assignments_path)
    face_markers = _read_face_markers(face_markers_path)
    move_history = _read_move_history(move_history_path)
    deleted_media_path_keys = _read_deleted_media_path_keys(ledger_path)
    applied_assignments_by_path: dict[str, Dict[str, Any]] = {}
    queued_assignments_by_path: dict[str, Dict[str, Any]] = {}
    review_identities = _read_decisions(review_identities_path)
    manual_groups = _read_manual_groups(manual_groups_path)
    if manual_group_actions_path is None:
        manual_group_actions_path = (
            DEFAULT_MANUAL_GROUP_ACTIONS
            if manual_groups_path == DEFAULT_MANUAL_GROUPS
            else manual_groups_path.with_name("manual_group_actions.json")
        )
    assorted_associations = _read_assorted_associations(assorted_associations_path)
    raw_allowed_roots = {str(path) for cluster in clusters for path in cluster["source_roots"] if path}
    rebuild_status_cache: dict[str, Any] = {}

    def refresh_pending_assignments() -> None:
        pending_assignments.clear()
        pending_assignments.update(_read_pending_assignments(pending_assignments_path))

    def refresh_applied_assignments() -> None:
        try:
            applied = list_durable_assignment_queue(evidence_db_path, statuses=("applied",))
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.warning("unable to refresh applied assignment paths: %s", exc)
            return
        applied_assignments_by_path.clear()
        applied_assignments_by_path.update({
            _image_decision_key(str(item["path"])): item
            for item in applied
            if item.get("path")
        })

    def refresh_queued_assignments() -> None:
        try:
            queued = list_durable_assignment_queue(
                evidence_db_path,
                statuses=("pending", "applying", "error", "conflict", "rejected"),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.warning("unable to refresh queued assignment paths: %s", exc)
            return
        queued_assignments_by_path.clear()
        queued_assignments_by_path.update({
            _image_decision_key(str(item["path"])): item
            for item in queued
            if item.get("path")
        })

    def is_deleted_media_path(path: str) -> bool:
        key = _image_decision_key(path)
        if key not in deleted_media_path_keys:
            return False
        try:
            media_path = Path(_lexical_path(path))
            return media_path.is_symlink() or not media_path.is_file()
        except OSError:
            return True

    completed_move_sources: set[str] = set()

    def refresh_move_history() -> None:
        nonlocal completed_move_sources
        move_history[:] = _read_move_history(move_history_path)
        completed_move_sources = {
            _image_decision_key(str(move.get("source")))
            for operation in move_history
            if not operation.get("undone")
            for move in operation.get("moves", [])
            if isinstance(move, dict) and move.get("source") and move.get("destination")
        }

    def live_media_paths(paths: List[str], *, check_all: bool = False) -> List[str]:
        """Return audit paths that still resolve to regular media files."""
        live = []
        for path in paths:
            if is_deleted_media_path(path):
                continue
            key = _image_decision_key(path)
            if not check_all and key not in completed_move_sources:
                live.append(path)
                continue
            try:
                exists = Path(_lexical_path(path)).is_file()
            except OSError:
                exists = False
            if exists:
                live.append(path)
            elif key in completed_move_sources:
                continue
        return live

    def refresh_face_markers() -> None:
        """Reload durable confirmations written by the reconciler or another UI."""
        face_markers.clear()
        face_markers.update(_read_face_markers(face_markers_path))

    # Index durable image decisions once at startup.  The cluster list endpoint
    # checks confirmed images across the whole audit; scanning every decision
    # and move-history record for every path made that request appear hung on
    # large audits.  Keep the compatibility fallback below for older ledgers,
    # but make the common path O(1).
    decision_by_path: dict[str, Dict[str, Any]] = {}
    for candidate in image_decisions.values():
        for candidate_path in (candidate.get("path"), candidate.get("canonical_path")):
            if candidate_path:
                decision_by_path.setdefault(_image_decision_key(str(candidate_path)), candidate)
    for operation in move_history:
        if operation.get("undone"):
            continue
        for item in operation.get("moves", []):
            if item.get("unassigned"):
                continue
            source = str(item.get("source") or "")
            destination = str(item.get("destination") or "")
            if source and destination:
                candidate = decision_by_path.get(_image_decision_key(destination))
                if candidate is not None:
                    decision_by_path.setdefault(_image_decision_key(source), candidate)

    def decision_for_path(path: str) -> Optional[Dict[str, Any]]:
        applied = applied_assignments_by_path.get(_image_decision_key(path))
        if applied is not None:
            return {
                "path": path,
                "identity": applied.get("identity", ""),
                "family": "review",
                "status": "confirmed",
                "assignment_id": applied.get("assignment_id"),
            }
        durable = queued_assignments_by_path.get(_image_decision_key(path))
        if durable is not None:
            provenance = durable.get("provenance") or {}
            queue_status = str(durable.get("status") or "pending")
            return {
                "path": path,
                "identity": durable.get("identity", ""),
                "family": provenance.get("family") or "review",
                "status": "queued" if queue_status in {"pending", "applying"} else queue_status,
                "queued_status": queue_status,
                "assignment_id": durable.get("assignment_id"),
                "error": durable.get("error"),
            }
        queued = pending_assignments.get(_image_decision_key(path))
        if queued is not None:
            return {**queued, "status": "queued", "queued_status": queued.get("status", "confirmed")}
        direct = image_decisions.get(_image_decision_key(path))
        if direct is not None:
            return direct
        indexed = decision_by_path.get(_image_decision_key(path))
        if indexed is not None:
            return indexed
        marker = face_markers.get(_image_decision_key(path))
        if marker is not None:
            return marker
        requested = _lexical_path(path)
        # Image decisions retain the audit path for compatibility and carry a
        # canonical_path after a move.  Prefer this durable record join before
        # consulting bounded move history.
        # Requests can update this shared mapping while another request resolves
        # historical/canonical paths. Iterate a stable snapshot to avoid a
        # RuntimeError when an assignment is saved concurrently.
        for candidate in tuple(image_decisions.values()):
            for candidate_path in (candidate.get("path"), candidate.get("canonical_path")):
                if candidate_path and _lexical_path(str(candidate_path)) == requested:
                    return candidate
        for operation in reversed(move_history):
            if operation.get("undone"):
                continue
            for item in reversed(operation.get("moves", [])):
                if item.get("unassigned"):
                    continue
                if item.get("source") and _lexical_path(str(item.get("source"))) == requested:
                    return image_decisions.get(_image_decision_key(str(item.get("destination") or "")))
        return None

    def confirmed_paths(paths: Iterable[str]) -> set[str]:
        return {
            path for path in paths
            if _image_decision_key(path) in applied_assignments_by_path
            or (image_decisions.get(_image_decision_key(path)) or decision_by_path.get(_image_decision_key(path)) or face_markers.get(_image_decision_key(path)) or {}).get("status") == "confirmed"
        }
    # Keep startup independent of slow/offline storage; resolve only when a
    # media request or confirmed move actually needs the containment check.
    allowed_roots = {Path(path) for path in raw_allowed_roots}
    allowed_roots.add(DEFAULT_REVIEW_DEST_ROOT.resolve())
    review_dest_root = DEFAULT_REVIEW_DEST_ROOT.resolve()
    # Media requests should not resolve every path through a slow/offline
    # FUSE mount.  The audit is already the allow-list for review media; keep
    # a lexical index for fast containment checks and reserve realpath checks
    # for actual move operations below.
    known_paths_lexical = {_lexical_path(path) for path in known_paths}
    review_dest_root_lexical = _lexical_path(str(DEFAULT_REVIEW_DEST_ROOT))

    def is_known_media_path(raw_path: str) -> bool:
        requested = _lexical_path(raw_path)
        if requested in known_paths_lexical:
            return True
        return requested == review_dest_root_lexical or requested.startswith(review_dest_root_lexical + os.sep)

    def is_under_allowed_root(path: Path) -> bool:
        resolved_roots = set()
        for root in allowed_roots:
            try:
                resolved_roots.add(root.resolve())
            except OSError:
                continue
        return any(path == root or root in path.parents for root in resolved_roots)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    configured_token = ui_token or os.environ.get("PICORG_UI_TOKEN", "")
    if allow_unauthenticated_writes is None:
        allow_unauthenticated_writes = os.environ.get("PICORG_UI_ALLOW_UNAUTH_WRITES", "0") == "1"
    trusted_networks = parse_trusted_networks(os.environ.get("PICORG_UI_TRUSTED_CIDRS", DEFAULT_TRUSTED_CIDRS))
    force_authentication = os.environ.get("PICORG_UI_AUTH", "0") == "1"
    app.config["PICORG_FACE_REBUILD_LOCK"] = str(face_rebuild_lock_path)
    app.config["PICORG_PIPELINE_LOCK"] = str(pipeline_lock_path) if pipeline_lock_path else None
    write_lock = threading.RLock()
    benchmark_lock = threading.Lock()
    benchmark_state: Dict[str, Any] = {
        "running": False,
        "exit_code": None,
        "lines": [],
        "started_at": None,
        "finished_at": None,
    }

    def refresh_review_identities() -> Dict[str, Dict[str, Any]]:
        """Pick up identities added by another PicOrg/UI process without restart."""
        latest = _read_decisions(review_identities_path)
        if latest != review_identities:
            review_identities.clear()
            review_identities.update(latest)
        return review_identities

    def catalog_rows() -> List[Dict[str, Any]]:
        catalog, _, _, _, preferred_targets = sorter.load_identity_catalog()
        # Keep the preferred registry target visible when an older source also
        # exposes the alias as a standalone canonical (for example `stoya`).
        suppressed = {
            sorter.normalize_key(alias)
            for alias, target in preferred_targets.items()
            if sorter.normalize_key(alias) != sorter.normalize_key(target)
        }
        return [
            {"canonical": item.canonical, "family": item.family, "aliases": list(item.aliases),
             "source_aliases": {source: list(values) for source, values in getattr(item, "source_aliases", ())},
             "provenance": list(getattr(item, "provenance", ()))}
            for item in catalog
            if sorter.normalize_key(item.canonical) not in suppressed
        ]

    def identity_rows_with_local_decisions() -> List[Dict[str, Any]]:
        """Add only local decisions that do not already resolve to a catalog identity."""
        rows = catalog_rows()
        alias_targets: Dict[str, set[str]] = {}
        canonical_keys = set()
        for row in rows:
            canonical = str(row.get("canonical") or "").strip()
            canonical_key = sorter.normalize_key(canonical)
            if canonical_key:
                canonical_keys.add(canonical_key)
            for name in [canonical, *(row.get("aliases") or [])]:
                alias_key = sorter.normalize_key(str(name))
                if alias_key and canonical_key:
                    alias_targets.setdefault(alias_key, set()).add(canonical_key)
        for item in refresh_review_identities().values():
            canonical = str(item.get("identity") or "").strip()
            canonical_key = sorter.normalize_key(canonical)
            if not canonical_key or canonical_key in canonical_keys:
                continue
            names = [canonical, str(item.get("cluster_id") or ""), *(item.get("aliases") or [])]
            matches: set[str] = set()
            for name in names:
                matches.update(alias_targets.get(sorter.normalize_key(str(name)), set()))
            if len(matches) == 1:
                continue
            rows.append({"canonical": canonical, "family": item.get("family", "review"),
                         "aliases": list(item.get("aliases") or []), "source": "review"})
            canonical_keys.add(canonical_key)
        return rows

    def run_accuracy_benchmark() -> None:
        started = datetime.now(timezone.utc).isoformat()
        with benchmark_lock:
            benchmark_state.update({"running": True, "exit_code": None, "lines": [], "started_at": started, "finished_at": None})
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                ["bash", str(Path(__file__).with_name("run_accuracy_benchmark.sh"))],
                cwd=str(Path(__file__).resolve().parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if process.stdout is not None:
                for line in process.stdout:
                    with benchmark_lock:
                        benchmark_state["lines"] = (benchmark_state["lines"] + [line.rstrip()])[-200:]
            code = process.wait()
        except Exception as exc:  # Keep the review UI usable if a tool is unavailable.
            code = 1
            with benchmark_lock:
                benchmark_state["lines"] = (benchmark_state["lines"] + [f"ERROR: {exc}"])[-200:]
        with benchmark_lock:
            benchmark_state.update({"running": False, "exit_code": code, "finished_at": datetime.now(timezone.utc).isoformat()})

    @app.before_request
    def request_context() -> None:
        g.request_id = request.headers.get("X-Request-ID", "")[:80] or uuid.uuid4().hex
        g.face_rebuild_active = face_rebuild_is_active(face_rebuild_lock_path)
        g.pipeline_active = pipeline_lock_is_active(pipeline_lock_path)
        g.read_only = g.face_rebuild_active or g.pipeline_active
        if request.path in {"/health", "/healthz", "/readyz"}:
            return
        # A pending image assignment is an intentional report-only write.  It
        # records operator intent without touching media, markers, or the
        # active decision ledger, so it remains available during a rebuild.
        pending_assignment_write = request.path == "/api/image-decisions/pending" and request.method == "POST"
        assorted_association_write = request.path == "/api/assorted-folder-associations" and request.method == "POST"
        if g.read_only and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not (pending_assignment_write or assorted_association_write):
            return jsonify({
                "error": "review writes are disabled while a pipeline job is running",
                "read_only": True,
                "request_id": g.request_id,
            }), 423
        client_ip = None
        try:
            client_ip = ipaddress.ip_address(request.remote_addr or "")
            if isinstance(client_ip, ipaddress.IPv6Address) and client_ip.ipv4_mapped:
                client_ip = client_ip.ipv4_mapped
        except ValueError:
            pass
        lan_client = allow_unauthenticated_writes or (client_ip is not None and any(client_ip in network for network in trusted_networks))
        # State-changing API writes alter the decision ledger, review state, or
        # files. LAN clients are intentionally unrestricted; non-LAN clients
        # must authenticate. PICORG_UI_AUTH=1 forces authentication everywhere.
        token_required = force_authentication or not lan_client
        if token_required and not configured_token:
            from flask import abort

            abort(401, description="remote access requires PICORG_UI_TOKEN")
        if token_required and configured_token:
            supplied = request.headers.get("X-Picorg-Token", "") or request.headers.get("Authorization", "")
            if supplied.startswith("Bearer "):
                supplied = supplied[7:]
            if not hmac.compare_digest(supplied, configured_token):
                from flask import abort

                abort(401, description="valid X-Picorg-Token or Bearer token required")

    @app.after_request
    def response_headers(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
        if request.path == "/" or request.path.startswith("/api/") or request.path == "/media":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "request body is too large", "request_id": g.request_id}), 413

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description, "request_id": g.request_id}), error.code
        return error

    @app.errorhandler(Exception)
    def handle_unexpected(error):
        LOGGER.exception("request failed request_id=%s path=%s", g.request_id, request.path, exc_info=error)
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error", "request_id": g.request_id}), 500
        return "The review UI encountered an internal error.", 500

    @app.get("/")
    def index():
        return HTML_PAGE

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/health")
    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "request_id": g.request_id})

    @app.get("/readyz")
    def readyz():
        ready = bool(clusters) and audit_path.is_file()
        return jsonify({"status": "ready" if ready else "not_ready", "clusters": len(clusters), "current_run": current_run_status, "request_id": g.request_id}), 200 if ready else 503

    @app.get("/api/summary")
    def summary():
        refresh_pending_assignments()
        report = payload.get("report") or {}
        compact_report = {key: value for key, value in report.items() if key not in {"gallery_sets", "results"}}
        read_only = face_rebuild_is_active(face_rebuild_lock_path) or pipeline_lock_is_active(pipeline_lock_path)
        reason = "face rebuild running" if face_rebuild_is_active(face_rebuild_lock_path) else ("pipeline job running" if read_only else None)
        pipeline_status = scheduler.read_status(scheduler_status_path)
        return jsonify({"audit": str(audit_path), "report": compact_report, "clusters": len(clusters), "hidden_preflight": hidden_preflight, "decisions": len(decisions), "pending_assignments": len(pending_assignments), "attention": {"total": attention_queue["total"], "counts": attention_queue["counts"]}, "cache": str(audit_path.with_suffix(".clusters.json")), "read_only": read_only, "read_only_reason": reason, "current_run": current_run_status, "freshness": {"last_intake_at": pipeline_status.get("last_intake_at"), "last_map_refresh_at": pipeline_status.get("last_map_refresh_at") or current_run_published_at}})

    @app.get("/api/pending-assignments")
    def pending_assignment_status():
        refresh_pending_assignments()
        try:
            durable = list_durable_assignment_queue(evidence_db_path, statuses=("pending", "applying", "error", "conflict"))
        except (OSError, RuntimeError):
            LOGGER.exception("unable to read durable assignment queue")
            durable = []
        return jsonify({
            "schema_version": 1,
            "count": len(pending_assignments),
            "assignments": list(pending_assignments.values()),
            "durable_count": len(durable),
            "durable_assignments": durable,
            "evidence_db": str(evidence_db_path),
            "path": str(pending_assignments_path),
            "read_only": face_rebuild_is_active(face_rebuild_lock_path) or pipeline_lock_is_active(pipeline_lock_path),
        })

    @app.get("/api/assignment-queue")
    def assignment_queue_status():
        """Expose the SQLite assignment queue; no media or markers are changed."""
        raw_statuses = request.args.get("status", "")
        statuses = tuple(value.strip() for value in raw_statuses.split(",") if value.strip()) or None
        try:
            assignments = list_durable_assignment_queue(evidence_db_path, statuses=statuses)
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.exception("unable to read assignment queue")
            return jsonify({"error": f"assignment queue unavailable: {exc}"}), 503
        for item in assignments:
            provenance = item.get("provenance")
            if item.get("status") == "applied" and isinstance(provenance, dict) and provenance.get("applied_move"):
                item["move_id"] = f"assignment-{item['assignment_id']}"
        return jsonify({"schema_version": 1, "count": len(assignments), "assignments": assignments, "evidence_db": str(evidence_db_path)})

    @app.post("/api/assignment-queue/<int:assignment_id>/reject")
    def reject_assignment_queue_item(assignment_id: int):
        """Reject one queued assignment without touching its source file."""
        try:
            update_durable_assignment_status(evidence_db_path, assignment_id, "rejected", "rejected from review UI")
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": f"unable to reject assignment: {exc}"}), 400
        return jsonify({"assignment_id": assignment_id, "status": "rejected"}), 200

    @app.get("/api/runtime")
    def runtime_status():
        face_active = face_rebuild_is_active(face_rebuild_lock_path)
        pipeline_active = pipeline_lock_is_active(pipeline_lock_path)
        read_only = face_active or pipeline_active
        return jsonify({"read_only": read_only, "pipeline_active": pipeline_active, "face_rebuild_active": face_active, "read_only_reason": "face rebuild running" if face_active else ("pipeline job running" if pipeline_active else None)})

    @app.get("/api/rebuild-status")
    def rebuild_status():
        """Expose bounded rebuild telemetry for the UI without returning raw logs."""
        face_active = face_rebuild_is_active(face_rebuild_lock_path)
        pipeline_active = pipeline_lock_is_active(pipeline_lock_path)
        read_only = face_active or pipeline_active
        process_data = _rebuild_process_snapshot()
        process = process_data["process"]
        ledger = _repair_ledger_summary(repair_ledger_path, rebuild_status_cache)
        progress = _rebuild_progress_snapshot(rebuild_log_path, running=face_active)
        return jsonify({
            "running": face_active,
            "read_only": read_only,
            "pipeline_active": pipeline_active,
            "stage": process.get("stage") if process else ("rebuild in progress" if read_only else None),
            "pid": process.get("pid") if process else None,
            "elapsed_seconds": process.get("elapsed_seconds") if process else None,
            "process_count": process_data["process_count"],
            "progress": progress,
            "repair_ledger": ledger,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    @app.get("/api/scheduler/status")
    def scheduler_status():
        snapshot = _scheduler_snapshot(scheduler_config_path, scheduler_status_path)
        try:
            snapshot["evidence_health"] = json.loads(evidence_health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snapshot["evidence_health"] = {"available": False, "path": str(evidence_health_path)}
        return jsonify(snapshot)

    @app.get("/api/evidence-health")
    def evidence_health():
        try:
            report = json.loads(evidence_health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"available": False, "path": str(evidence_health_path)}), 200
        if not isinstance(report, dict):
            return jsonify({"available": False, "path": str(evidence_health_path)}), 200
        report["available"] = True
        return jsonify(report)

    @app.post("/api/scheduler/config")
    def save_scheduler_config():
        body = request.get_json(silent=True) or {}
        current = scheduler.read_config(scheduler_config_path)
        merged = {**current, **{key: body[key] for key in scheduler.DEFAULT_CONFIG if key in body}}
        config = scheduler.normalize_config(merged)
        _atomic_json_write(scheduler_config_path, config)
        return jsonify({"config": config}), 200

    @app.post("/api/scheduler/run")
    def run_scheduler_job():
        body = request.get_json(silent=True) or {}
        command = str(body.get("command") or "run").strip()
        job = str(body.get("job") or "").strip() or None
        if command not in {"run", "cycle", "daemon"}:
            return jsonify({"error": "command must be run, cycle, or daemon"}), 400
        try:
            launched = _launch_scheduler_process(command, job, scheduler_config_path, scheduler_status_path)
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({**launched, "status": _scheduler_snapshot(scheduler_config_path, scheduler_status_path)}), 202

    @app.post("/api/scheduler/stop")
    def stop_scheduler_job():
        snapshot = _scheduler_snapshot(scheduler_config_path, scheduler_status_path)
        status = snapshot["status"]
        pid = status.get("pid") if snapshot["running"] else status.get("daemon_pid") if snapshot["daemon_running"] else None
        if not pid:
            return jsonify({"stopped": False, "reason": "no active scheduler process"}), 200
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"unable to stop scheduler: {exc}"}), 409
        return jsonify({"stopped": True, "pid": int(pid)}), 202

    @app.get("/api/attention")
    def attention():
        category = str(request.args.get("category") or "").strip().casefold()
        query = str(request.args.get("q") or "").strip().casefold()
        items = [
            item for item in attention_queue["items"]
            if (not category or item["category"].casefold() == category)
            and (not query or query in str(item.get("path") or "").casefold() or query in item["category"].casefold() or query in item["reason"].casefold())
        ]
        try:
            page = max(int(request.args.get("page", 1)), 1)
            page_size = min(max(int(request.args.get("page_size", 100)), 1), 500)
        except ValueError:
            page, page_size = 1, 100
        start = (page - 1) * page_size
        return jsonify({
            "items": items[start:start + page_size],
            "counts": attention_queue["counts"],
            "total": attention_queue["total"],
            "filtered": len(items),
            "page": page,
            "page_size": page_size,
            "has_next": start + page_size < len(items),
            "truncated": attention_queue["truncated"],
        })

    @app.get("/api/accuracy-benchmark")
    def accuracy_benchmark_status():
        with benchmark_lock:
            return jsonify(dict(benchmark_state))

    @app.post("/api/accuracy-benchmark")
    def accuracy_benchmark_start():
        with benchmark_lock:
            if benchmark_state["running"]:
                return jsonify({"error": "accuracy benchmark is already running", **benchmark_state}), 409
            worker = threading.Thread(target=run_accuracy_benchmark, name="picorg-accuracy-benchmark", daemon=True)
            worker.start()
            return jsonify({"started": True, **benchmark_state}), 202

    @app.get("/api/clusters")
    def list_clusters():
        refresh_pending_assignments()
        refresh_applied_assignments()
        refresh_queued_assignments()
        refresh_face_markers()
        try:
            page = max(int(request.args.get("page", 1)), 1)
            page_size = min(max(int(request.args.get("page_size", request.args.get("limit", 50))), 1), 200)
        except ValueError:
            page, page_size = 1, 50
        query = str(request.args.get("q") or "").strip().casefold()
        status = str(request.args.get("status") or "").strip()
        hide_confirmed = str(request.args.get("hide_confirmed") or "0").strip().lower() in {"1", "true", "yes"}
        # Keep the API backward-compatible (no mode means all); the browser
        # explicitly requests face-first mode for the default review view.
        mode = str(request.args.get("mode") or "all").strip().casefold()
        if mode not in {"all", "face", "name"}:
            mode = "all"
        identity_search_keys: set[str] = set()
        if query:
            normalized_query = sorter.normalize_key(query)
            for identity in catalog_rows():
                names = [identity["canonical"], *identity.get("aliases", [])]
                if any(
                    query in str(name).casefold()
                    or (normalized_query and normalized_query in sorter.normalize_key(str(name)))
                    for name in names
                ):
                    identity_search_keys.update(sorter.normalize_key(str(name)) for name in names)
        alias_matches = {
            cluster["cluster_id"]: bool(identity_search_keys) and any(
                sorter.normalize_key(identity) in identity_search_keys
                for identity in cluster.get("expected_identities", [])
            )
            for cluster in clusters
        }
        filtered = [
            cluster
            for cluster in clusters
            if (mode == "all" or (mode == "face" and (str(cluster.get("key") or "").startswith("folder:") or cluster.get("face_cluster_labels") or set(cluster.get("review_methods", [])) & {"face-only", "name+face", "face-identity"})) or (mode == "name" and not (cluster.get("face_cluster_labels") or set(cluster.get("review_methods", [])) & {"face-only", "name+face", "face-identity"})))
            and (not query or query in cluster["title"].casefold() or query in cluster["key"].casefold() or alias_matches[cluster["cluster_id"]])
            and (not status or (decisions.get(cluster["cluster_id"], {}).get("status", "pending") == status))
        ]
        # Refresh once per request so external UI processes' completed moves
        # are reflected without rereading the ledger for every cluster.
        refresh_move_history()
        live_paths_by_cluster = {
            cluster["cluster_id"]: [
                path for path in live_media_paths(cluster.get("paths", []))
            ]
            for cluster in filtered
        }
        confirmed_by_cluster = {
            cluster_id: confirmed_paths(paths)
            for cluster_id, paths in live_paths_by_cluster.items()
        }
        all_live_paths = [path for paths in live_paths_by_cluster.values() for path in paths]
        grouped_paths = manual_group_memberships(all_live_paths)
        cluster_counts: Dict[str, Dict[str, int]] = {}
        for cluster in filtered:
            cluster_id = cluster["cluster_id"]
            paths = live_paths_by_cluster[cluster_id]
            hidden_paths = confirmed_by_cluster[cluster_id]
            grouped_unassigned = sum(
                bool(grouped_paths.get(path))
                and path not in hidden_paths
                and _image_decision_key(path) not in queued_assignments_by_path
                and _image_decision_key(path) not in pending_assignments
                for path in paths
            )
            total_count = len(paths)
            hidden_count = len(hidden_paths)
            unassigned_count = max(0, total_count - hidden_count - grouped_unassigned)
            cluster_counts[cluster_id] = {
                "total_count": total_count,
                "hidden_count": hidden_count,
                "grouped_unassigned_count": grouped_unassigned,
                "unassigned_count": unassigned_count,
            }
        display_paths_by_cluster = {
            cluster_id: [
                path for path in paths
                if not hide_confirmed or path not in confirmed_by_cluster[cluster_id]
            ]
            for cluster_id, paths in live_paths_by_cluster.items()
        }
        filtered = [
            cluster for cluster in filtered
            if display_paths_by_cluster[cluster["cluster_id"]]
        ]
        filtered.sort(key=lambda cluster: (
            -cluster_counts[cluster["cluster_id"]]["unassigned_count"],
            -int(cluster.get("quality_rank") or 0),
            -cluster_counts[cluster["cluster_id"]]["total_count"],
            cluster["title"].casefold(),
        ))
        start = (page - 1) * page_size
        return jsonify({
            "total": len(filtered), "page": page, "page_size": page_size,
            "has_next": start + page_size < len(filtered),
            "clusters": [
                {
                    **_public_cluster(cluster, decisions.get(cluster["cluster_id"])),
                    "count": len(display_paths_by_cluster[cluster["cluster_id"]]),
                    **cluster_counts[cluster["cluster_id"]],
                    "sample_paths": display_paths_by_cluster[cluster["cluster_id"]][:12],
                    "manual_groups_by_path": {
                        path: grouped_paths[path]
                        for path in display_paths_by_cluster[cluster["cluster_id"]][:12]
                        if grouped_paths.get(path)
                    },
                    "identity_alias_match": alias_matches[cluster["cluster_id"]],
                }
                for cluster in filtered[start:start + page_size]
            ],
        })

    @app.post("/api/clusters/<cluster_id>/status")
    def set_cluster_status(cluster_id: str):
        cluster = by_id.get(cluster_id)
        if cluster is None:
            return jsonify({"error": "unknown cluster"}), 404
        body = request.get_json(silent=True) or {}
        requested = str(body.get("status") or "").strip().lower()
        status = {"approve": "confirmed", "approved": "confirmed", "reject": "rejected", "rejected": "rejected"}.get(requested)
        if status is None:
            return jsonify({"error": "status must be approve or reject"}), 400
        purity_flags = _cluster_purity_flags(cluster)
        if status == "confirmed" and purity_flags and not bool(body.get("purity_ack")):
            return jsonify({
                "error": "cluster requires image-level purity review before bulk confirmation",
                "purity_flags": purity_flags,
                "sample_paths": cluster.get("sample_paths", []),
            }), 409
        expected = cluster.get("expected_identities") or []
        identity = str(expected[0] if expected else cluster.get("title") or "review").strip()
        decision = {"cluster_id": cluster_id, "identity": identity, "family": "review", "status": status, "aliases": [], "notes": "quick cluster review", "sample_paths": cluster.get("sample_paths", []), "count": cluster.get("count", 0), "saved_at": datetime.now(timezone.utc).isoformat()}
        with write_lock:
            decisions[cluster_id] = decision
            _write_decisions(decisions_path, decisions)
        return jsonify(_public_cluster(cluster, decision)), 201

    @app.get("/api/clusters/<cluster_id>")
    def get_cluster(cluster_id: str):
        refresh_pending_assignments()
        refresh_applied_assignments()
        refresh_queued_assignments()
        refresh_face_markers()
        cluster = by_id.get(cluster_id)
        if cluster is None:
            return jsonify({"error": "unknown cluster"}), 404
        hide_confirmed = str(request.args.get("hide_confirmed") or "0").strip().lower() in {"1", "true", "yes"}
        refresh_move_history()
        live_paths = live_media_paths(cluster["paths"], check_all=True)
        hidden = confirmed_paths(live_paths)
        visible_paths = [path for path in live_paths if not hide_confirmed or path not in hidden]
        result = _public_cluster(cluster, decisions.get(cluster_id))
        result["paths"] = visible_paths
        result["count"] = len(visible_paths)
        result["sample_paths"] = visible_paths[:12]
        result["hidden_confirmed"] = len(hidden)
        result["face_link_scores"] = cluster.get("face_link_scores", {})
        result["manual_groups_by_path"] = manual_group_memberships(visible_paths)
        result["image_decisions"] = {
            path: decision for path in cluster["paths"] if (decision := decision_for_path(path)) is not None
        }
        return jsonify(result)

    @app.post("/api/clusters/<cluster_id>/members")
    def update_member(cluster_id: str):
        if cluster_id not in by_id:
            return jsonify({"error": "unknown cluster"}), 404
        body = request.get_json(silent=True) or {}
        path = str(body.get("path") or "")
        action = str(body.get("action") or "").strip().lower()
        target_id = str(body.get("target_cluster_id") or cluster_id)
        if path not in known_paths:
            return jsonify({"error": "path is not in the audit index"}), 404
        if action not in {"add", "remove"}:
            return jsonify({"error": "action must be add or remove"}), 400
        overrides = _read_overrides(overrides_path)
        moves = {str(key): str(value) for key, value in (overrides.get("moves") or {}).items()}
        removed = {str(value) for value in (overrides.get("removed") or [])}
        if action == "add":
            if target_id not in by_id:
                return jsonify({"error": "unknown target cluster"}), 404
            removed.discard(path)
            moves[path] = target_id
        else:
            removed.add(path)
            moves.pop(path, None)
        with write_lock:
            _atomic_json_write(overrides_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "moves": moves, "removed": sorted(removed)})
            _apply_overrides(clusters, {"moves": moves, "removed": removed})
        return jsonify({"saved": True, "path": path, "action": action, "cluster": _public_cluster(by_id[cluster_id], decisions.get(cluster_id))}), 201

    @app.get("/api/identities")
    def identities():
        result = identity_rows_with_local_decisions()
        scope = request.args.get("scope", "")
        if scope == "baseline":
            result = [item for item in result if item.get("family") in BASELINE_IDENTITY_FAMILIES]
        # catalog_rows contains every identity registered by the sorter; all
        # registry families are valid identity targets in the review pickers.
        return jsonify(_aggregate_identity_options(result))

    @app.get("/api/identity-reconciliation")
    def identity_reconciliation():
        return jsonify(_identity_reconciliation_snapshot(registry_path, identity_reconciliation_decisions_path))

    @app.get("/identity-reconciliation")
    def identity_reconciliation_page():
        return IDENTITY_RECONCILIATION_PAGE

    @app.post("/api/identity-reconciliation")
    def save_identity_reconciliation():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be an object"}), 400
        source = str(body.get("source") or "").strip()
        action = str(body.get("action") or "")
        snapshot = _identity_reconciliation_snapshot(registry_path, identity_reconciliation_decisions_path)
        manual = next((item for item in snapshot["manual_entries"] if item["canonical"] == source), None)
        if manual is None:
            return jsonify({"error": "manual identity is missing or already matches the shared registry"}), 404
        decision: Dict[str, Any] = {
            "source": source,
            "status": "deferred",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        if action == "link":
            target = str(body.get("target") or "").strip()
            shared = next((item for item in snapshot["shared_identities"] if item["id"] == target), None)
            if shared is None:
                return jsonify({"error": "target must be a confirmed shared identity"}), 400
            decision.update({"action": "link", "target": target, "status": "ready_for_registry_owner"})
        elif action == "new":
            proposed = str(body.get("proposed_id") or "").strip()
            if not _valid_identity(proposed):
                return jsonify({"error": "proposed shared identity name is invalid"}), 400
            shared_keys = {
                sorter.normalize_key(value)
                for item in snapshot["shared_identities"]
                for value in [item["id"], item["primary_folder"], *item["display_names"], *item["aliases"]]
                if sorter.normalize_key(value)
            }
            if sorter.normalize_key(proposed) in shared_keys:
                return jsonify({"error": "that name already matches a shared identity; link it instead"}), 409
            decision.update({"action": "new", "proposed_id": proposed, "status": "ready_for_registry_owner"})
        elif action == "defer":
            decision.update({"action": "defer", "status": "deferred"})
        else:
            return jsonify({"error": "action must be link, new, or defer"}), 400
        with identity_reconciliation_lock:
            try:
                saved = json.loads(identity_reconciliation_decisions_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            decisions = saved.get("decisions", {})
            if not isinstance(decisions, dict):
                decisions = {}
            previous = decisions.get(source, {})
            history = previous.get("history", []) if isinstance(previous, dict) else []
            if not isinstance(history, list):
                history = []
            decision["history"] = [*history, {key: value for key, value in decision.items() if key != "history"}]
            decisions[source] = decision
            _atomic_json_write(identity_reconciliation_decisions_path, {
                "schema_version": 1,
                "updated_at": decision["reviewed_at"],
                "decisions": decisions,
            })
        return jsonify({"saved": True, "decision": decision, "registry_changed": False}), 201

    def manual_group_move_destinations() -> Dict[str, str]:
        destinations: Dict[str, str] = {}
        for operation in move_history:
            if operation.get("undone"):
                continue
            for move in operation.get("moves", []):
                if isinstance(move, dict) and move.get("source") and move.get("destination"):
                    destinations[_lexical_path(str(move["source"]))] = str(move["destination"])
        return destinations

    def remap_manual_group_path(path: str, destinations: Dict[str, str]) -> str:
        seen: set[str] = set()
        while (key := _lexical_path(path)) in destinations and key not in seen:
            seen.add(key)
            path = destinations[key]
        return path

    def resolve_manual_group_paths(
        group: Dict[str, Any], move_destinations: Optional[Dict[str, str]] = None
    ) -> List[str]:
        if move_destinations is None:
            refresh_move_history()
            move_destinations = manual_group_move_destinations()
        resolved = []
        for raw_path in group.get("paths", []):
            path = remap_manual_group_path(str(raw_path), move_destinations)
            if is_known_media_path(path) and (path == str(raw_path) or Path(_lexical_path(path)).is_file()):
                try:
                    resolved.append(path)
                except OSError:
                    continue
        return sorted(set(resolved))

    def identity_assigned(path: str) -> bool:
        key = _image_decision_key(path)
        decision = (
            image_decisions.get(key)
            or decision_by_path.get(key)
            or applied_assignments_by_path.get(key)
            or {}
        )
        return decision.get("status") == "confirmed" and bool(str(decision.get("identity") or "").strip())

    def visible_manual_group_paths(
        group: Dict[str, Any], move_destinations: Optional[Dict[str, str]] = None
    ) -> List[str]:
        return [
            path for path in resolve_manual_group_paths(group, move_destinations)
            if not identity_assigned(path)
        ]

    def manual_group_memberships(paths: Iterable[str]) -> Dict[str, List[str]]:
        """Return unassigned collection labels for visible paths."""
        refresh_move_history()
        move_destinations = manual_group_move_destinations()
        requested = {_lexical_path(str(path)): str(path) for path in paths}
        memberships: Dict[str, set[str]] = {path: set() for path in requested.values()}
        latest_groups = _read_manual_groups(manual_groups_path)
        for group in latest_groups.values():
            for raw_path in group.get("paths", []):
                current_path = remap_manual_group_path(str(raw_path), move_destinations)
                visible_path = requested.get(_lexical_path(current_path))
                if visible_path is not None and not identity_assigned(visible_path):
                    memberships[visible_path].add(group["name"])
        return {path: sorted(names, key=str.casefold) for path, names in memberships.items() if names}

    @app.get("/api/manual-groups")
    def list_manual_groups():
        manual_groups.clear()
        manual_groups.update(_read_manual_groups(manual_groups_path))
        refresh_move_history()
        move_destinations = manual_group_move_destinations()
        result = [
            {"name": group["name"], "count": len(visible_manual_group_paths(group, move_destinations)), "updated_at": group.get("updated_at")}
            for group in manual_groups.values()
        ]
        return jsonify(sorted(result, key=lambda item: item["name"].casefold()))

    @app.get("/api/manual-groups/<group_name>")
    def get_manual_group(group_name: str):
        key = sorter.normalize_key(group_name)
        manual_groups.clear()
        manual_groups.update(_read_manual_groups(manual_groups_path))
        group = manual_groups.get(key)
        if group is None:
            return jsonify({"error": "manual collection not found"}), 404
        paths = visible_manual_group_paths(group)
        return jsonify({"name": group["name"], "paths": paths, "count": len(paths), "updated_at": group.get("updated_at"), "manual_groups_by_path": manual_group_memberships(paths)})

    @app.post("/api/manual-groups")
    def update_manual_group():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        key = sorter.normalize_key(name)
        if not _valid_identity(name) or not key:
            return jsonify({"error": "collection name must be a safe, non-empty name"}), 400
        raw_paths = body.get("paths") or []
        if not isinstance(raw_paths, list) or not raw_paths or len(raw_paths) > 500:
            return jsonify({"error": "select 1 to 500 audit images for the collection"}), 400
        paths = list(dict.fromkeys(str(path) for path in raw_paths if str(path)))
        if not paths or any(not is_known_media_path(path) for path in paths):
            return jsonify({"error": "collection members must be media paths from the current audit"}), 400
        now = datetime.now(timezone.utc).isoformat()
        with write_lock:
            latest = _read_manual_groups(manual_groups_path)
            group = latest.get(key, {"name": name, "paths": [], "created_at": now})
            existing_paths = resolve_manual_group_paths(group)
            existing_keys = {_lexical_path(path) for path in existing_paths}
            added = [path for path in paths if _lexical_path(path) not in existing_keys]
            group.update({"name": group.get("name") or name, "paths": sorted(set(existing_paths) | set(paths)), "updated_at": now})
            latest[key] = group
            undo_id = None
            if added:
                undo_id = uuid.uuid4().hex
                actions = _read_manual_group_actions(manual_group_actions_path)
                actions.append({
                    "id": undo_id,
                    "kind": "group",
                    "name": group["name"],
                    "key": key,
                    "paths": added,
                    "saved_at": now,
                    "undone": False,
                })
                _write_manual_group_actions(manual_group_actions_path, actions)
            _atomic_json_write(manual_groups_path, {"schema_version": 1, "updated_at": now, "groups": list(latest.values())})
            manual_groups.clear()
            manual_groups.update(latest)
        return jsonify({"name": group["name"], "added": len(added), "count": len(group["paths"]), "undo_id": undo_id, "identity_changed": False, "files_moved": False}), 201

    @app.post("/api/manual-groups/<group_name>/remove")
    def remove_manual_group_members(group_name: str):
        body = request.get_json(silent=True) or {}
        raw_paths = body.get("paths") or []
        if not isinstance(raw_paths, list) or not raw_paths or len(raw_paths) > 500:
            return jsonify({"error": "paths must contain 1 to 500 collection members"}), 400
        remove_keys = {_lexical_path(str(path)) for path in raw_paths if str(path)}
        key = sorter.normalize_key(group_name)
        now = datetime.now(timezone.utc).isoformat()
        with write_lock:
            latest = _read_manual_groups(manual_groups_path)
            group = latest.get(key)
            if group is None:
                return jsonify({"error": "manual collection not found"}), 404
            current_paths = resolve_manual_group_paths(group)
            group["paths"] = [path for path in current_paths if _lexical_path(path) not in remove_keys]
            group["updated_at"] = now
            latest[key] = group
            _atomic_json_write(manual_groups_path, {"schema_version": 1, "updated_at": now, "groups": list(latest.values())})
            manual_groups.clear()
            manual_groups.update(latest)
        return jsonify({"name": group["name"], "count": len(group["paths"]), "removed": len(remove_keys)}), 200

    @app.get("/api/assorted-folders")
    def assorted_folders():
        """List person-like assorted folders without reading or moving media."""
        assorted_associations.clear()
        assorted_associations.update(_read_assorted_associations(assorted_associations_path))
        return jsonify({
            "root": str(assorted_root),
            "folders": _scan_assorted_folders(assorted_root, assorted_associations),
            "read_only": True,
            "moves_enabled": False,
        })

    @app.post("/api/assorted-folder-associations")
    def associate_assorted_folder():
        """Associate an assorted folder with an identity; never move its files."""
        body = request.get_json(silent=True) or {}
        folder = _assorted_folder_path(str(body.get("folder") or ""), assorted_root)
        if folder is None or not _assorted_folder_is_candidate(folder.name):
            return jsonify({"error": "folder must be a non-generic directory under the assorted root"}), 400
        identity = str(body.get("identity") or body.get("canonical") or "").strip()
        family = str(body.get("family") or "review").strip()
        if not _valid_identity(identity):
            return jsonify({"error": "identity is required and must be a safe name"}), 400
        if family not in FAMILIES:
            return jsonify({"error": "invalid identity family"}), 400
        refresh_review_identities()
        catalog = catalog_rows()
        known_names = {}
        for item in [*catalog, *review_identities.values()]:
            canonical = str(item.get("canonical") or item.get("identity") or "").strip()
            aliases = item.get("aliases") or []
            for name in [canonical, *(aliases if isinstance(aliases, list) else [])]:
                normalized = sorter.normalize_key(str(name))
                if normalized:
                    known_names.setdefault(normalized, canonical or str(name))
        known = set(known_names)
        key = sorter.normalize_key(identity)
        create_identity = bool(body.get("create_identity"))
        if create_identity and key in known:
            return jsonify({"error": f"{identity!r} conflicts with existing identity {known_names[key]!r}. Choose a different name.", "collision": True}), 409
        if key not in known and not create_identity:
            return jsonify({"error": "identity is not in the catalog; set create_identity to create a local identity"}), 409
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "folder": str(folder),
            "label": folder.name,
            "identity": identity,
            "family": family,
            "status": "associated",
            "created_identity": key not in known,
            "notes": str(body.get("notes") or "").strip()[:1000],
            "saved_at": now,
            "source": "assorted_folder_ui",
        }
        with write_lock:
            if record["created_identity"]:
                review_identities[key] = {
                    "cluster_id": identity,
                    "identity": identity,
                    "family": family,
                    "aliases": [],
                    "status": "pending",
                    "notes": "Created from assorted folder association; no media moved.",
                    "saved_at": now,
                }
                _write_decisions(review_identities_path, review_identities)
            assorted_associations[_lexical_path(str(folder))] = record
            _atomic_json_write(
                assorted_associations_path,
                {
                    "schema_version": 1,
                    "updated": now,
                    "root": str(assorted_root),
                    "associations": list(assorted_associations.values()),
                },
            )
            _append_review_ledger(ledger_path, {
                "event": "assorted_folder_association",
                "folder": str(folder),
                "identity": identity,
                "family": family,
                "created_identity": record["created_identity"],
                "moved": [],
                "saved_at": now,
            })
        return jsonify({**record, "moved": [], "applied": False, "message": "Identity association saved; no files were moved."}), 201

    @app.get("/api/identity-picker/settings")
    def identity_picker_settings():
        return jsonify({"prefixes": _read_identity_picker_prefixes(identity_picker_settings_path)})

    @app.post("/api/identity-picker/settings")
    def save_identity_picker_settings():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be an object"}), 400
        try:
            prefixes = _normalize_identity_picker_prefixes(body.get("prefixes"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _atomic_json_write(identity_picker_settings_path, {
            "schema_version": 1,
            "prefixes": prefixes,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return jsonify({"prefixes": prefixes}), 200

    @app.get("/api/recent-choices")
    def recent_choices():
        with recent_choices_lock:
            return jsonify(_read_recent_choices(recent_choices_path))

    @app.post("/api/recent-choices")
    def remember_recent_choice():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be an object"}), 400
        kind = str(body.get("kind") or "")
        with recent_choices_lock:
            recent = _read_recent_choices(recent_choices_path)
            if kind == "identity":
                canonical = str(body.get("canonical") or "").strip()[:256]
                if not canonical:
                    return jsonify({"error": "canonical identity is required"}), 400
                family = str(body.get("family") or "review")
                choice = {"canonical": canonical, "family": family if family in FAMILIES else "review"}
                recent["identities"] = [choice, *[
                    item for item in recent["identities"]
                    if item["canonical"].casefold() != canonical.casefold()
                ]][:20]
            elif kind == "group":
                name = str(body.get("name") or "").strip()[:128]
                if not name:
                    return jsonify({"error": "group name is required"}), 400
                recent["groups"] = [name, *[
                    item for item in recent["groups"] if item.casefold() != name.casefold()
                ]][:20]
            else:
                return jsonify({"error": "kind must be identity or group"}), 400
            _atomic_json_write(recent_choices_path, {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **recent,
            })
        return jsonify(recent), 200

    @app.get("/api/identity-groups")
    def identity_groups():
        refresh_pending_assignments()
        refresh_face_markers()
        include_paths = request.args.get("include_paths", "0").strip().lower() in {"1", "true", "yes"}
        prefixes = tuple(value.strip().casefold() for value in request.args.getlist("prefix") if value.strip())
        verified_examples = request.args.get("verified_examples", "0").strip().lower() in {"1", "true", "yes"}
        # Disk scans of the sorted library are opt-in. The default path uses
        # review/image decisions and confirmed cluster assignments only so the
        # identities tab cannot freeze the UI on a multi-second rglob.
        include_disk = request.args.get("include_disk", "0").strip().lower() in {"1", "true", "yes"}
        groups: Dict[str, Dict[str, Any]] = {}
        identity_catalog, _, _, _, preferred_targets = sorter.load_identity_catalog()
        catalog_by_key: Dict[str, sorter.Identity] = {}
        for item in identity_catalog:
            catalog_by_key.setdefault(sorter.normalize_key(item.canonical), item)
        catalog_alias_targets: Dict[str, set[str]] = {}
        for item in identity_catalog:
            for name in [item.canonical, *item.aliases]:
                key = sorter.normalize_key(name)
                if key:
                    catalog_alias_targets.setdefault(key, set()).add(sorter.normalize_key(item.canonical))

        def ensure_group(raw_identity: str, family: str, aliases: Iterable[str] = ()) -> Dict[str, Any]:
            """Display known aliases under one catalog canonical, retaining the alias as metadata."""
            raw_name = raw_identity.strip()
            raw_key = sorter.normalize_key(raw_name)
            target_key = preferred_targets.get(raw_key)
            if not target_key:
                alias_targets = catalog_alias_targets.get(raw_key, set())
                if len(alias_targets) == 1:
                    target_key = next(iter(alias_targets))
            catalog_identity = catalog_by_key.get(target_key or raw_key)
            if catalog_identity is not None:
                raw_identity = catalog_identity.canonical
                family = catalog_identity.family
                if sorter.normalize_key(raw_name) != sorter.normalize_key(raw_identity):
                    aliases = (*aliases, raw_name)
            key = sorter.normalize_key(raw_identity)
            generic = sorter.normalize_key(raw_identity) in (
                sorter.PROJECT_BLOCKED_TOKENS | sorter.PROJECT_AMBIGUOUS_TOKENS
            )
            group = groups.setdefault(
                key,
                {
                    "identity": raw_identity,
                    "family": family or "review",
                    "generic": generic,
                    "aliases": [],
                    "paths": [],
                    "status_by_path": {},
                },
            )
            group["aliases"] = sorted(
                {str(alias).strip() for alias in (*group.get("aliases", []), *aliases) if str(alias).strip() and sorter.normalize_key(str(alias)) != sorter.normalize_key(raw_identity)},
                key=str.casefold,
            )
            return group
        rows = [{"canonical": item["canonical"], "family": item["family"], "aliases": item.get("aliases", [])} for item in identity_rows_with_local_decisions()]
        for identity in rows:
            raw_identity = str(identity.get("canonical") or "").strip()
            if raw_identity:
                ensure_group(raw_identity, str(identity.get("family") or "review"), identity.get("aliases", []))
        if include_disk:
            for identity in rows:
                raw_identity = str(identity.get("canonical") or "").strip()
                if not raw_identity:
                    continue
                family = str(identity.get("family") or "review")
                identity_dir = sorter.destination_for(sorter.Identity(raw_identity, family, ()), DEFAULT_REVIEW_DEST_ROOT)
                group = ensure_group(raw_identity, family, identity.get("aliases", []))
                for path in iter_media_files(identity_dir):
                    raw_path = str(path)
                    group["paths"].append(raw_path)
                    group["status_by_path"].setdefault(raw_path, "confirmed")
        for decision in image_decisions.values():
            raw_identity = str(decision.get("identity") or "").strip()
            if not raw_identity:
                continue
            group = ensure_group(raw_identity, str(decision.get("family") or "review"))
            path = str(decision.get("path") or "")
            if path and is_known_media_path(path):
                group["paths"].append(path)
                group["status_by_path"][path] = str(decision.get("status") or "pending")
        for marker in face_markers.values():
            raw_identity = str(marker.get("identity") or "").strip()
            if not raw_identity:
                continue
            group = ensure_group(raw_identity, str(marker.get("family") or "review"))
            path = str(marker.get("path") or "")
            if path and is_known_media_path(path):
                group["paths"].append(path)
                group["status_by_path"][path] = str(marker.get("status") or "pending")
        for decision in pending_assignments.values():
            raw_identity = str(decision.get("identity") or "").strip()
            if not raw_identity:
                continue
            group = ensure_group(raw_identity, str(decision.get("family") or "review"))
            path = str(decision.get("path") or "")
            if path and is_known_media_path(path):
                group["paths"].append(path)
                group["status_by_path"][path] = "queued"
        for cluster in clusters:
            decision = decisions.get(cluster["cluster_id"]) or {}
            raw_identity = str(decision.get("identity") or "").strip()
            if not raw_identity:
                continue
            group = ensure_group(raw_identity, str(decision.get("family") or "review"))
            status = str(decision.get("status") or "pending")
            for path in cluster.get("paths", []):
                group["paths"].append(path)
                group["status_by_path"].setdefault(path, status)
        for group in groups.values():
            group["paths"] = sorted(set(group["paths"]))
            group["count"] = len(group["paths"])
            group["sample_paths"] = group["paths"][:12]
            if verified_examples and prefixes and group["identity"].casefold().startswith(prefixes):
                image_extensions = MEDIA_EXTENSIONS - {".mp4", ".mov"}
                examples = []
                for raw_path in group["paths"]:
                    if Path(raw_path).suffix.casefold() not in image_extensions:
                        continue
                    candidate = Path(_lexical_path(raw_path))
                    try:
                        if not candidate.is_symlink() and candidate.is_file():
                            examples.append(raw_path)
                    except OSError:
                        continue
                    if len(examples) == 2:
                        break
                group["sample_paths"] = examples
            statuses = group.pop("status_by_path", {})
            group["confirmed"] = sum(statuses.get(path) == "confirmed" for path in group["paths"])
            group["pending"] = sum(statuses.get(path, "pending") == "pending" for path in group["paths"])
            group["rejected"] = sum(statuses.get(path) == "rejected" for path in group["paths"])
            group["status_by_path"] = statuses if include_paths else {
                path: statuses[path] for path in group["sample_paths"] if path in statuses
            }
            if not include_paths:
                group.pop("paths", None)
            group["disk_scanned"] = include_disk
        sample_memberships = manual_group_memberships(
            path for group in groups.values() for path in group.get("sample_paths", [])
        )
        for group in groups.values():
            group["manual_groups_by_path"] = {
                path: sample_memberships[path]
                for path in group.get("sample_paths", [])
                if sample_memberships.get(path)
            }
        result = sorted(groups.values(), key=lambda item: (str(item.get("family") or "").casefold(), item["identity"].casefold()))
        if prefixes:
            result = [item for item in result if item["identity"].casefold().startswith(prefixes)]
        return jsonify(result)

    @app.post("/api/identities")
    def create_review_identity():
        body = request.get_json(silent=True) or {}
        canonical = str(body.get("canonical") or body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        if not _valid_identity(canonical):
            return jsonify({"error": "canonical identity is required and must be a safe folder name"}), 400
        if family not in FAMILIES:
            return jsonify({"error": "invalid family"}), 400
        paths = list(dict.fromkeys(str(path) for path in body.get("paths", []) if str(path)))
        if len(paths) > 200 or any(not is_known_media_path(path) for path in paths):
            return jsonify({"error": "paths must contain at most 200 images from the audit index"}), 400
        refresh_review_identities()
        raw_aliases = body.get("aliases") or []
        if not isinstance(raw_aliases, list) or len(raw_aliases) > 100:
            return jsonify({"error": "aliases must be a list of at most 100 names"}), 400
        requested_aliases = list(dict.fromkeys(str(value).strip() for value in raw_aliases if str(value).strip()))
        if any(not _valid_identity(alias) for alias in requested_aliases):
            return jsonify({"error": "aliases must be safe names without path separators"}), 400
        canonical_key = sorter.normalize_key(canonical)
        known_names = {}
        for item in [*catalog_rows(), *review_identities.values()]:
            existing = str(item.get("canonical") or item.get("identity") or "").strip()
            aliases = item.get("aliases") or []
            for name in [existing, *(aliases if isinstance(aliases, list) else [])]:
                normalized = sorter.normalize_key(str(name))
                if normalized:
                    known_names.setdefault(normalized, existing or str(name))
        if canonical_key in known_names:
            conflict = known_names[canonical_key]
            return jsonify({"error": f"{canonical!r} conflicts with existing identity {conflict!r}. Choose a different name.", "collision": True, "identity": conflict}), 409
        for alias in requested_aliases:
            conflict = known_names.get(sorter.normalize_key(alias))
            if conflict and sorter.normalize_key(conflict) != canonical_key:
                return jsonify({"error": f"alias {alias!r} conflicts with existing identity {conflict!r}.", "collision": True, "identity": conflict}), 409
        notes = str(body.get("notes") or "").strip()[:1000]
        record = {"cluster_id": canonical, "identity": canonical, "family": family, "aliases": requested_aliases, "notes": notes, "saved_at": datetime.now(timezone.utc).isoformat()}
        registry_added = False
        with write_lock:
            if family != "review":
                try:
                    registry_added = add_project_registry_identity(canonical, family, requested_aliases, notes, registry_path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    return jsonify({"error": f"Unable to save identity to the local registry: {exc}"}), 409
            review_identities[canonical] = record
            _write_decisions(review_identities_path, review_identities)
            saved_at = record["saved_at"]
            for path in paths:
                key = _image_decision_key(path)
                image_decisions[key] = {
                    "key": key,
                    "scope": "image",
                    "path": path,
                    "identity": canonical,
                    "family": family,
                    "status": "confirmed",
                    "notes": str(body.get("notes") or "").strip()[:1000],
                    "saved_at": saved_at,
                }
            if paths:
                _write_image_decisions(image_decisions_path, image_decisions)
            if paths:
                # Capture hashes before the move, but do not publish markers
                # until the corresponding file move succeeds.
                _record_face_markers(face_markers_path, face_markers, paths, canonical, family, "confirmed", persist=False)
            move_result = move_review_paths(paths, canonical, family, allowed_roots, DEFAULT_REVIEW_DEST_ROOT) if paths else {"moved": [], "errors": []}
            moved_sources = {str(item.get("source")) for item in move_result.get("moved", [])}
            for path in paths:
                if path not in moved_sources:
                    image_decisions[_image_decision_key(path)]["status"] = "needs-evidence"
                    face_markers.pop(_image_decision_key(path), None)
            if move_result["moved"]:
                _relink_path_records(move_result["moved"], image_decisions, preserve_source_path=True)
                _relink_path_records(move_result["moved"], face_markers)
                _write_image_decisions(image_decisions_path, image_decisions)
                _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            elif paths:
                _write_image_decisions(image_decisions_path, image_decisions)
                _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            move_id = _record_move_operation(move_history_path, move_history, move_result, canonical, family)
            _record_move_audit(ledger_path, move_id, move_result, canonical, family)
        return jsonify({**record, **move_result, "move_id": move_id, "registry_added": registry_added}), 201

    @app.post("/api/image-decisions")
    def save_image_decisions():
        body = request.get_json(silent=True) or {}
        paths = list(dict.fromkeys(str(path) for path in body.get("paths", []) if str(path)))
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        status = str(body.get("status") or "pending").strip()
        if not paths or len(paths) > 200:
            return jsonify({"error": "paths must contain 1 to 200 images"}), 400
        if any(not is_known_media_path(path) for path in paths):
            return jsonify({"error": "one or more paths are not in the audit index"}), 404
        if not _valid_identity(identity):
            return jsonify({"error": "identity is required and must be a safe folder name"}), 400
        if family not in FAMILIES or status not in DECISION_STATUSES:
            return jsonify({"error": "family or status is invalid"}), 400
        saved_at = datetime.now(timezone.utc).isoformat()
        with write_lock:
            for path in paths:
                key = _image_decision_key(path)
                image_decisions[key] = {"key": key, "scope": "image", "path": path, "identity": identity, "family": family, "status": status, "notes": str(body.get("notes") or "").strip()[:1000], "saved_at": saved_at}
            _write_image_decisions(image_decisions_path, image_decisions)
            # Capture the source fingerprint before a confirmed assignment moves
            # the file; after the move the audit path is intentionally absent.
            _record_face_markers(face_markers_path, face_markers, paths, identity, family, status, persist=False)
            _append_review_ledger(ledger_path, {
                "event": "image_decision",
                "scope": "image",
                "paths": paths,
                "sha256": {path: face_markers.get(_image_decision_key(path), {}).get("sha256") for path in paths},
                "identity": identity,
                "family": family,
                "status": status,
                "notes": str(body.get("notes") or "").strip()[:1000],
                "saved_at": saved_at,
            })
            move_result = move_review_paths(paths, identity, family, allowed_roots, DEFAULT_REVIEW_DEST_ROOT) if status == "confirmed" else {"moved": [], "errors": []}
            if status == "confirmed":
                moved_sources = {str(item.get("source")) for item in move_result.get("moved", [])}
                for path in paths:
                    if path not in moved_sources:
                        image_decisions[_image_decision_key(path)]["status"] = "needs-evidence"
                        face_markers.pop(_image_decision_key(path), None)
            if move_result["moved"]:
                _relink_path_records(move_result["moved"], image_decisions, preserve_source_path=True)
                _relink_path_records(move_result["moved"], face_markers)
                _write_image_decisions(image_decisions_path, image_decisions)
                _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            elif paths:
                _write_image_decisions(image_decisions_path, image_decisions)
                _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            move_id = _record_move_operation(move_history_path, move_history, move_result, identity, family)
            _record_move_audit(ledger_path, move_id, move_result, identity, family)
        return jsonify({"saved": len(paths), "identity": identity, "paths": paths, **move_result, "move_id": move_id}), 201

    @app.post("/api/image-decisions/pending")
    def queue_image_decisions():
        """Record review intent without moving media or updating markers."""
        body = request.get_json(silent=True) or {}
        paths = list(dict.fromkeys(str(path) for path in body.get("paths", []) if str(path)))
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        status = str(body.get("status") or "confirmed").strip()
        if not paths or len(paths) > 200:
            return jsonify({"error": "paths must contain 1 to 200 images"}), 400
        if any(not is_known_media_path(path) for path in paths):
            return jsonify({"error": "one or more paths are not in the audit index"}), 404
        if not _valid_identity(identity):
            return jsonify({"error": "identity is required and must be a safe folder name"}), 400
        if family not in FAMILIES or status not in DECISION_STATUSES:
            return jsonify({"error": "family or status is invalid"}), 400
        queued_at = datetime.now(timezone.utc).isoformat()
        notes = str(body.get("notes") or "").strip()[:1000]
        records: list[dict[str, Any]] = []
        for path in paths:
            key = _image_decision_key(path)
            try:
                fingerprint = sorter.file_sha256(Path(path))
            except OSError:
                fingerprint = None
            records.append({
                "key": key,
                "scope": "image",
                "path": path,
                "identity": identity,
                "family": family,
                "status": status,
                "notes": notes,
                "queued_at": queued_at,
                "source": "read_only_review",
                "audit": str(audit_path),
                "request_id": g.request_id,
                "sha256": fingerprint,
            })
        with write_lock:
            durable_ids = []
            try:
                for record in records:
                    durable_ids.append(queue_durable_assignment(
                        evidence_db_path,
                        path=record["path"],
                        identity=identity,
                        expected_sha256=record.get("sha256"),
                        confidence=body.get("confidence"),
                        source="review_ui",
                        created_by=str(request.headers.get("X-Picorg-User") or "operator")[:120],
                        provenance={"audit": str(audit_path), "request_id": g.request_id, "family": family, "status": status},
                    ))
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                LOGGER.exception("durable assignment queue write failed")
                return jsonify({"error": f"durable assignment queue unavailable: {exc}"}), 503
            for record, assignment_id in zip(records, durable_ids):
                record["assignment_id"] = assignment_id
            def merge_records(assignments: Dict[str, Dict[str, Any]]) -> None:
                assignments.update({record["key"]: record for record in records})

            latest_pending = _update_pending_assignments(pending_assignments_path, merge_records)
            pending_assignments.clear()
            pending_assignments.update(latest_pending)
            _append_review_ledger(ledger_path, {
                "event": "pending_assignment",
                "scope": "image",
                "paths": paths,
                "identity": identity,
                "family": family,
                "status": status,
                "queued": True,
                "queued_at": queued_at,
                "audit": str(audit_path),
                "request_id": g.request_id,
            })
        return jsonify({
            "queued": len(paths),
            "saved": len(paths),
            "identity": identity,
            "paths": paths,
            "pending_path": str(pending_assignments_path),
            "read_only": g.read_only,
            "applied": False,
            "message": "Assignment queued; it will be applied by the reconcile/apply step.",
        }), 202

    @app.post("/api/image-decisions/async")
    def queue_async_image_decision():
        """Queue one confirmed image and let the scheduler apply it in background."""
        body = request.get_json(silent=True) or {}
        paths = list(dict.fromkeys(str(path) for path in body.get("paths", []) if str(path)))
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        if len(paths) != 1 or not _valid_identity(identity) or family not in FAMILIES:
            return jsonify({"error": "exactly one valid path, identity, and family are required"}), 400
        path = paths[0]
        if not is_known_media_path(path):
            return jsonify({"error": "path is not in the audit index"}), 404
        try:
            source = Path(_lexical_path(path))
            if source.is_symlink() or not source.is_file():
                return jsonify({"error": "source media is missing or unreadable"}), 409
            expected_sha256 = sorter.file_sha256(source)
        except OSError as exc:
            return jsonify({"error": f"unable to fingerprint source media: {exc}"}), 409
        queued_at = datetime.now(timezone.utc).isoformat()
        with write_lock:
            try:
                assignment_id = queue_durable_assignment(
                    evidence_db_path,
                    path=path,
                    identity=identity,
                    expected_sha256=expected_sha256,
                    source="review_ui_async",
                    created_by=str(request.headers.get("X-Picorg-User") or "operator")[:120],
                    provenance={"audit": str(audit_path), "request_id": g.request_id, "family": family, "queued_at": queued_at},
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                return jsonify({"error": f"assignment queue unavailable: {exc}"}), 503
            _append_review_ledger(ledger_path, {"event": "async_assignment_queued", "assignment_id": assignment_id, "path": path, "identity": identity, "family": family, "sha256": expected_sha256, "queued_at": queued_at, "request_id": g.request_id})
            worker = None
            read_only = face_rebuild_is_active(face_rebuild_lock_path) or pipeline_lock_is_active(pipeline_lock_path)
            if not read_only:
                try:
                    worker = _launch_scheduler_process("run", "reconcile_confirmed", scheduler_config_path, scheduler_status_path)
                except RuntimeError:
                    # An existing scheduler job will consume the durable queue.
                    worker = {"already_running": True}
                except (OSError, ValueError) as exc:
                    LOGGER.warning("unable to start async assignment worker: %s", exc)
        return jsonify({"queued": 1, "assignment_id": assignment_id, "status": "pending", "worker": worker, "path": path, "identity": identity}), 202

    @app.post("/api/image-decisions/unassign")
    def unassign_image_decision():
        body = request.get_json(silent=True) or {}
        raw_path = str(body.get("path") or "")
        key = _image_decision_key(raw_path)
        decision = image_decisions.get(key)
        # Accept legacy clients that still post the pre-move audit path.  The
        # durable record is now keyed by the canonical destination, so resolve
        # that alias through the move ledger before rejecting the request.
        if decision is None and raw_path:
            requested = Path(raw_path).resolve()
            for operation in reversed(move_history):
                if operation.get("undone"):
                    continue
                for item in reversed(operation.get("moves", [])):
                    if item.get("unassigned"):
                        continue
                    if Path(str(item.get("source") or "")).resolve() == requested:
                        destination_key = _image_decision_key(str(item.get("destination") or ""))
                        decision = image_decisions.get(destination_key)
                        if decision is not None:
                            key = destination_key
                        break
                if decision is not None:
                    break
        if not raw_path or decision is None:
            return jsonify({"error": "image assignment not found"}), 404
        requested_path = Path(raw_path).resolve()
        original = requested_path
        move_item = None
        for operation in reversed(move_history):
            if operation.get("undone"):
                continue
            for item in reversed(operation.get("moves", [])):
                if item.get("unassigned"):
                    continue
                source_path = Path(str(item.get("source") or "")).resolve()
                destination_path = Path(str(item.get("destination") or "")).resolve()
                if source_path == requested_path or destination_path == requested_path:
                    move_item = item
                    # The UI normally sends the canonical destination after a
                    # confirmed assignment.  Restore it to the original
                    # audit path while keeping source/destination semantics
                    # unambiguous for the safety checks below.
                    original = source_path
                    break
            if move_item:
                break
        restored = None
        with write_lock:
            if move_item:
                destination = Path(str(move_item.get("destination") or "")).resolve()
                if original.exists() and destination.exists():
                    return jsonify({"error": "both original and assigned files exist; resolve the duplicate first"}), 409
                if not original.exists():
                    try:
                        if not destination.is_file() or not (destination == review_dest_root or review_dest_root in destination.parents):
                            raise ValueError("assigned file is missing or outside the review tree")
                        if not is_under_allowed_root(original):
                            raise ValueError("original path is outside the audit roots")
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(original))
                        restored = {"source": str(destination), "destination": str(original)}
                    except (OSError, ValueError) as exc:
                        return jsonify({"error": str(exc)}), 409
                move_item["unassigned"] = True
                move_item["unassigned_at"] = datetime.now(timezone.utc).isoformat()
                _write_move_history(move_history_path, move_history)
                if restored:
                    _relink_path_records([restored], image_decisions)
                    _relink_path_records([restored], face_markers)
                    key = _image_decision_key(str(original))
                    decision = image_decisions.get(key, decision)
            decision["previous_identity"] = decision.get("identity")
            decision["identity"] = ""
            decision["family"] = "review"
            decision["status"] = "pending"
            decision["notes"] = (str(decision.get("notes") or "") + " | unassigned")[:1000]
            decision["saved_at"] = datetime.now(timezone.utc).isoformat()
            _write_image_decisions(image_decisions_path, image_decisions)
            if key in face_markers:
                face_markers[key]["identity"] = ""
                face_markers[key]["family"] = "review"
                face_markers[key]["status"] = "pending"
                _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            _append_review_ledger(ledger_path, {
                "event": "image_unassign",
                "scope": "image",
                "path": raw_path,
                "identity": decision.get("previous_identity") or "",
                "status": "pending",
                "restored": restored,
                "saved_at": decision["saved_at"],
            })
        return jsonify({"path": raw_path, "unassigned": True, "restored": restored}), 200

    @app.post("/api/media/delete")
    def delete_review_media():
        """Permanently delete one reviewed media file with an explicit confirmation."""
        body = request.get_json(silent=True) or {}
        raw_path = str(body.get("path") or "")
        if not raw_path or body.get("confirm") is not True or str(body.get("confirm_path") or "") != raw_path:
            return jsonify({"error": "permanent deletion requires confirm=true and the exact confirm_path"}), 400
        if not is_known_media_path(raw_path):
            return jsonify({"error": "media path is outside the audit roots"}), 403
        requested = Path(_lexical_path(raw_path))
        if requested.suffix.lower() not in MEDIA_EXTENSIONS:
            return jsonify({"error": "unsupported media extension"}), 400
        try:
            if requested.is_symlink():
                return jsonify({"error": "symlink media cannot be deleted"}), 403
            resolved = requested.resolve(strict=True)
            if not resolved.is_file() or not is_under_allowed_root(resolved):
                return jsonify({"error": "media is not a regular file in an allowed review root"}), 403
            protected = set()
            for root in sorter.PROTECTED_SOURCE_ROOTS:
                try:
                    protected.add(root.resolve())
                except OSError:
                    continue
            if any(resolved == root or root in resolved.parents for root in protected):
                return jsonify({"error": "protected source media cannot be deleted"}), 403
            sha256 = sorter.file_sha256(resolved)
            size = resolved.stat().st_size
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"media is unavailable: {exc}"}), 404
        now = datetime.now(timezone.utc).isoformat()
        intent = {"event": "media_delete_started", "path": str(resolved), "sha256": sha256, "size": size, "request_id": g.request_id, "started_at": now}
        with write_lock:
            _append_review_ledger(ledger_path, intent)
            try:
                resolved.unlink()
            except OSError as exc:
                _append_review_ledger(ledger_path, {**intent, "event": "media_delete_failed", "error": str(exc), "finished_at": datetime.now(timezone.utc).isoformat()})
                return jsonify({"error": f"permanent delete failed: {exc}"}), 409
            lexical_paths = {_lexical_path(str(requested)), _lexical_path(str(resolved))}
            for mapping in (image_decisions, face_markers):
                for key, record in list(mapping.items()):
                    record_paths = {_lexical_path(str(record.get("path"))) if record.get("path") else "", _lexical_path(str(record.get("canonical_path"))) if record.get("canonical_path") else ""}
                    if lexical_paths & record_paths:
                        mapping.pop(key, None)
            _write_image_decisions(image_decisions_path, image_decisions)
            _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": now, "markers": list(face_markers.values())})
            for operation in move_history:
                for item in operation.get("moves", []):
                    if _lexical_path(str(item.get("destination") or "")) in lexical_paths or _lexical_path(str(item.get("source") or "")) in lexical_paths:
                        item["deleted"] = True
                        item["deleted_at"] = now
                        item["deleted_sha256"] = sha256
            _write_move_history(move_history_path, move_history)
            _append_review_ledger(ledger_path, {"event": "media_deleted", "path": str(resolved), "sha256": sha256, "size": size, "request_id": g.request_id, "finished_at": datetime.now(timezone.utc).isoformat()})
            deleted_media_path_keys.add(_image_decision_key(str(resolved)))
        return jsonify({"deleted": True, "path": str(resolved), "sha256": sha256, "size": size}), 200

    @app.get("/api/moves")
    def list_move_operations():
        operations = list(move_history)
        queued = list_durable_assignment_queue(evidence_db_path, statuses=("applied", "undone"))
        for assignment in queued:
            provenance = assignment.get("provenance")
            applied_move = provenance.get("applied_move") if isinstance(provenance, dict) else None
            if not isinstance(applied_move, dict):
                continue
            operations.append({
                "id": f"assignment-{assignment['assignment_id']}",
                "assignment_id": assignment["assignment_id"],
                "identity": applied_move.get("identity") or assignment.get("identity"),
                "saved_at": assignment.get("applied_at") or assignment.get("created_at"),
                "moves": applied_move.get("moves", []),
                "undone": assignment.get("status") == "undone",
            })
        operations.sort(key=lambda item: str(item.get("saved_at") or ""))
        return jsonify({"operations": operations[-100:]})

    @app.post("/api/moves/<move_id>/undo")
    def undo_move_operation(move_id: str):
        operation = next((item for item in reversed(move_history) if item.get("id") == move_id), None)
        queue_assignment_id: Optional[int] = None
        applied_move: Dict[str, Any] = {}
        if operation is None and move_id.startswith("assignment-"):
            try:
                queue_assignment_id = int(move_id.removeprefix("assignment-"))
            except ValueError:
                return jsonify({"error": "move operation not found"}), 404
            assignment = next((item for item in list_durable_assignment_queue(evidence_db_path, statuses=("applied", "undone")) if int(item.get("assignment_id") or 0) == queue_assignment_id), None)
            if assignment is None:
                return jsonify({"error": "applied assignment move not found"}), 404
            if assignment.get("status") == "undone":
                return jsonify({"error": "assignment move is already undone"}), 409
            provenance = assignment.get("provenance")
            applied_move = provenance.get("applied_move") if isinstance(provenance, dict) else None
            if not isinstance(applied_move, dict):
                return jsonify({"error": "assignment has no reversible move record"}), 409
            operation = {
                "id": move_id,
                "identity": assignment.get("identity"),
                "moves": applied_move.get("moves", []),
                "undone": False,
            }
        if operation is None:
            return jsonify({"error": "move operation not found"}), 404
        if operation.get("undone"):
            return jsonify({"error": "move operation is already undone"}), 409
        restored: List[Dict[str, str]] = []
        restored_paths: List[str] = []
        errors: List[Dict[str, str]] = []
        with write_lock:
            for item in operation.get("moves", []):
                if item.get("unassigned") or item.get("undone") or item.get("deleted"):
                    continue
                source = Path(str(item.get("destination") or "")).resolve()
                target = Path(str(item.get("source") or "")).resolve()
                try:
                    if not source.is_file():
                        raise FileNotFoundError("moved file is missing")
                    if not (source == review_dest_root or review_dest_root in source.parents):
                        raise ValueError("destination is outside the review tree")
                    if not is_under_allowed_root(target):
                        raise ValueError("original path is outside the audit roots")
                    expected_sha256 = str(applied_move.get("expected_sha256") or "")
                    if queue_assignment_id is not None and expected_sha256 and sorter.file_sha256(source) != expected_sha256:
                        raise ValueError("moved file fingerprint changed; refusing undo")
                    if target.exists():
                        raise FileExistsError("original path already exists")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                    restored.append({"source": str(source), "destination": str(target)})
                    item["undone"] = True
                    _relink_path_records([restored[-1]], image_decisions)
                    _relink_path_records([restored[-1]], face_markers)
                    key = _image_decision_key(str(target))
                    if key in image_decisions:
                        image_decisions[key]["status"] = "pending"
                        image_decisions[key]["notes"] = (str(image_decisions[key].get("notes") or "") + " | assignment undone")[:1000]
                    if key in face_markers:
                        face_markers[key]["status"] = "pending"
                except (OSError, ValueError) as exc:
                    errors.append({"source": str(source), "error": str(exc)})
            if restored:
                _write_image_decisions(image_decisions_path, image_decisions)
                _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            elif queue_assignment_id is not None and not operation.get("moves"):
                target = Path(str(applied_move.get("path") or "")).resolve()
                if not (is_under_allowed_root(target) or target == review_dest_root or review_dest_root in target.parents) or not target.is_file():
                    errors.append({"source": str(target), "error": "assigned file is missing or outside the audit roots"})
                else:
                    expected_sha256 = str(applied_move.get("expected_sha256") or "")
                    try:
                        if expected_sha256 and sorter.file_sha256(target) != expected_sha256:
                            raise ValueError("assigned file fingerprint changed; refusing undo")
                        key = _image_decision_key(str(target))
                        if key in image_decisions:
                            image_decisions[key]["status"] = "pending"
                            image_decisions[key]["notes"] = (str(image_decisions[key].get("notes") or "") + " | assignment undone")[:1000]
                        if key in face_markers:
                            face_markers[key]["status"] = "pending"
                        _write_image_decisions(image_decisions_path, image_decisions)
                        _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
                        restored_paths.append(str(target))
                    except (OSError, ValueError) as exc:
                        errors.append({"source": str(target), "error": str(exc)})
            operation["undone"] = not errors and not any(not item.get("unassigned") and not item.get("undone") and not item.get("deleted") for item in operation.get("moves", []))
            operation["undone_at"] = datetime.now(timezone.utc).isoformat()
            operation["undo_errors"] = errors
            if queue_assignment_id is None:
                _write_move_history(move_history_path, move_history)
            elif operation["undone"]:
                update_durable_assignment_status(evidence_db_path, queue_assignment_id, "undone")
            _append_review_ledger(ledger_path, {
                "event": "move_undo",
                "move_id": move_id,
                "assignment_id": queue_assignment_id,
                "restored": restored,
                "errors": errors,
                "saved_at": operation["undone_at"],
            })
        restored_paths.extend(str(item.get("destination")) for item in restored if item.get("destination"))
        return jsonify({"move_id": move_id, "restored": restored, "restored_paths": restored_paths, "errors": errors, "undone": operation["undone"]}), 200 if not errors else 409

    def latest_undo_action() -> Dict[str, Any] | None:
        move_payload = list_move_operations().get_json() or {}
        candidates = []
        for operation in move_payload.get("operations", []):
            if operation.get("undone"):
                continue
            moves = operation.get("moves") or []
            if not moves and not operation.get("assignment_id"):
                continue
            candidates.append({
                "kind": "move",
                "id": str(operation.get("id") or ""),
                "identity": str(operation.get("identity") or ""),
                "saved_at": str(operation.get("saved_at") or ""),
                "count": len(moves) or 1,
            })
        candidates.extend(
            {
                "kind": "group",
                "id": str(action.get("id") or ""),
                "identity": str(action.get("name") or ""),
                "saved_at": str(action.get("saved_at") or ""),
                "count": len(action.get("paths") or []),
            }
            for action in _read_manual_group_actions(manual_group_actions_path)
            if not action.get("undone") and action.get("id")
        )
        return max(candidates, key=lambda item: item["saved_at"], default=None)

    @app.get("/api/undo/latest")
    def get_latest_undo_action():
        action = latest_undo_action()
        return jsonify({"available": action is not None, "action": action})

    @app.post("/api/undo/latest")
    def undo_latest_action():
        action = latest_undo_action()
        if action is None:
            return jsonify({"error": "no reversible assignment or group action"}), 404
        if action["kind"] == "move":
            response = undo_move_operation(action["id"])
            if response.status_code >= 400:
                return response
            return jsonify({**(response.get_json() or {}), "kind": "move", "identity": action["identity"]}), response.status_code

        now = datetime.now(timezone.utc).isoformat()
        action_id = action["id"]
        group_action = None
        removed_paths: List[str] = []
        with write_lock:
            actions = _read_manual_group_actions(manual_group_actions_path)
            group_action = next((item for item in reversed(actions) if item.get("id") == action_id), None)
            if group_action is None or group_action.get("undone"):
                return jsonify({"error": "group assignment was already undone"}), 409
            latest = _read_manual_groups(manual_groups_path)
            group_key = str(group_action.get("key") or sorter.normalize_key(group_action.get("name") or ""))
            group = latest.get(group_key)
            if group is None:
                return jsonify({"error": "manual collection no longer exists"}), 409
            target_keys = {_lexical_path(str(path)) for path in group_action.get("paths", [])}
            current_paths = list(group.get("paths", []))
            removed_paths = [path for path in current_paths if _lexical_path(str(path)) in target_keys]
            group["paths"] = [path for path in current_paths if _lexical_path(str(path)) not in target_keys]
            group["updated_at"] = now
            latest[group_key] = group
            _atomic_json_write(manual_groups_path, {"schema_version": 1, "updated_at": now, "groups": list(latest.values())})
            group_action["undone"] = True
            group_action["undone_at"] = now
            _write_manual_group_actions(manual_group_actions_path, actions)
            manual_groups.clear()
            manual_groups.update(latest)
        _append_review_ledger(ledger_path, {
            "event": "manual_group_undo",
            "undo_id": action_id,
            "name": group_action.get("name"),
            "removed_paths": removed_paths,
            "saved_at": now,
        })
        return jsonify({
            "kind": "group",
            "undo_id": action_id,
            "identity": group_action.get("name"),
            "removed_paths": removed_paths,
            "undone": True,
        })

    @app.get("/api/face-markers")
    def face_markers_summary():
        confirmed = [item for item in face_markers.values() if item.get("status") == "confirmed"]
        return jsonify({"markers": len(face_markers), "confirmed": len(confirmed), "path": str(face_markers_path)})

    @app.get("/api/decisions")
    def list_decisions():
        counts = {status: sum(item.get("status", "pending") == status for item in decisions.values()) for status in DECISION_STATUSES}
        return jsonify({"counts": counts, "decisions": list(decisions.values())})

    @app.delete("/api/decisions/<cluster_id>")
    def delete_decision(cluster_id: str):
        if cluster_id not in decisions:
            return jsonify({"error": "decision not found"}), 404
        with write_lock:
            del decisions[cluster_id]
            _write_decisions(decisions_path, decisions)
        return jsonify({"deleted": cluster_id})

    @app.get("/api/export-preview")
    def export_preview():
        promotable = [item for item in decisions.values() if item.get("status") == "confirmed" and item.get("family") in (FAMILIES - {"review"})]
        skipped = [item for item in decisions.values() if item not in promotable]
        return jsonify({"promotable": promotable, "skipped": skipped})

    @app.post("/api/decisions")
    def save_decision():
        body = request.get_json(silent=True) or {}
        cluster_id = str(body.get("cluster_id") or "")
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        if cluster_id not in by_id:
            return jsonify({"error": "unknown cluster"}), 404
        if not _valid_identity(identity):
            return jsonify({"error": "identity is required and must be a safe folder name"}), 400
        if family not in FAMILIES:
            return jsonify({"error": "invalid family"}), 400
        status = str(body.get("status") or "pending").strip()
        if status not in DECISION_STATUSES:
            return jsonify({"error": "invalid decision status"}), 400
        purity_flags = _cluster_purity_flags(by_id[cluster_id])
        if status == "confirmed" and purity_flags and not bool(body.get("purity_ack")):
            return jsonify({
                "error": "cluster requires image-level purity review before bulk confirmation",
                "purity_flags": purity_flags,
                "sample_paths": by_id[cluster_id]["sample_paths"],
            }), 409
        decision = {
            "cluster_id": cluster_id,
            "identity": identity,
            "family": family,
            "status": status,
            "aliases": [str(alias).strip() for alias in body.get("aliases", []) if str(alias).strip()][:20],
            "notes": str(body.get("notes") or "").strip()[:1000],
            "sample_paths": by_id[cluster_id]["sample_paths"],
            "count": by_id[cluster_id]["count"],
            "purity_flags": purity_flags,
            "purity_ack": bool(body.get("purity_ack")),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with write_lock:
            decisions[cluster_id] = decision
            _write_decisions(decisions_path, decisions)
            move_result = {"moved": [], "errors": []}
            _append_review_ledger(ledger_path, {
                "event": "cluster_decision",
                "scope": "cluster",
                "cluster_id": cluster_id,
                "paths": by_id[cluster_id]["paths"],
                "identity": identity,
                "family": family,
                "status": status,
                "purity_flags": purity_flags,
                "saved_at": decision["saved_at"],
            })
            if status == "confirmed":
                # Keep a per-image ledger in sync with the cluster decision so
                # thumbnail actions (Unassign/Reassign) remain available after
                # a whole-cluster approval.  Capture these source paths before
                # the confirmed move removes them from the audit root.
                saved_at = decision["saved_at"]
                for path in by_id[cluster_id]["paths"]:
                    key = _image_decision_key(path)
                    image_decisions[key] = {
                        "key": key,
                        "scope": "image",
                        "path": path,
                        "identity": identity,
                        "family": family,
                        "status": status,
                        "notes": decision["notes"],
                        "saved_at": saved_at,
                    }
                _write_image_decisions(image_decisions_path, image_decisions)
                # Preserve the source hash before moving files out of the audit
                # root so future marker/embedding joins remain fingerprinted.
                _record_face_markers(
                    face_markers_path, face_markers, by_id[cluster_id]["paths"], identity, family, status, persist=False
                )
                move_result = move_review_paths(
                    by_id[cluster_id]["paths"], identity, family, allowed_roots, DEFAULT_REVIEW_DEST_ROOT
                )
                moved_sources = {str(item.get("source")) for item in move_result.get("moved", [])}
                for path in by_id[cluster_id]["paths"]:
                    if path not in moved_sources:
                        image_decisions[_image_decision_key(path)]["status"] = "needs-evidence"
                        face_markers.pop(_image_decision_key(path), None)
                if move_result["moved"]:
                    _relink_path_records(move_result["moved"], image_decisions, preserve_source_path=True)
                    _relink_path_records(move_result["moved"], face_markers)
                    _write_image_decisions(image_decisions_path, image_decisions)
                    _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
                else:
                    _write_image_decisions(image_decisions_path, image_decisions)
                    _atomic_json_write(face_markers_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(face_markers.values())})
            move_id = _record_move_operation(move_history_path, move_history, move_result, identity, family)
            _record_move_audit(ledger_path, move_id, move_result, identity, family)
        return jsonify({**decision, **move_result, "move_id": move_id}), 201

    @app.post("/api/decisions/bulk")
    def save_bulk_decisions():
        body = request.get_json(silent=True) or {}
        cluster_ids = [str(item) for item in body.get("cluster_ids", [])]
        unknown = [item for item in cluster_ids if item not in by_id]
        if unknown or not cluster_ids:
            return jsonify({"error": "cluster_ids must identify existing clusters"}), 400
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        status = str(body.get("status") or "pending").strip()
        if not _valid_identity(identity) or family not in FAMILIES or status not in DECISION_STATUSES:
            return jsonify({"error": "identity, family, or status is invalid"}), 400
        for cluster_id in cluster_ids:
            decisions[cluster_id] = {
                "cluster_id": cluster_id,
                "identity": identity,
                "family": family,
                "status": status,
                "aliases": [],
                "notes": str(body.get("notes") or "").strip()[:1000],
                "sample_paths": by_id[cluster_id]["sample_paths"],
                "count": by_id[cluster_id]["count"],
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        with write_lock:
            _write_decisions(decisions_path, decisions)
            for cluster_id in cluster_ids:
                _append_review_ledger(ledger_path, {
                    "event": "bulk_cluster_decision",
                    "scope": "cluster",
                    "cluster_id": cluster_id,
                    "paths": by_id[cluster_id]["paths"],
                    "identity": identity,
                    "family": family,
                    "status": status,
                    "saved_at": decisions[cluster_id]["saved_at"],
                })
        return jsonify({"saved": len(cluster_ids), "cluster_ids": cluster_ids}), 201

    @app.get("/api/identity-preview/<identity_name>")
    def identity_preview(identity_name: str):
        if not _valid_identity(identity_name):
            return jsonify({"error": "invalid identity"}), 400
        catalog, alias_index, _, _, preferred_targets = sorter.load_identity_catalog()
        catalog_by_key = {sorter.normalize_key(item.canonical): item for item in catalog}
        key = sorter.normalize_key(identity_name)
        target_key = preferred_targets.get(key, key)
        identity = catalog_by_key.get(target_key)
        if identity is None:
            matches = [item for item in catalog if any(sorter.normalize_key(alias) == key for alias in item.aliases)]
            if len(matches) == 1:
                identity = matches[0]
        if identity is None:
            return jsonify({"error": "identity is not in the registry"}), 404
        resolved_aliases = {identity.canonical}
        canonical_key = sorter.normalize_key(identity.canonical)
        for alias in identity.aliases:
            alias_key = sorter.normalize_key(alias)
            alias_owners = alias_index.get(alias_key, set())
            preferred = preferred_targets.get(alias_key)
            if (len(alias_owners) == 1 and next(iter(alias_owners)).canonical == identity.canonical) or preferred == canonical_key:
                if _valid_identity(alias):
                    resolved_aliases.add(alias)
        directories = {sorter.destination_for(identity, DEFAULT_REVIEW_DEST_ROOT)}
        family_dirs = FAMILIES | {"redditdaily", "linked"}
        for alias in resolved_aliases:
            if identity.family == "linked":
                directories.add(DEFAULT_REVIEW_DEST_ROOT / alias)
            for family in family_dirs:
                directories.add(DEFAULT_REVIEW_DEST_ROOT / family / alias)
        try:
            paths = set()
            image_extensions = MEDIA_EXTENSIONS - {".mp4", ".mov", ".webm", ".m4v"}
            for identity_dir in sorted(directories, key=lambda path: str(path).casefold()):
                if identity_dir.is_symlink() or not identity_dir.is_dir():
                    continue
                for path in iter_media_files(identity_dir):
                    if path.suffix.lower() in image_extensions and is_known_media_path(str(path)):
                        paths.add(str(path))
        except OSError as exc:
            LOGGER.warning("identity preview scan failed for %s (%s)", identity.canonical, exc)
            return jsonify({"error": "identity preview is temporarily unavailable"}), 503
        sorted_paths = sorted(paths, key=str.casefold)
        return jsonify({"identity": identity.canonical, "count": len(sorted_paths), "paths": sorted_paths[:36]}), 200

    @app.get("/media")
    def media():
        raw_path = str(request.args.get("path") or "")
        if not is_known_media_path(raw_path):
            return jsonify({"error": "media path is outside the audit roots"}), 403
        path = Path(_lexical_path(raw_path))
        try:
            if path.is_symlink():
                return jsonify({"error": "symlink media is not served"}), 403
            valid_file = path.suffix.lower() in MEDIA_EXTENSIONS and path.is_file()
        except OSError as exc:
            LOGGER.warning("media unavailable: %s (%s)", raw_path, exc)
            return jsonify({"error": "media temporarily unavailable"}), 503
        if not valid_file:
            return jsonify({"error": "media not found"}), 404
        try:
            return send_file(path, mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        except OSError as exc:
            LOGGER.warning("media read failed: %s (%s)", raw_path, exc)
            return jsonify({"error": "media temporarily unavailable"}), 503

    return app


HTML_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Picorg candidate review</title>
<style>
body{font:14px system-ui;margin:0;color:#202124;background:#f6f7f9}main{display:grid;grid-template-columns:330px 1fr;min-height:100vh}.side{background:#20252b;color:#f5f7fa;padding:18px;overflow:auto}.side h1{font-size:20px}.cluster-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}.cluster{display:grid;grid-template-columns:88px 1fr;gap:10px;width:100%;text-align:left;background:#2d343c;color:inherit;border:1px solid #46505a;border-radius:8px;padding:8px;margin:0;cursor:pointer}.cluster.active{border-color:#7cc4ff}.cluster-thumb{width:88px;height:88px;object-fit:cover;border-radius:5px;background:#111}.cluster-body{min-width:0}.cluster-title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.cluster small{display:block;color:#b7c0ca;margin-top:3px}.cluster-actions{display:flex;gap:6px;margin-top:8px}.cluster-actions button{padding:5px 7px;font-size:12px}.approve{background:#1f8a55;color:#fff;border:0;border-radius:4px}.reject{background:#a84141;color:#fff;border:0;border-radius:4px}.detail{padding:24px;max-width:1100px}.meta{background:white;padding:12px;border-radius:8px;margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}.grid img,.grid video{width:100%;height:160px;object-fit:cover;background:#ddd;border-radius:6px}.grid a{cursor:zoom-in}.imageSelect{width:22px;height:22px;accent-color:#176b87;cursor:pointer;align-self:start;justify-self:center;margin:4px}.form{background:white;padding:16px;border-radius:8px;margin-top:16px;display:grid;gap:9px;max-width:650px}input,select,textarea,button{font:inherit;padding:8px}button{cursor:pointer}button:disabled{cursor:wait;opacity:.6}:focus-visible{outline:3px solid #7cc4ff;outline-offset:2px}.status{min-height:22px;color:#176b37}.muted{color:#68737d}.media-modal{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(0,0,0,.86)}.media-modal[hidden]{display:none}.media-modal-content{max-width:95vw;max-height:90vh}.media-modal-content img,.media-modal-content video{display:block;max-width:95vw;max-height:85vh;object-fit:contain}.media-modal-close{position:absolute;top:12px;right:18px;border:0;border-radius:6px;background:#fff;color:#111;font-size:24px;line-height:1;padding:6px 12px}.media-modal-caption{color:#fff;max-width:95vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:8px}@media(max-width:700px){main{display:block}.side{position:static}.detail{padding:14px}.cluster-grid{grid-template-columns:1fr}.detail{padding:14px}.grid{grid-template-columns:repeat(auto-fill,minmax(110px,1fr))}.grid img,.grid video{height:120px}}@media(min-width:701px){.side{grid-column:1}.cluster-grid{grid-template-columns:1fr}}
</style></head><body><main><aside class="side"><h1>Candidate clusters</h1><div id="summary" class="muted" aria-live="polite">Loading…</div><div id="accuracyTools" class="accuracy-tools"><b>Accuracy tools</b><button id="refreshIdentities" type="button" onclick="refreshIdentities()">Refresh identities</button><button id="runBenchmark" type="button" onclick="runAccuracyBenchmark()">Run image-level benchmark</button><pre id="accuracyStatus" class="muted" aria-live="polite">Not run</pre></div><label for="filter">Search clusters</label><input id="filter" placeholder="Filter clusters" oninput="loadPage(true)"><div id="list" aria-live="polite"></div><div><button id="nextPage" type="button" onclick="loadPage(false)">Next page</button></div></aside><section class="detail" aria-live="polite"><div id="detail"><h2>Select a cluster</h2><p class="muted">Review the images, then record an explicit identity decision.</p></div></section></main><div id="mediaModal" class="media-modal" hidden role="dialog" aria-modal="true" aria-label="Media preview"><button class="media-modal-close" type="button" aria-label="Close preview">×</button><div id="mediaModalContent" class="media-modal-content"></div></div>
<script>
let clusters=[], selected=null, manualGroupMembershipsByPath={};
let currentClusterImageDecisions={}, currentClusterAssignment=null, currentIdentityAssignment=null, lastMoveId=null, hideConfirmed=true;
let page=1, hasNext=false, clusterMode='face';
function openMediaModal(event, anchor){if(event.target.closest('.imageSelect'))return;event.preventDefault();let path=anchor.getAttribute('href');let mediaPath=new URL(path,location.href).searchParams.get('path')||path;let source=anchor.querySelector('img,video');let modal=document.querySelector('#mediaModal');let content=document.querySelector('#mediaModalContent');content.replaceChildren();let media=document.createElement(mediaPath.toLowerCase().match(/[.](mp4|mov)$/)?'video':'img');media.src=path;media.alt=source?.alt||'Full-size media preview';if(media.tagName==='VIDEO'){media.controls=true;media.autoplay=true;media.muted=true}content.append(media);let caption=document.createElement('div');caption.className='media-modal-caption';caption.textContent=mediaPath;content.append(caption);modal.hidden=false;document.querySelector('.media-modal-close').focus()}
function closeMediaModal(){let modal=document.querySelector('#mediaModal'),content=document.querySelector('#mediaModalContent');if(!modal||modal.hidden)return;modal.hidden=true;content?.replaceChildren()}
document.querySelector('#mediaModal').addEventListener('click',event=>{if(event.target.closest?.('.media-modal-close')){event.preventDefault();event.stopPropagation();closeMediaModal();return}if(event.target.id==='mediaModal')closeMediaModal()});
document.querySelector('#mediaModal').addEventListener('click',event=>{if(event.target.id==='mediaModal')closeMediaModal()});document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!document.querySelector('#mediaModal').hidden)closeMediaModal()});
function persistRecentChoice(kind,value,family='review'){let body=kind==='identity'?{kind,canonical:value,family}:{kind,name:value};fetch('/api/recent-choices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).catch(()=>{})}
function renderAccuracyStatus(data){let output=document.querySelector('#accuracyStatus');let button=document.querySelector('#runBenchmark');if(!output||!button)return;button.disabled=Boolean(data.running);button.textContent=data.running?'Benchmark running…':'Run image-level benchmark';let lines=(data.lines||[]).slice(-8);if(data.running)lines.unshift('Running review-only calibration…');else if(data.exit_code===0)lines.unshift('Completed successfully.');else if(data.exit_code!==null)lines.unshift('Failed (exit '+data.exit_code+').');output.textContent=lines.join('\n')||'Not run'}
async function loadAccuracyStatus(){try{let response=await fetch('/api/accuracy-benchmark',{headers:{Accept:'application/json'}});let data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);renderAccuracyStatus(data);if(data.running)window.setTimeout(loadAccuracyStatus,2000)}catch(error){let output=document.querySelector('#accuracyStatus');if(output)output.textContent='Unable to read benchmark status: '+error.message}}
async function runAccuracyBenchmark(){let button=document.querySelector('#runBenchmark');if(button)button.disabled=true;try{let response=await fetch('/api/accuracy-benchmark',{method:'POST',headers:{Accept:'application/json'}});let data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);renderAccuracyStatus(data);loadAccuracyStatus()}catch(error){if(button)button.disabled=false;let output=document.querySelector('#accuracyStatus');if(output)output.textContent='Unable to start benchmark: '+error.message}}
async function init(){let a=await fetch('/api/summary').then(x=>x.json());document.querySelector('#summary').textContent=`${a.clusters} clusters · ${a.decisions} saved decisions`;await loadPage(true)}
async function loadPage(reset){if(reset){page=1;clusters=[]}let q=encodeURIComponent(document.querySelector('#filter').value);let c=await fetch(`/api/clusters?page=${page}&page_size=50&mode=${clusterMode}&hide_confirmed=${hideConfirmed?'1':'0'}&q=${q}`).then(x=>x.json());clusters=reset?c.clusters:clusters.concat(c.clusters);hasNext=c.has_next;document.querySelector('#summary').textContent=`${c.total} face matching clusters · loaded ${clusters.length}`;renderList();if(reset&&clusters[0]){select(clusters[0].cluster_id);page=2}else if(!reset&&hasNext)page++}
function manualGroupBadgeMarkup(path){let groups=manualGroupMembershipsByPath[path]||[];return groups.length?`<span class="manual-group-badges" aria-label="Manual collections">${groups.map(name=>`<span class="manual-group-badge">${esc(name)}</span>`).join('')}</span>`:''}
function updateManualGroupMembership(path,name){if(!path||!name)return;let groups=manualGroupMembershipsByPath[path]||[];if(!groups.includes(name))manualGroupMembershipsByPath[path]=[...groups,name].sort((a,b)=>a.localeCompare(b));let tile=[...document.querySelectorAll('#detail .media-tile')].find(item=>item.querySelector('.imageSelect')?.dataset.path===path);let anchor=tile?.querySelector('a');if(anchor){anchor.querySelector('.manual-group-badges')?.remove();anchor.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path))}for(const thumb of [...document.querySelectorAll('.cluster-thumb-wrap')].filter(item=>item.dataset.path===path)){thumb.querySelector('.manual-group-badges')?.remove();thumb.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path))}let frame=document.querySelector('#mediaModalContent .media-preview-frame');if(frame&&modalPaths[modalIndex]===path){frame.querySelector('.manual-group-badges')?.remove();frame.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path))}}
function mediaTile(path,assignment){let item=assignment||{};let identity=item.identity||'';let family=item.family||'review';let state=item.status||'pending';let queueStatus=item.queued_status||'';let stateLabel=state==='queued'?'Queued · '+(queueStatus||'pending'):['error','conflict','rejected'].includes(state)?'Move '+state:state;if(hideConfirmed&&state==='confirmed')return '';let encodedPath=encodeURIComponent(path);let encodedIdentity=encodeURIComponent(identity);let encodedFamily=encodeURIComponent(family);let assignmentHtml=identity?`<div class="image-assignment"><span>Assigned: <b>${esc(identity)}</b></span><span class="image-state" role="status">${esc(stateLabel)}</span><button class="confirm-image" type="button" ${state==='confirmed'?'disabled':''} onclick="confirmImage(event,decodeURIComponent('${encodedPath}'),decodeURIComponent('${encodedIdentity}'),decodeURIComponent('${encodedFamily}'))">${state==='confirmed'?'Confirmed':state==='queued'?'Queued':'Confirm'}</button><details class="image-actions"><summary aria-label="More image actions">⋯</summary><menu><li><button type="button" onclick="unassignImage(event,decodeURIComponent('${encodedPath}'))">Unassign</button></li><li><button type="button" onclick="reassignImage(event,decodeURIComponent('${encodedPath}'))">Reassign</button></li></menu></details></div>`:'';let queuedOverlay=state==='queued'?`<span class="media-queued-overlay" aria-label="Assignment queued for ${esc(identity)}">Queued for ${esc(identity)}</span>`:'';return `<div class="media-tile"><label class="select-control"><input type="checkbox" class="imageSelect" data-path="${esc(path)}" aria-label="Select ${esc(path)}"><span>Select</span></label><a href="/media?path=${encodedPath}" onclick="openMediaModal(event,this)"><img src="/media?path=${encodedPath}" loading="lazy" title="${esc(path)}" alt="Preview of ${esc(path)}">${queuedOverlay}${manualGroupBadgeMarkup(path)}</a>${assignmentHtml}</div>`}
 function renderList(){let q=document.querySelector('#filter').value.toLowerCase();document.querySelector('#list').className='cluster-grid';document.querySelector('#list').innerHTML=clusters.filter(x=>x.identity_alias_match||(x.title+' '+x.expected_identities.join(' ')).toLowerCase().includes(q)).map(x=>{let path=x.sample_paths?.[0]||'';let thumb=path?`<div class="cluster-thumb-wrap" data-path="${esc(path)}"><img class="cluster-thumb" src="/media?path=${encodeURIComponent(path)}" alt="Thumbnail for ${esc(x.title)}" loading="lazy">${manualGroupBadgeMarkup(path)}</div>`:'<div class="cluster-thumb" aria-hidden="true"></div>';let status=x.decision?.status||'pending';return `<article class="cluster ${selected===x.cluster_id?'active':''}" role="button" tabindex="0" onclick="select('${x.cluster_id}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();select('${x.cluster_id}')}" aria-label="Open ${esc(x.title)}"><div>${thumb}</div><div class="cluster-body"><b class="cluster-title">${esc(x.title)}</b><small>${x.count} files · ${status}</small><div class="cluster-actions"><button type="button" class="approve" onclick="setClusterStatus(event,'${x.cluster_id}','approve')">Approve</button><button type="button" class="reject" onclick="setClusterStatus(event,'${x.cluster_id}','reject')">Reject</button></div></div></article>`}).join('')}
async function setClusterStatus(event,id,status){event.stopPropagation();let item=clusters.find(x=>x.cluster_id===id)||{};let endpoint='/api/clusters/'+id+'/status';let body={status};if(status==='approve'){let suggested=item.expected_identities?.[0]||item.title||'';let identity=window.prompt('Confirm the identity for this cluster:',suggested);if(!identity?.trim())return;let risky=(item.count||0)>=100||!item.expected_identities?.length||!(item.face_cluster_labels||[]).length||(item.face_cluster_labels||[]).length>1||(item.families||[]).length>1;if(risky&&!window.confirm('This cluster is large, name-only, or has mixed face/source evidence. Review individual images when people may be mixed. Continue?'))return;if(!window.confirm('Confirm '+identity.trim()+' for '+(item.count||0)+' file(s) and move them into the identity folder?'))return;endpoint='/api/decisions';body={cluster_id:id,identity:identity.trim(),family:item.family||'review',status:'confirmed',purity_ack:risky,aliases:[],notes:'Approved from cluster card after identity confirmation'}}let r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let d=await r.json();if(!r.ok){alert(d.error||'Unable to save review status');return}if(item)item.decision=d.decision||d;renderList();if(status==='approve')await select(id)}
let pendingClusterDetail=null;
async function select(id){if(!id)return;selected=id;renderList();let x=await fetch('/api/clusters/'+id,{headers:{Accept:'application/json'}}).then(async r=>{let data=await r.json();if(!r.ok)throw new Error(data.error||`Request failed (${r.status})`);return data});if(selected===id)pendingClusterDetail={id,data:x};manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(x.manual_groups_by_path||{})};currentClusterImageDecisions=x.image_decisions||{};currentClusterAssignment=x.decision||null;currentIdentityAssignment=null;let images=x.sample_paths.map(path=>mediaTile(path,currentClusterImageDecisions[path]||currentClusterAssignment)).join('');let sampleCount=x.sample_paths.length;let loadAll=(x.count||0)>sampleCount?`<div class="identity-toolbar"><button type="button" id="loadAllImages" onclick="loadClusterImages()">Load all ${x.count} images</button><span class="muted">Showing ${sampleCount} samples first</span></div>`:'';document.querySelector('#detail').innerHTML=`<h2>${esc(x.title)}</h2><div class="meta"><b>${x.count} files</b><br>Expected labels: ${esc(x.expected_identities.join(', ')||'none')}<br>Sources: ${esc(x.source_roots.join(', '))}</div>${loadAll}<div class="grid" data-loaded="sample">${images}</div><form class="form" onsubmit="save(event)"><label>Filter identities <input id="identityFilter" oninput="filterIdentityOptions()" placeholder="Type a name to narrow the list"></label><div id="recentIdentityList" class="recent-identity-shortlist" role="group" aria-label="Recently used identities"></div><label>Assign to identity <select id="identity" required><option value="">Loading identities…</option></select></label><label>Family <select id="family">${['linked','manual','metadaily','redditdaily','reddit_follow','reddit_subreddit','pscrape','review'].map(f=>`<option ${x.decision?.family===f?'selected':''}>${f}</option>`).join('')}</select></label><label>New identity <input id="newIdentity" value="" placeholder="Type only when creating a new identity"></label><label>Aliases, one per line<textarea id="aliases" placeholder="optional aliases">${esc((x.decision?.aliases||[]).join('\n'))}</textarea></label><label>Evidence / notes<textarea id="notes" placeholder="Why this assignment is supported">${esc(x.decision?.notes||'')}</textarea></label><button type="submit" class="approve">Save explicit decision</button><div class="status" id="status"></div></form>`;loadIdentityOptions(x.decision?.identity||'')}
let identityOptions=[];let selectedIdentityValue='';
const RECENT_IDENTITY_STORAGE_KEY='picorg.recent-identities.v1';
let recentIdentityUses=[];
try{let storedRecent=JSON.parse(localStorage.getItem(RECENT_IDENTITY_STORAGE_KEY)||'[]');if(Array.isArray(storedRecent))recentIdentityUses=storedRecent.filter(item=>item&&typeof item.canonical==='string'&&item.canonical.trim()).slice(0,20)}catch(_error){}
function identityOptionMatches(item,query){let value=item?.canonical||'';return Boolean(value&&(!query||value.toLocaleLowerCase().includes(query)||(item.aliases||[]).some(alias=>String(alias).toLocaleLowerCase().includes(query))))}
function matchingRecentIdentityOptions(query){let catalog=new Map(identityOptions.map(item=>[String(item.canonical||'').toLocaleLowerCase(),item]));return recentIdentityUses.map(item=>catalog.get(item.canonical.toLocaleLowerCase())||item).filter(item=>identityOptionMatches(item,query))}
function rememberIdentityUsed(canonical,family){let value=String(canonical||'').trim();if(!value)return;recentIdentityUses=[{canonical:value,family:family||'review'},...recentIdentityUses.filter(item=>item.canonical.toLocaleLowerCase()!==value.toLocaleLowerCase())].slice(0,20);try{localStorage.setItem(RECENT_IDENTITY_STORAGE_KEY,JSON.stringify(recentIdentityUses))}catch(_error){}persistRecentChoice('identity',value,family||'review');renderIdentityOptions();syncModalIdentityOptions()}
async function restoreRecentIdentityUses(){let local=[...recentIdentityUses];try{let response=await fetch('/api/recent-choices'),data=await response.json(),remote=Array.isArray(data.identities)?data.identities:[];if(remote.length){recentIdentityUses=remote}else if(local.length){recentIdentityUses=local;for(const item of local.slice().reverse())await fetch('/api/recent-choices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'identity',canonical:item.canonical,family:item.family||'review'})})}try{localStorage.setItem(RECENT_IDENTITY_STORAGE_KEY,JSON.stringify(recentIdentityUses))}catch(_error){}renderIdentityOptions();syncModalIdentityOptions()}catch(_error){}}
restoreRecentIdentityUses();
async function postNewIdentityWithPrompt(endpoint,body){let payload={...body};while(true){let response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await response.json();if(response.status!==409||!data.collision)return{response,data,payload};let current=payload.canonical||payload.identity||'',replacement=window.prompt(`${data.error||'That identity name already exists.'}\nEnter a different identity name:`,current);if(replacement===null)return{response,data,payload,cancelled:true};replacement=replacement.trim();if(!replacement){window.alert('Enter a different identity name to continue.');continue}if(Object.hasOwn(payload,'canonical'))payload.canonical=replacement;if(Object.hasOwn(payload,'identity'))payload.identity=replacement}}
async function loadIdentityOptions(selectedIdentity){let select=document.querySelector('#identity');selectedIdentityValue=selectedIdentity;try{identityOptions=await fetch('/api/identities?scope=canonical').then(r=>r.json());if(select)renderIdentityOptions();syncModalIdentityOptions()}catch(error){if(select)select.replaceChildren(new Option('Unable to load identities',''));throw error}}
function renderIdentityOptions(){let select=document.querySelector('#identity');if(!select)return;let filter=(document.querySelector('#identityFilter')?.value||'').trim().toLocaleLowerCase(),typed=(document.querySelector('#newIdentity')?.value||'').trim(),typedKey=typed.toLocaleLowerCase();let recent=matchingRecentIdentityOptions(filter);let exact=identityOptions.find(item=>item.canonical?.toLocaleLowerCase()===typedKey)||(!typed?identityOptions.find(item=>item.canonical?.toLocaleLowerCase()===filter):null);let recentKeys=new Set(recent.map(item=>String(item.canonical).toLocaleLowerCase()));if(exact)recentKeys.add(exact.canonical.toLocaleLowerCase());let grouped={};for(let item of identityOptions){let value=item.canonical||'';if(identityOptionMatches(item,filter)&&!recentKeys.has(value.toLocaleLowerCase()))(grouped[item.family||'other']??=[]).push(value)}select.replaceChildren(new Option('Choose an identity',''));if(typed){let value=exact?.canonical||typed;select.append(new Option(exact?value:`Create new identity: ${typed}`,value,true,true))}else if(exact)select.append(new Option(exact.canonical,exact.canonical,exact.canonical===selectedIdentityValue,exact.canonical===selectedIdentityValue));if(recent.length){let group=document.createElement('optgroup');group.label='Recently used';for(let item of recent){let value=item.canonical;group.append(new Option(value,value,value===selectedIdentityValue,value===selectedIdentityValue))}select.append(group)}let order=['manual','metadaily','redditdaily','reddit_follow','reddit_subreddit','pscrape','review','other'];order=[...new Set([...order,...Object.keys(grouped).sort()])];for(let family of order){let values=[...new Set(grouped[family]||[])].sort((a,b)=>a.localeCompare(b));if(!values.length)continue;let group=document.createElement('optgroup');group.label=family==='reddit_subreddit'?'Subreddits':family==='reddit_follow'?'Reddit accounts':family==='redditdaily'?'Redditdaily':family==='metadaily'?'Metadaily':family[0].toUpperCase()+family.slice(1);for(let value of values)group.append(new Option(value,value,value===selectedIdentityValue,value===selectedIdentityValue));select.append(group)}}
function renderRecentIdentityShortlist(){let list=document.querySelector('#recentIdentityList');if(!list)return;let query=(document.querySelector('#identityFilter')?.value||'').trim().toLocaleLowerCase(),options=matchingRecentIdentityOptions(query),selected=document.querySelector('#identity')?.value||'';list.replaceChildren();list.hidden=!options.length;if(!options.length)return;let label=document.createElement('span');label.textContent='Recently used';list.append(label);for(let item of options){let button=document.createElement('button');button.type='button';button.textContent=item.canonical;button.title=`Use ${item.canonical} as the target identity`;button.setAttribute('aria-pressed',String(item.canonical.toLocaleLowerCase()===selected.toLocaleLowerCase()));button.onclick=()=>{let picker=document.querySelector('#identity'),family=document.querySelector('#family'),typed=document.querySelector('#newIdentity');if(typed)typed.value='';renderIdentityOptions();if(picker&&!Array.from(picker.options).some(option=>option.value===item.canonical))picker.add(new Option(item.canonical,item.canonical));if(picker)picker.value=item.canonical;if(family&&Array.from(family.options).some(option=>option.value===item.family))family.value=item.family;selectedIdentityValue=item.canonical;renderRecentIdentityShortlist()};list.append(button)}}
const renderIdentityOptionsWithRecentShortlist=renderIdentityOptions;renderIdentityOptions=function(){renderIdentityOptionsWithRecentShortlist();renderRecentIdentityShortlist()};
function filterIdentityOptions(){renderIdentityOptions()}
document.addEventListener('input',event=>{if(event.target?.id==='newIdentity')renderIdentityOptions()});document.addEventListener('change',event=>{if(event.target?.id==='identity')selectedIdentityValue=event.target.value});
async function save(e){e.preventDefault();let chosenIdentity=document.querySelector('#newIdentity')?.value.trim()||identity.value.trim();if(!chosenIdentity){status.textContent='Choose an identity or enter a new identity name';return}let r=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cluster_id:selected,identity:chosenIdentity,family:family.value,aliases:aliases.value.split('\n'),notes:notes.value})});let d=await r.json();status.textContent=r.ok?'Saved decision for '+d.count+' files':d.error;if(r.ok){let x=clusters.find(x=>x.cluster_id===selected);x.decision=d;renderList()}}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function save(e){e.preventDefault();let chosenIdentity=document.querySelector('#newIdentity')?.value.trim()||identity.value.trim();if(!chosenIdentity){status.textContent='Choose an identity or enter a new identity name';return}let r=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cluster_id:selected,identity:chosenIdentity,family:family.value,status:(document.querySelector('#decisionStatus')||{value:'pending'}).value,aliases:aliases.value.split('\\n'),notes:notes.value})});let d=await r.json();status.textContent=r.ok?'Saved decision for '+d.count+' files':d.error;if(r.ok){rememberIdentityUsed(chosenIdentity,family.value);let x=clusters.find(x=>x.cluster_id===selected);x.decision=d;renderList()}}
const statusObserver=new MutationObserver(()=>{let form=document.querySelector('.form');if(form&&!document.querySelector('#decisionStatus')){let label=document.createElement('label');label.textContent='Status ';let select=document.createElement('select');select.id='decisionStatus';['pending','needs-evidence','confirmed','rejected'].forEach(v=>{let option=document.createElement('option');option.value=v;option.textContent=v;select.appendChild(option)});label.appendChild(select);form.insertBefore(label,form.children[1])}});
statusObserver.observe(document.querySelector('#detail'),{childList:true});
const memberObserver=new MutationObserver(()=>{let grid=document.querySelector('#detail .grid');if(!grid||document.querySelector('#memberTools'))return;let tools=document.createElement('div');tools.id='memberTools';tools.className='meta';tools.innerHTML='<b>Cluster membership</b><br><select id="targetCluster">'+clusters.map(c=>'<option value="'+c.cluster_id+'">'+esc(c.title)+' ('+c.count+')</option>').join('')+'</select><button onclick="updateMember(\'add\')">Move selected image</button><button onclick="updateMember(\'remove\')">Remove selected image</button><p class="muted">Click an image to preview it; use the checkbox to select it for assignment.</p>';document.querySelector('#detail').insertBefore(tools,grid);grid.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{window.memberPath=new URL(a.href).searchParams.get('path');document.querySelector('#memberTools p').textContent='Selected: '+window.memberPath}));});
memberObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
const imageAssignObserver=new MutationObserver(()=>{let grid=document.querySelector('#detail .grid');if(!grid||document.querySelector('#imageAssignTools'))return;let tools=document.createElement('div');tools.id='imageAssignTools';tools.className='meta';tools.innerHTML='<b>Selected image assignment</b><div class="cluster-selection-actions" role="group" aria-label="Select cluster images"><button type="button" onclick="selectAllImages()">Select all</button><button type="button" onclick="selectNoImages()">Select none</button><button type="button" id="hideConfirmedLabel" onclick="toggleHideConfirmed()">Hide confirmed</button></div><div class="cluster-primary-actions" role="group" aria-label="Assign selected images"><button type="button" class="approve" onclick="assignSelectedImages()">Assign selected and move</button><button type="button" class="undo-action" onclick="undoLastMove()">Undo last action</button></div><details class="cluster-more-actions"><summary>More controls</summary><button type="button" onclick="createIdentity()">Save typed identity as new</button></details><p class="muted">Confirmed assignments can be hidden from the grid; assignments still remain stored.</p>';document.querySelector('#detail').insertBefore(tools,grid);window.setTimeout(updateAssignmentLabels,0)});
imageAssignObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
const mediaObserver=new MutationObserver(()=>{document.querySelectorAll('#detail .grid a').forEach(a=>{let path=new URL(a.href).searchParams.get('path')||'';let node=a.querySelector('img');let isVideo=path.toLowerCase().endsWith('.mp4')||path.toLowerCase().endsWith('.mov');let hideUnavailable=media=>{media.onerror=()=>{media.closest('.media-tile')?.remove()}};if(isVideo&&node&&!a.querySelector('video')){let video=document.createElement('video');video.controls=true;video.preload='metadata';video.muted=true;video.setAttribute('aria-label','Video preview');video.src=a.href;hideUnavailable(video);node.replaceWith(video)}if(node&&!node.dataset.errorBound){node.dataset.errorBound='1';node.alt='Preview of '+path;hideUnavailable(node)}})});
mediaObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
function toggleHideConfirmed(){hideConfirmed=!hideConfirmed;let label=document.querySelector('#hideConfirmedLabel');if(label)label.textContent=hideConfirmed?'Show confirmed':'Hide confirmed';if(viewMode==='identities'&&selectedIdentity)selectIdentity(selectedIdentity);else if(selected)select(selected)}
function selectAllImages(){document.querySelectorAll('#detail .imageSelect').forEach(input=>{input.checked=true})}
function selectNoImages(){document.querySelectorAll('#detail .imageSelect').forEach(input=>{input.checked=false})}
document.addEventListener('keydown',event=>{if(['INPUT','TEXTAREA','SELECT','BUTTON'].includes(event.target.tagName))return;let input=document.querySelector('#detail .imageSelect:checked');if(event.key===' '){event.preventDefault();if(input)input.checked=false;else document.querySelector('#detail .imageSelect')?.click();return}if(!input)return;let path=input.dataset.path;let tile=input.closest('.media-tile');if(event.key.toLowerCase()==='c')tile?.querySelector('.confirm-image')?.click();if(event.key.toLowerCase()==='r')reassignImage(event,path);if(event.key.toLowerCase()==='u')unassignImage(event,path)});
async function confirmImage(event,path,identity,family){event.stopPropagation();let button=event.currentTarget;button.disabled=true;let r=await fetch(assignmentEndpoint(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[path],identity,family,status:'confirmed',notes:'Confirmed from cluster thumbnail'})});let d=await r.json();if(!r.ok){button.disabled=false;alert(d.error||'Image confirmation failed');return}let queued=Number(d.queued||0)>0,moved=(d.moved||[]).some(item=>item?.source===path);if(!queued&&!moved){let detail=(d.errors||[]).map(item=>item?.error||item?.message||String(item)).join('; ');currentClusterImageDecisions[path]={identity,family,status:'needs-evidence',error:detail};button.disabled=false;button.textContent='Move failed';button.title=detail||'Assignment saved, but the image was not moved';showQueuedMoveStatus(`Assignment saved, but the image was not moved${detail?`: ${detail}`:''}`,true);return {...d,needs_review:true}}rememberIdentityUsed(identity,family);lastMoveId=d.move_id||lastMoveId;button.textContent=queued?'Queued':'Confirmed';button.title=queued?'Queued for later application':'Confirmed; moved '+(d.moved||[]).length+' image(s)';currentClusterImageDecisions[path]={identity,family,status:queued?'queued':'confirmed'};showUndoOption(queued?'Assignment queued':'Assigned '+identity+'; moved '+(d.moved||[]).length+' image(s)',d.move_id);return d}
async function unassignImage(event,path){event.stopPropagation();if(!confirm('Unassign this image and return it to its previous location?'))return;let r=await fetch('/api/image-decisions/unassign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});let d=await r.json();if(!r.ok){alert(d.error||'Unassign failed');return}let tile=event.currentTarget.closest('.media-tile');tile?.querySelector('.image-assignment')?.remove();let input=tile?.querySelector('.imageSelect');if(input)input.checked=false;delete currentClusterImageDecisions[path];}
function reassignImage(event,path){event.stopPropagation();let input=[...document.querySelectorAll('#detail .imageSelect')].find(item=>item.dataset.path===path);if(input)input.checked=true;let identityPicker=document.querySelector('#identity');if(identityPicker){identityPicker.focus();identityPicker.scrollIntoView({block:'center'})}let status=document.querySelector('#imageAssignTools p');if(status)status.textContent='Selected '+path+'; choose a new identity, then assign selected.';}
async function assignSelectedImages(){let paths=[...document.querySelectorAll('#detail .imageSelect:checked')].map(x=>x.dataset.path);let typedIdentity=document.querySelector('#newIdentity')?.value.trim()||'';let identity=typedIdentity||document.querySelector('#identity')?.value.trim()||'';let family=document.querySelector('#family')?.value||'review';if(!paths.length||!identity){alert('Select images and choose an identity or enter a new identity name');return}let r=await fetch(assignmentEndpoint(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths,identity,family,status:'confirmed',notes:document.querySelector('#notes')?.value||'Assigned from cluster'})});let d=await r.json();if(!r.ok){alert(d.error||'Image assignment failed');return}rememberIdentityUsed(identity,family);lastMoveId=d.move_id||lastMoveId;showUndoOption(d.queued?'Queued '+d.queued+' assignment(s) for after the rebuild':'Assigned and confirmed '+d.saved+'; moved '+(d.moved||[]).length+' image(s) to '+d.identity+((d.errors||[]).length?' ('+d.errors.length+' move error(s))':''),d.move_id);return {...d,paths}}
async function createIdentity(){let identity=document.querySelector('#newIdentity')?.value.trim();let paths=[...document.querySelectorAll('.imageSelect:checked')].map(x=>x.dataset.path);if(!identity){alert('Type a new identity name first');return}if(document.body.classList.contains('read-only')){let r=await fetch('/api/image-decisions/pending',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({identity,family:document.querySelector('#family')?.value||'review',paths,status:'confirmed',notes:document.querySelector('#notes')?.value||'New identity assignment queued from review'})});let d=await r.json();document.querySelector('#imageAssignTools p').textContent=r.ok?'Queued '+d.queued+' assignment(s) for '+identity+'; identity creation and moves will run after the rebuild.':(d.error||'Identity assignment failed');return}let {response:r,data:d,cancelled}=await postNewIdentityWithPrompt('/api/identities',{canonical:identity,family:document.querySelector('#family')?.value||'review',paths,notes:document.querySelector('#notes')?.value||''});if(cancelled)return;if(r.ok){lastMoveId=d.move_id||lastMoveId;await loadIdentityOptions(d.identity);document.querySelector('#identity').value=d.identity}showUndoOption(r.ok?'Saved and confirmed '+d.identity+'; moved '+(d.moved||[]).length+' image(s)'+((d.errors||[]).length?' ('+d.errors.length+' move error(s))':''): (d.error||'Identity creation failed'),d.move_id)}
function showUndoOption(message,moveId=null,target='#imageAssignTools p'){let status=document.querySelector(target);if(!status)return;status.replaceChildren(document.createTextNode(message));if(!moveId)return;let button=document.createElement('button');button.type='button';button.textContent='Undo this assignment / move';button.onclick=()=>window.picorgUndoMove(moveId);status.append(document.createTextNode(' '),button)}
async function undoLastMove(){let moveId=lastMoveId;if(!moveId){let r=await fetch('/api/moves');let d=await r.json();moveId=d.operations?.slice().reverse().find(x=>!x.undone)?.id||''}if(!moveId){alert('No reversible move found');return}if(!confirm('Undo the last confirmed move?'))return;let r=await fetch('/api/moves/'+encodeURIComponent(moveId)+'/undo',{method:'POST'});let d=await r.json();if(!r.ok){alert(d.error||'Undo failed');return}lastMoveId=null;let status=document.querySelector('#imageAssignTools p');if(status)status.textContent='Undid '+d.restored.length+' move(s)';if(selected)await select(selected)}
async function updateMember(action){if(!window.memberPath){alert('Select an image first');return}let body={path:window.memberPath,action};if(action==='add')body.target_cluster_id=document.querySelector('#targetCluster').value||selected;let r=await fetch('/api/clusters/'+selected+'/members',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let d=await r.json();if(!r.ok){alert(d.error||'Membership update failed');return}select(selected)}
let loadGeneration=0;
async function loadClusterImages(){let grid=document.querySelector('#detail .grid');if(!grid||!selected)return;let generation=++loadGeneration;grid.dataset.loaded='0';grid.innerHTML='<p class=\"muted\">Loading all cluster images…</p>';try{let x=pendingClusterDetail?.id===selected?pendingClusterDetail.data:null;pendingClusterDetail=null;if(!x){let response=await fetch('/api/clusters/'+selected,{headers:{Accept:'application/json'}});x=await response.json();if(!response.ok)throw new Error(x.error||`Request failed (${response.status})`)}if(generation!==loadGeneration)return;currentClusterImageDecisions=x.image_decisions||{};currentClusterAssignment=x.decision||null;let paths=Array.isArray(x.paths)?x.paths:[];grid.innerHTML='';for(let offset=0;offset<paths.length;offset+=100){if(generation!==loadGeneration)return;grid.insertAdjacentHTML('beforeend',paths.slice(offset,offset+100).map(path=>mediaTile(path,currentClusterImageDecisions[path]||currentClusterAssignment)).join(''));grid.dataset.loaded=String(Math.min(offset+100,paths.length));await new Promise(requestAnimationFrame)}if(!paths.length)grid.innerHTML='<p class=\"muted\">No images in this cluster.</p>'}catch(error){if(generation===loadGeneration){grid.innerHTML=`<p role=\"alert\">${esc(error.message)} <button type=\"button\" onclick=\"loadClusterImages()\">Retry</button></p>`}}}
const clusterImageObserver=new MutationObserver(()=>{/* intentionally no auto load-all; samples use data-loaded=sample until Load all */});
clusterImageObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
let lastDetailSelection=null;
const selectionViewObserver=new MutationObserver(()=>{if(window.innerWidth<=700&&selected&&selected!==lastDetailSelection&&document.querySelector('#detail .grid')){lastDetailSelection=selected;document.querySelector('#detail').scrollIntoView({behavior:'smooth',block:'start'})}});
selectionViewObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
async function loadPage(reset){let generation=++loadGeneration;if(reset){page=1;clusters=[];document.querySelector('#list').textContent='Loading…'}let q=encodeURIComponent(document.querySelector('#filter').value);try{let r=await fetch(`/api/clusters?page=${page}&page_size=50&q=${q}`,{headers:{Accept:'application/json'}});let c=await r.json();if(!r.ok)throw new Error(c.error||`Request failed (${r.status})`);if(generation!==loadGeneration)return;for(const cluster of c.clusters||[])manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(cluster.manual_groups_by_path||{})};clusters=reset?c.clusters:clusters.concat(c.clusters);hasNext=c.has_next;document.querySelector('#summary').textContent=`${c.total} matching clusters · loaded ${clusters.length}`;let next=document.querySelector('#nextPage');next.disabled=!hasNext;next.textContent=hasNext?'Next page':'No more pages';renderList();if(reset){page=2;document.querySelector('#detail').innerHTML='<h2>Select a cluster</h2><p class="muted">Choose a cluster from the list. Large clusters show samples first — use Load all only when needed.</p>'}else if(!reset&&hasNext)page++}catch(error){if(generation!==loadGeneration)return;document.querySelector('#list').innerHTML=`<p role="alert">${esc(error.message)} <button type="button" onclick="loadPage(${reset})">Retry</button></p>`}}
  async function init(){try{let r=await fetch('/api/summary',{headers:{Accept:'application/json'}});let a=await r.json();if(!r.ok)throw new Error(a.error||`Request failed (${r.status})`);applyRuntimeState(a);renderSystemStatus(a);document.querySelector('#summary').textContent=`${a.clusters} clusters · ${a.decisions} saved decisions`;await loadPage(true)}catch(error){document.querySelector('#summary').innerHTML=`<span role="alert">${esc(error.message)}</span>`;document.querySelector('#list').innerHTML='<button type="button" onclick="init()">Retry loading</button>'}}
window.addEventListener('unhandledrejection',event=>{let detail=document.querySelector('#detail');if(detail)detail.innerHTML=`<p role="alert">${esc(event.reason?.message||'The request failed.')} <button type="button" onclick="select(selected)">Retry</button></p>`});
</script></body></html>"""


HTML_PAGE = (HTML_PAGE.replace("</style>", ".detail{height:100vh;overflow:auto;box-sizing:border-box;position:sticky;top:0}main{grid-template-columns:minmax(300px,360px) minmax(0,1fr)}.side{min-width:0}.media-tile{position:relative;min-width:0}.media-tile>a{display:block}.select-control{position:absolute;z-index:2;top:6px;left:6px;display:flex;align-items:center;gap:4px;padding:4px 7px;background:rgba(0,0,0,.78);color:#fff;border-radius:5px;font-size:12px;cursor:pointer}.select-control .imageSelect{width:20px;height:20px;margin:0}.select-control span{user-select:none}@media(max-width:700px){main{display:block}.detail{position:static;height:auto;overflow:visible}.cluster-grid{grid-template-columns:1fr}}" + chr(10) + "</style>")
             .replace("join('\n')", "join('\\n')")
             .replace("split('\n')", "split(String.fromCharCode(10))")
             .replace('onclick="updateMember(\'add\')"', 'onclick="updateMember(&#39;add&#39;)"')
             .replace('onclick="updateMember(\'remove\')"', 'onclick="updateMember(&#39;remove&#39;)"')
             .replace('<h1>Candidate clusters</h1>', '<h1>PicOrg review</h1><div class="view-tabs"><button id="identityTab" type="button" onclick="window.picorgNavigate(\'identities\')">Identities</button><button id="clusterTab" type="button" onclick="window.picorgNavigate(\'clusters\')">Clusters</button></div><details class="quick-help"><summary>How to review</summary><ol><li>Choose an identity or face cluster.</li><li>Inspect images individually; use the checkbox to select.</li><li>Assign only when the face evidence is clear.</li></ol></details>')
             .replace('<label for="filter">Search clusters</label>', '<label for="filter">Search</label>')
             .replace('placeholder="Filter clusters"', 'placeholder="Search identities"')
             .replace('<input id="filter" placeholder="Search identities" oninput="loadPage(true)">', '<select id="clusterMode" aria-label="Cluster source" onchange="clusterMode=this.value;loadPage(true)"><option value="face" selected>Face groups (face match)</option></select><input id="filter" placeholder="Search identities" oninput="loadPage(true)">')
             .replace('</style>', '.view-tabs{display:flex;gap:6px;margin:12px 0}.view-tabs button{flex:1;padding:7px 4px}.view-tabs button.active{background:#7cc4ff;color:#10202c;border-color:#7cc4ff}.identity-grid{display:grid;gap:8px}.identity-card{display:block;width:100%;text-align:left;background:#2d343c;color:inherit;border:1px solid #46505a;border-radius:8px;padding:11px;cursor:pointer}.identity-card.active{border-color:#7cc4ff}.identity-card b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.identity-card small{display:block;color:#b7c0ca;margin-top:4px}.identity-family{font-size:11px;color:#8fd3ff;text-transform:uppercase;letter-spacing:.04em}.identity-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.identity-toolbar button{padding:7px 10px}.accuracy-tools{display:grid;gap:6px;margin:14px 0;padding:10px;background:#2d343c;border:1px solid #46505a;border-radius:8px}.accuracy-tools button{font-size:12px}.accuracy-tools pre{white-space:pre-wrap;overflow:auto;max-height:130px;margin:0;font:11px ui-monospace,monospace;color:#b7c0ca}.image-assignment{display:flex;gap:5px;align-items:center;justify-content:space-between;margin-top:4px;font-size:11px}.image-assignment span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.image-assignment button{padding:3px 6px;font-size:11px}</style>', 1))


HTML_PAGE = HTML_PAGE.replace(
    ".image-assignment{display:flex;gap:5px;align-items:center;justify-content:space-between;margin-top:4px;font-size:11px}",
    ".image-assignment{display:flex;gap:5px;align-items:center;justify-content:flex-start;flex-wrap:wrap;margin-top:4px;font-size:11px}",
).replace(
    ".image-assignment span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".image-assignment span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}",
)

HTML_PAGE = HTML_PAGE.replace(
    "</style>",
    ".image-actions{position:relative}.image-actions summary{cursor:pointer;list-style:none;padding:2px 6px;border:1px solid #66717c;border-radius:4px}.image-actions summary::-webkit-details-marker{display:none}.image-actions menu{position:absolute;right:0;z-index:4;display:grid;gap:4px;margin:4px 0 0;padding:6px;min-width:100px;background:#20252b;border:1px solid #66717c;border-radius:5px}.image-actions menu li{list-style:none}.image-actions menu button{width:100%;text-align:left}#imageAssignTools,#memberTools,.identity-toolbar,.detail>.form{position:sticky;top:0;z-index:3;padding:8px;background:#f6f7f9;border-bottom:1px solid #ccd3da}.detail{display:flex;flex-direction:column}.detail>h2,.detail>.meta{order:1}.detail>#imageAssignTools,.detail>#memberTools,.detail>.identity-toolbar,.detail>.form{order:2}.detail>.grid{order:3}</style>",
)

HTML_PAGE = HTML_PAGE.replace(
    '<select id="clusterMode"',
    '<label><input id="hideConfirmed" type="checkbox" checked onchange="hideConfirmed=this.checked;refreshVisibleClusters()"> Hide confirmed images</label><select id="clusterMode"',
)
HTML_PAGE = HTML_PAGE.replace('oninput="loadPage(true)"', 'oninput="refreshVisibleClusters()"')
HTML_PAGE = HTML_PAGE.replace(
    'mode=${clusterMode}&q=${q}',
    'mode=${clusterMode}&hide_confirmed=${hideConfirmed?\'1\':\'0\'}&q=${q}',
)
HTML_PAGE = HTML_PAGE.replace(
    'page=${page}&page_size=50&q=${q}',
    'page=${page}&page_size=50&mode=${clusterMode}&hide_confirmed=${hideConfirmed?\'1\':\'0\'}&q=${q}',
)
HTML_PAGE = HTML_PAGE.replace('>Next page</button>', '>Load next 50</button>')
HTML_PAGE = HTML_PAGE.replace('oninput="refreshVisibleClusters()"', 'oninput="queueFilterRefresh()"')
HTML_PAGE = HTML_PAGE.replace(
    'Assigned: <b>${esc(identity)}</b>',
    'Assigned: <b>${esc(identity)}</b> <small class="assignment-state">(${esc(state)})</small>',
)

HTML_PAGE = HTML_PAGE.replace(
    "</style>",
    "#imageAssignTools{display:flex;align-items:center;gap:7px;flex-wrap:wrap}#imageAssignTools>b,#imageAssignTools>p{flex-basis:100%;margin:2px 0}#imageAssignTools button{padding:6px 9px;font-size:13px}.detail>.form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;max-width:none}.detail>.form label:nth-of-type(1),.detail>.form label:nth-of-type(2),.detail>.form label:nth-of-type(3),.detail>.form label:nth-of-type(4),.detail>.form label:nth-of-type(5),.detail>.form label:nth-of-type(6),.detail>.form>button,.detail>.form>.status{grid-column:span 2}.detail>.form input,.detail>.form select,.detail>.form textarea{width:100%;box-sizing:border-box}.media-modal-toolbar{display:grid;grid-template-columns:auto auto minmax(150px,1fr) minmax(100px,.7fr) minmax(150px,1fr) auto;gap:6px;align-items:center}.media-modal-toolbar button{min-width:0;padding:6px 9px;font-size:13px}.media-modal-toolbar select,.media-modal-toolbar input{min-width:0;max-width:none;width:100%;box-sizing:border-box;padding:6px 8px}@media(max-width:760px){.detail>.form{grid-template-columns:1fr}.detail>.form label:nth-of-type(1),.detail>.form label:nth-of-type(2),.detail>.form label:nth-of-type(3),.detail>.form label:nth-of-type(4),.detail>.form label:nth-of-type(5),.detail>.form label:nth-of-type(6),.detail>.form>button,.detail>.form>.status{grid-column:span 1}.media-modal-toolbar{grid-template-columns:repeat(2,minmax(0,1fr));width:100%}.media-modal-toolbar select,.media-modal-toolbar input{grid-column:span 2}.media-modal-toolbar .approve{grid-column:span 2}}</style>",
    1,
)

HTML_PAGE = HTML_PAGE.replace(
    "</style>",
    ".media-modal-content{display:flex;flex-direction:column;align-items:center;gap:10px}.media-modal-toolbar{display:flex;gap:8px;align-items:center;justify-content:center;flex-wrap:wrap;width:min(95vw,900px)}.media-modal-toolbar button{min-width:92px}.media-modal-toolbar select,.media-modal-toolbar input{min-width:180px;max-width:36vw}.media-modal-status{color:#fff;min-height:20px;max-width:95vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}</style>",
    1,
)

IDENTITY_UI_SCRIPT = r"""
let viewMode='clusters', identityGroups=[], selectedIdentity=null;
const loadClustersPage=loadPage;
async function loadIdentityGroups(){let r=await fetch('/api/identity-groups?include_disk=1',{headers:{Accept:'application/json'}});let data=await r.json();if(!r.ok)throw new Error(data.error||`Request failed (${r.status})`);identityGroups=data;for(const group of data)manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(group.manual_groups_by_path||{})};return data}
async function refreshIdentities(){let button=document.querySelector('#refreshIdentities');if(button)button.disabled=true;try{await loadIdentityGroups();if(viewMode==='identities')renderIdentityList();else document.querySelector('#summary').textContent=`${identityGroups.length} identities loaded`}catch(error){if(viewMode==='identities')document.querySelector('#list').innerHTML=`<p role="alert">${esc(error.message)}</p>`}finally{if(button)button.disabled=false}}
function setActiveView(){document.querySelector('#identityTab')?.classList.toggle('active',viewMode==='identities');document.querySelector('#clusterTab')?.classList.toggle('active',viewMode==='clusters');let next=document.querySelector('#nextPage');if(next)next.style.display=viewMode==='clusters'?'block':'none';let filter=document.querySelector('#filter');if(filter)filter.placeholder=viewMode==='identities'?'Search identities':'Filter clusters';let label=document.querySelector('label[for="filter"]');if(label)label.textContent=viewMode==='identities'?'Search identities':'Search clusters'}
async function showView(mode){viewMode=mode;setActiveView();document.querySelector('#filter').value='';selected=null;if(mode==='identities'){document.querySelector('#list').textContent='Scanning identity folders…';document.querySelector('#summary').textContent='Loading identity media from disk…';document.querySelector('#detail').innerHTML='<p class="muted">Scanning sorted identity folders (a few seconds)…</p>';try{await loadIdentityGroups();renderIdentityList()}catch(error){document.querySelector('#list').innerHTML=`<p role="alert">${esc(error.message)} <button type="button" onclick="showView('identities')">Retry</button></p>`}return}document.querySelector('#list').textContent='Loading clusters…';await loadClustersPage(true)}
window.picorgNavigate=async function(mode){try{return await window.showView(mode)}catch(error){let detail=document.querySelector('#detail');if(detail)detail.innerHTML=`<p role="alert">Unable to open ${esc(mode)}: ${esc(error?.message||error)} <button type="button" onclick="window.picorgNavigate('${esc(mode)}')">Retry</button></p>`;return null}};
function isCuratedIdentity(g){let hidden=['reddit_follow','reddit_subreddit','reddit_friends','pscrape','imdb'];return !hidden.includes(g.family)&&(!g.generic||g.family==='review')&&(g.count>0||['manual','review','metadaily','redditdaily'].includes(g.family))}
async function refreshVisibleClusters(){if(viewMode!=='clusters')return;let q=encodeURIComponent(document.querySelector('#filter').value);try{let r=await fetch(`/api/clusters?page=1&page_size=50&mode=${clusterMode}&hide_confirmed=${hideConfirmed?'1':'0'}&q=${q}`,{headers:{Accept:'application/json'}});let c=await r.json();if(!r.ok)throw new Error(c.error||`Request failed (${r.status})`);clusters=c.clusters;for(const cluster of clusters)manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(cluster.manual_groups_by_path||{})};page=2;hasNext=c.has_next;document.querySelector('#summary').textContent=`${c.total} ${clusterMode==='face'?'face':''} matching clusters · loaded ${clusters.length}`;renderList()}catch(error){document.querySelector('#list').innerHTML=`<p role="alert">${esc(error.message)}</p>`}}
function renderIdentityList(){let q=(document.querySelector('#filter').value||'').trim().toLowerCase();let familyOrder={manual:0,review:1,metadaily:2,redditdaily:3};let visible=identityGroups.filter(g=>isCuratedIdentity(g)&&(g.identity+' '+(g.aliases||[]).join(' ')).toLowerCase().includes(q)).sort((a,b)=>((b.count||0)-(a.count||0))||((familyOrder[a.family]??9)-(familyOrder[b.family]??9))||a.identity.localeCompare(b.identity));document.querySelector('#list').className='identity-grid';document.querySelector('#summary').textContent=`${visible.length} identities · ${visible.reduce((n,g)=>n+g.count,0)} assigned media`;document.querySelector('#list').innerHTML=visible.map(g=>{let encoded=encodeURIComponent(g.identity);return `<button type="button" class="identity-card ${selectedIdentity===g.identity?'active':''}" onclick="selectIdentity(decodeURIComponent('${encoded}'))"><span class="identity-family">${esc(g.family||'review')}</span><b>${esc(g.identity)}</b><small>${g.count} media · ${g.confirmed} approved · ${g.pending} pending · ${g.rejected} rejected</small></button>`}).join('')||'<p class="muted">No identities match this search.</p>'}
async function selectIdentity(identity){selectedIdentity=identity;renderIdentityList();let g=identityGroups.find(item=>item.identity===identity);if(!g){document.querySelector('#detail').innerHTML='<p class="muted">Identity not found.</p>';return}currentClusterImageDecisions={};currentClusterAssignment=null;currentIdentityAssignment={identity:g.identity,family:g.family||'review',status:'pending'};let images=g.sample_paths.map(path=>mediaTile(path,currentIdentityAssignment)).join('');document.querySelector('#detail').innerHTML=`<h2>${esc(g.identity)}</h2><div class="meta"><b>${g.count} assigned media</b><br>Family: ${esc(g.family||'review')}<br>Approved: ${g.confirmed} · Pending: ${g.pending} · Rejected: ${g.rejected}</div><div class="identity-toolbar"><button type="button" onclick="selectAllImages()">Select all</button><button type="button" onclick="selectNoImages()">Select none</button><button type="button" class="approve" onclick="reviewIdentitySelection('confirmed')">Approve selected</button><button type="button" class="reject" onclick="reviewIdentitySelection('rejected')">Reject selected</button></div><p class="muted">Approve only when the face evidence supports this identity. Approved files are moved into the identity folder.</p><div class="grid">${images||'<p class="muted">No assigned media yet.</p>'}</div>`}
async function reviewIdentitySelection(status){let paths=[...document.querySelectorAll('#detail .imageSelect:checked')].map(x=>x.dataset.path);if(!paths.length||!selectedIdentity){alert('Select at least one image first');return}let g=identityGroups.find(item=>item.identity===selectedIdentity)||{};let r=await fetch(assignmentEndpoint(),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths,identity:selectedIdentity,family:g.family||'review',status,notes:'Reviewed from identity view'})});let d=await r.json();if(!r.ok){alert(d.error||'Unable to save review');return}rememberIdentityUsed(selectedIdentity,g.family||'review');lastMoveId=d.move_id||lastMoveId;if(d.queued){alert('Queued '+d.queued+' assignment(s); they will be applied after the rebuild.')}await loadIdentityGroups();renderIdentityList();await selectIdentity(selectedIdentity);let message=d.queued?'Queued '+d.queued+' assignment(s) for after the rebuild':status==='confirmed'?`Approved ${d.saved} image(s); moved ${(d.moved||[]).length}${(d.errors||[]).length?'; '+d.errors.length+' move error(s)':''} for ${selectedIdentity}`:'Reviewed '+d.saved+' image(s) for '+selectedIdentity;showUndoOption(message,d.move_id)}
const originalRenderList=renderList;
renderList=function(){if(viewMode==='identities'){renderIdentityList();return}originalRenderList();document.querySelectorAll('.cluster-actions').forEach(node=>node.remove())};
loadPage=async function(reset){if(viewMode==='identities'){if(reset){await loadIdentityGroups();renderIdentityList()}return}return loadClustersPage(reset)};
setActiveView();showView('clusters');loadAccuracyStatus();refreshRuntimeState();refreshSystemStatus();

const originalClusterSelect=select;
select=async function(id){await originalClusterSelect(id);if(hideConfirmed){document.querySelectorAll('#detail .imageSelect').forEach(input=>{if(currentClusterImageDecisions[input.dataset.path]?.status==='confirmed')input.closest('.media-tile')?.remove()})}};
const originalLoadClusterImages=loadClusterImages;
loadClusterImages=async function(){await originalLoadClusterImages();if(hideConfirmed){document.querySelectorAll('#detail .imageSelect').forEach(input=>{if(currentClusterImageDecisions[input.dataset.path]?.status==='confirmed')input.closest('.media-tile')?.remove()})}};
function showHiddenConfirmedNote(){if(!hideConfirmed)return;let hidden=Object.values(currentClusterImageDecisions).filter(item=>item?.status==='confirmed').length;if(!hidden)return;let meta=document.querySelector('#detail .meta');if(!meta||meta.querySelector('.hidden-confirmed-note'))return;let note=document.createElement('span');note.className='hidden-confirmed-note muted';note.textContent=`${hidden} confirmed assignment(s) hidden; uncheck “Hide confirmed images” to view their identities.`;meta.append(' ',note)}
const originalSelectWithAssignments=select;
select=async function(id){await originalSelectWithAssignments(id);showHiddenConfirmedNote()};
const originalAssignSelectedImages=assignSelectedImages;
  assignSelectedImages=async function(){const result=await originalAssignSelectedImages();if(selected)await select(selected);return result};
const originalConfirmImage=confirmImage;
confirmImage=async function(event,path,identity,family){await originalConfirmImage(event,path,identity,family);if(hideConfirmed)event.currentTarget.closest('.media-tile')?.remove()};
"""

IDENTITY_UI_SCRIPT = IDENTITY_UI_SCRIPT.replace(
    "currentIdentityAssignment={identity:g.identity,family:g.family||'review',status:'pending'};let images=g.sample_paths.map(path=>mediaTile(path,currentIdentityAssignment)).join('');",
    "currentIdentityAssignment={identity:g.identity,family:g.family||'review',status:'pending'};let images=g.sample_paths.map(path=>mediaTile(path,{...currentIdentityAssignment,status:g.status_by_path?.[path]||'pending'})).join('');",
)
HTML_PAGE = HTML_PAGE.replace('</script>', IDENTITY_UI_SCRIPT + '</script>', 1)


MODAL_UI_SCRIPT = r"""
let modalPaths=[], modalIndex=0, deletedModalPaths=new Set(), modalLastIdentity=null, modalUndoMoveId=null, modalSessionId=0, modalActionSequence=0, modalUndoSequence=0, modalPendingAssignments=new Map(), modalMoreToolsOpen=false;
function gridMediaPaths(){return [...document.querySelectorAll('#detail .media-tile .imageSelect')].map(input=>input.dataset.path).filter(Boolean)}
function modalMediaType(path){return /\.(mp4|mov|webm|m4v)$/i.test(path)?'video':'img'}
function syncModalIdentityOptions(){let select=document.querySelector('#modalIdentity');if(!select)return;let current=select.value||document.querySelector('#identity')?.value||currentIdentityAssignment?.identity||selectedIdentity||'';let query=(document.querySelector('#modalIdentitySearch')?.value||'').trim().toLocaleLowerCase();let grouped={};for(let item of (identityOptions||[])){let value=item.canonical||'';if(value&&(!query||value.toLocaleLowerCase().includes(query)||(item.aliases||[]).some(alias=>String(alias).toLocaleLowerCase().includes(query))))(grouped[item.family||'other']??=[]).push(value)}if(current&&!Object.values(grouped).flat().includes(current)&&(!query||current.toLocaleLowerCase().includes(query)))(grouped[currentIdentityAssignment?.family||'review']??=[]).push(current);select.replaceChildren(new Option(query?'No matching identity':'Choose an identity',''));let empty=select.options[0];empty.disabled=Boolean(query);let order=['manual','review','metadaily','redditdaily','reddit_follow','reddit_subreddit','pscrape','other'];order=[...new Set([...order,...Object.keys(grouped).sort()])];for(let family of order){let values=[...new Set(grouped[family]||[])].sort((a,b)=>a.localeCompare(b));if(!values.length)continue;let group=document.createElement('optgroup');group.label=family==='reddit_subreddit'?'Subreddits':family==='reddit_follow'?'Reddit accounts':family==='redditdaily'?'Redditdaily':family==='metadaily'?'Metadaily':family[0].toUpperCase()+family.slice(1);for(let value of values)group.append(new Option(value,value,value===current,value===current));select.append(group)}}
function closeMediaModal(){let modal=document.querySelector('#mediaModal');modal.hidden=true;document.querySelector('#mediaModalContent').replaceChildren()}
function renderMediaModal(){let modal=document.querySelector('#mediaModal'),content=document.querySelector('#mediaModalContent');if(!modal||!content)return;let path=modalPaths[modalIndex];if(!path){closeMediaModal();return}content.replaceChildren();let queuedPath=currentClusterImageDecisions[path]?.status==='queued';let toolbar=document.createElement('div');toolbar.className='media-modal-toolbar';let navigation=document.createElement('div');navigation.className='modal-control-group modal-navigation';navigation.setAttribute('role','group');navigation.setAttribute('aria-label','Image navigation');let identityTools=document.createElement('section');identityTools.className='modal-control-group modal-identity-tools';identityTools.setAttribute('aria-label','Identity assignment');let primaryActions=document.createElement('div');primaryActions.className='modal-control-group modal-primary-actions';primaryActions.setAttribute('role','group');primaryActions.setAttribute('aria-label','Assign and undo');let more=document.createElement('details');more.className='modal-more-tools';more.open=modalMoreToolsOpen;more.addEventListener('toggle',()=>{modalMoreToolsOpen=more.open});let moreLabel=document.createElement('summary');moreLabel.textContent='More tools';let moreActions=document.createElement('div');moreActions.className='modal-more-actions';let previous=document.createElement('button');previous.type='button';previous.textContent='Previous';previous.disabled=modalIndex<=0;previous.onclick=()=>{if(modalIndex>0){modalIndex--;renderMediaModal()}};let next=document.createElement('button');next.type='button';next.textContent='Next';next.disabled=modalIndex>=modalPaths.length-1;next.onclick=()=>{if(modalIndex<modalPaths.length-1){modalIndex++;renderMediaModal()}};let position=document.createElement('span');position.className='modal-position';position.setAttribute('aria-live','polite');position.textContent=`${modalIndex+1} of ${modalPaths.length}`;navigation.append(previous,position,next);let search=document.createElement('input');search.id='modalIdentitySearch';search.placeholder='Type to find identity';search.setAttribute('aria-label','Search identities');search.oninput=syncModalIdentityOptions;let picker=document.createElement('select');picker.id='modalIdentity';picker.setAttribute('aria-label','Identity for current image');picker.onchange=()=>{let detail=document.querySelector('#identity');if(detail)detail.value=picker.value};let family=document.createElement('select');family.id='modalFamily';family.setAttribute('aria-label','Family for current image');['manual','review','metadaily','redditdaily','reddit_follow','reddit_subreddit','pscrape'].forEach(value=>family.append(new Option(value,value,value===(document.querySelector('#family')?.value||currentIdentityAssignment?.family||'review'))));let newIdentity=document.createElement('input');newIdentity.id='modalNewIdentity';newIdentity.placeholder='New identity (optional)';newIdentity.setAttribute('aria-label','New identity for current image');let lens=document.createElement('button');lens.type='button';lens.textContent='Google Lens';lens.title='Open this image in Google Lens';lens.onclick=()=>openGoogleLens(path);let download=document.createElement('button');download.type='button';download.textContent='Download for Lens';download.title='Download locally, then upload to Google Lens';download.onclick=()=>downloadForLens(path);let useLast=document.createElement('button');useLast.type='button';useLast.textContent='Use last identity';useLast.disabled=!modalLastIdentity;useLast.onclick=useLastModalIdentity;let undo=document.createElement('button');undo.type='button';undo.textContent='Undo last action';undo.className='modal-undo';undo.disabled=!modalUndoMoveId||[...modalPendingAssignments.values()].some(sequence=>sequence>modalUndoSequence);undo.onclick=undoModalMove;let assign=document.createElement('button');assign.type='button';assign.className='approve';assign.textContent='Assign & move';assign.onclick=()=>assignModalImage();let remove=document.createElement('button');remove.type='button';remove.className='reject delete-media';remove.textContent='Delete permanently';remove.title='Permanently delete this file';remove.onclick=()=>deleteModalImage();identityTools.append(search,picker,family,newIdentity,useLast);primaryActions.append(assign);moreActions.append(undo,lens,download,remove);more.append(moreLabel,moreActions);toolbar.append(navigation,identityTools,primaryActions,more);content.append(toolbar);let status=document.createElement('div');status.className='media-modal-status';status.textContent=deletedModalPaths.has(path)?'Deleted permanently — preview retained for review':queuedPath?'Assignment queued — moving in background':`Image ${modalIndex+1} of ${modalPaths.length}`;content.append(status);let media=document.createElement(modalMediaType(path));media.alt='Full-size media preview';media.decoding='async';media.loading='lazy';if(media.tagName==='VIDEO'){media.controls=true;media.autoplay=true;media.muted=true;media.playsInline=true}media.onerror=()=>{status.textContent='Unable to preview this file'};let frame=document.createElement('div');frame.className='media-preview-frame';frame.append(media);if(deletedModalPaths.has(path)){let overlay=document.createElement('div');overlay.className='media-deleted-overlay';overlay.textContent='Deleted permanently';frame.append(overlay)}else if(queuedPath){let overlay=document.createElement('div');overlay.className='media-queued-overlay';overlay.textContent='Queued';frame.append(overlay)}content.append(frame);let caption=document.createElement('div');caption.className='media-modal-caption';caption.textContent=path;content.append(caption);syncModalIdentityOptions();updateAssignmentLabels();modal.hidden=false;assign.focus();window.setTimeout(()=>{if(media.isConnected&&!deletedModalPaths.has(path))media.src='/media?path='+encodeURIComponent(path)},0)}
function downloadForLens(path){let link=document.createElement('a');link.href='/media?path='+encodeURIComponent(path);link.download=path.split('/').pop()||'picorg-image';link.rel='noreferrer';document.body.append(link);link.click();link.remove();let status=document.querySelector('.media-modal-status');if(status)status.textContent='Downloaded locally; upload the file to Google Lens.'}
function openGoogleLens(path){if(!window.confirm('This may send the image to Google. Continue?'))return;let mediaUrl=new URL('/media?path='+encodeURIComponent(path),window.location.href).href;let lensUrl='https://lens.google.com/uploadbyurl?url='+encodeURIComponent(mediaUrl);window.open(lensUrl,'_blank','noopener,noreferrer');let status=document.querySelector('.media-modal-status');if(status)status.textContent='Opened Google Lens; LAN-only URLs may require manual upload.'}
function openMediaModal(event,anchor,pathsOverride=null){if(event.target.closest('.imageSelect'))return;event.preventDefault();let path=new URL(anchor.getAttribute('href'),location.href).searchParams.get('path')||'';let paths=Array.isArray(pathsOverride)?[...new Set(pathsOverride.filter(Boolean))]:gridMediaPaths();if(!paths.includes(path))paths=[path,...paths];modalPaths=paths;modalIndex=Math.max(0,modalPaths.indexOf(path));modalLastIdentity=null;modalUndoMoveId=null;modalSessionId++;modalActionSequence=0;modalUndoSequence=0;modalPendingAssignments.clear();renderMediaModal()}
window.openReviewModalForPaths=openMediaModal;
function updateModalUndoControl(){let pendingNewer=[...modalPendingAssignments.values()].some(sequence=>sequence>modalUndoSequence),button=document.querySelector('.modal-undo');if(button)button.disabled=!modalUndoMoveId||pendingNewer;let modal=document.querySelector('#mediaModal'),status=document.querySelector('.media-modal-status');if(modal&&!modal.hidden&&!modalPaths.length&&status)status.textContent=modalPendingAssignments.size?'Batch complete — queued assignments still running.':'Assignment complete — no unassigned images remain in this cluster.'}
function useLastModalIdentity(){if(!modalLastIdentity)return;let picker=document.querySelector('#modalIdentity'),family=document.querySelector('#modalFamily'),newIdentity=document.querySelector('#modalNewIdentity');if(picker&&!Array.from(picker.options).some(option=>option.value===modalLastIdentity.identity))picker.add(new Option(modalLastIdentity.identity,modalLastIdentity.identity));if(picker)picker.value=modalLastIdentity.identity;if(family)family.value=modalLastIdentity.family;if(newIdentity)newIdentity.value=''}
async function undoModalMove(){if(!modalUndoMoveId)return;let result=await window.picorgUndoMove(modalUndoMoveId);if(!result)return;modalUndoMoveId=null;let paths=result.restored_paths||(result.restored||[]).map(item=>item.destination);for(let path of paths){if(!path)continue;currentClusterImageDecisions[path]={status:'pending'};if(!modalPaths.includes(path))modalPaths.unshift(path)}if(modalPaths.length){modalIndex=0;renderMediaModal()}}
async function assignModalImage(){
 let path=modalPaths[modalIndex];if(!path)return;let actionSequence=++modalActionSequence,sessionId=modalSessionId;
 let newIdentity=document.querySelector('#modalNewIdentity')?.value.trim()||'';
 let identity=newIdentity||document.querySelector('#modalIdentity')?.value.trim()||document.querySelector('#identity')?.value.trim()||'';
 if(!identity){alert('Choose an identity or enter a new identity name');return}
  let family=document.querySelector('#modalFamily')?.value||'review';
 let button=document.querySelector('.media-modal-toolbar .approve');if(button)button.disabled=true;
 let endpoint=newIdentity?'/api/identities':'/api/image-decisions/async';
 let body=newIdentity?{canonical:identity,family,paths:[path],notes:'Assigned from modal viewer'}:{paths:[path],identity,family,status:'confirmed',notes:'Assigned and confirmed from modal viewer'};
 try{
  let response,data,cancelled=false,payload=body;
  if(newIdentity){({response,data,cancelled,payload}=await postNewIdentityWithPrompt(endpoint,body));if(cancelled)return;identity=payload.canonical}
  else{response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});data=await response.json()}
  if(!response.ok)throw new Error(data.error||'Image assignment failed');
  if(!newIdentity&&data.assignment_id){
    rememberIdentityUsed(identity,family);modalLastIdentity={identity,family};modalPendingAssignments.set(String(data.assignment_id),actionSequence);updateModalUndoControl();
   currentClusterImageDecisions[path]={identity,family,status:'queued',assignment_id:data.assignment_id,cluster_id:selected};
   waitForAssignment(data.assignment_id).then(item=>finishQueuedAssignment(path,data.assignment_id,item,actionSequence,sessionId)).catch(error=>{if(sessionId===modalSessionId){modalPendingAssignments.delete(String(data.assignment_id));updateModalUndoControl();if(!modalPaths.length){let status=document.querySelector('.media-modal-status');if(status)status.textContent=`Move status unavailable: ${error.message}`}}showQueuedMoveStatus(`Move status unavailable for ${path}: ${error.message}`,true)});
   advanceModalPath(path);return;
  }
  let moved=Array.isArray(data.moved)&&data.moved.some(item=>item?.source===path);
  if(!moved){let detail=(data.errors||[]).map(item=>item?.error||item?.message||String(item)).join('; ');throw new Error(`Assignment saved, but the image was not moved${detail?`: ${detail}`:'.'}`)}
  if(!data.move_id)throw new Error('The file moved, but no move-audit ID was returned. Refresh and verify move history before retrying.');
   rememberIdentityUsed(identity,family);modalLastIdentity={identity,family};
   lastMoveId=data.move_id||lastMoveId;
   modalUndoMoveId=data.move_id;modalUndoSequence=actionSequence;
  currentClusterImageDecisions[path]={identity,family,status:'confirmed'};
  let tile=[...document.querySelectorAll('#detail .media-tile')].find(item=>item.querySelector('.imageSelect')?.dataset.path===path);
  if(hideConfirmed)tile?.remove();
  if(selected)await select(selected);
  showUndoOption('Moved image to '+identity,data.move_id);
  if(viewMode==='manual-groups')await window.picorgRefreshManualGroupView?.();
  advanceModalPath(path)
 }catch(error){let status=document.querySelector('.media-modal-status');if(status)status.textContent=error.message;if(button)button.disabled=false}
}
function advanceModalPath(path,completionMessage='Assignment complete — no unassigned images remain in this cluster.'){
 let position=modalPaths.indexOf(path);if(position>=0)modalPaths.splice(position,1);if(viewMode!=='manual-groups')modalPaths=modalPaths.filter(item=>!['queued','confirmed'].includes(currentClusterImageDecisions[item]?.status));if(!modalPaths.length){if(viewMode==='clusters'||modalUndoMoveId||modalPendingAssignments.size){let modal=document.querySelector('#mediaModal'),content=document.querySelector('#mediaModalContent'),status=document.createElement('div'),toolbar=document.createElement('div'),undo=document.createElement('button');status.className='media-modal-status';status.textContent=modalPendingAssignments.size?'Batch complete — queued assignments still running.':completionMessage;toolbar.className='media-modal-toolbar';undo.type='button';undo.textContent='Undo last action';undo.className='modal-undo';undo.disabled=false;undo.onclick=undoLastMove;toolbar.append(undo);content.replaceChildren(status,toolbar);modal.hidden=false;return}closeMediaModal();showQueuedMoveStatus(completionMessage);return}if(modalIndex>=modalPaths.length)modalIndex=modalPaths.length-1;renderMediaModal();
}
async function waitForAssignment(assignmentId){
 for(let attempt=0;attempt<240;attempt++){
  let response=await fetch('/api/assignment-queue?status=pending,applying,applied,error,conflict,rejected',{headers:{Accept:'application/json'}});
  let payload=await response.json();if(!response.ok)throw new Error(payload.error||`Assignment status request failed (${response.status})`);
  let item=(payload.assignments||[]).find(row=>Number(row.assignment_id)===Number(assignmentId));
  if(item&&['applied','error','conflict','rejected'].includes(item.status))return item;
  await new Promise(resolve=>window.setTimeout(resolve,500));
 }
 throw new Error('Background assignment timed out; check the assignment queue');
}
function showQueuedMoveStatus(message,isError=false){
 let status=document.querySelector('#queuedMoveStatus');
 if(!status){status=document.createElement('div');status.id='queuedMoveStatus';status.setAttribute('role','status');status.setAttribute('aria-live','polite');status.style.cssText='position:fixed;right:16px;bottom:16px;z-index:10000;max-width:min(520px,calc(100vw - 32px));padding:10px 14px;border-radius:8px;background:#173247;color:#fff;box-shadow:0 4px 18px #0005';document.body.append(status)}
 status.setAttribute('role',isError?'alert':'status');status.style.background=isError?'#7a2020':'#173247';status.textContent=message;
 window.clearTimeout(status.dismissTimer);if(!isError)status.dismissTimer=window.setTimeout(()=>status.remove(),8000);
}
async function refreshQueuedMoveCluster(clusterId,path,assignmentId,decision){
 const query=new URLSearchParams({hide_confirmed:hideConfirmed?'1':'0'});
 const response=await fetch(`/api/clusters/${encodeURIComponent(clusterId)}?${query}`,{headers:{Accept:'application/json'}});
 const data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
 if(viewMode!=='clusters'||selected!==clusterId)return;
 currentClusterImageDecisions[path]={...decision,...(currentClusterImageDecisions[path]||{}),status:'confirmed',assignment_id:assignmentId};
 const cluster=clusters.find(item=>item.cluster_id===clusterId);
 if(cluster){cluster.count=data.count;cluster.sample_paths=data.sample_paths;cluster.title=data.title;cluster.expected_identities=data.expected_identities;cluster.source_roots=data.source_roots;renderList()}
 const detail=document.querySelector('#detail');
 const title=detail?.querySelector('h2');if(title)title.textContent=data.title;
 const meta=detail?.querySelector('.meta');if(meta)meta.innerHTML=`<b>${data.count} files</b><br>Expected labels: ${esc(data.expected_identities.join(', ')||'none')}<br>Sources: ${esc(data.source_roots.join(', '))}`;
 if(hideConfirmed){[...document.querySelectorAll('#detail .imageSelect')].find(input=>input.dataset.path===path)?.closest('.media-tile')?.remove();showHiddenConfirmedNote()}
}
async function finishQueuedAssignment(path,assignmentId,item,actionSequence,sessionId){
 if(sessionId===modalSessionId){modalPendingAssignments.delete(String(assignmentId));if(item.status==='applied'&&item.move_id&&actionSequence>=modalUndoSequence){modalUndoMoveId=item.move_id;modalUndoSequence=actionSequence}updateModalUndoControl()}
 let decision=currentClusterImageDecisions[path];
 if(decision&&Number(decision.assignment_id)!==Number(assignmentId))return;
 decision=decision||{identity:item.identity||'',family:'review',assignment_id:assignmentId,cluster_id:selected};
 const clusterId=decision.cluster_id||selected;
 if(item.status==='applied'){
  decision.status='confirmed';delete decision.error;
  showQueuedMoveStatus(`Move complete: ${path} was moved to ${item.identity||decision.identity}.`);
  if(viewMode==='manual-groups')try{await window.picorgRefreshManualGroupView?.()}catch(error){console.error('Unable to refresh manual collection after queued assignment',error)}
  if(viewMode==='clusters'&&clusterId)try{await refreshQueuedMoveCluster(clusterId,path,assignmentId,decision)}catch(error){console.error('Unable to refresh cluster after queued move',error)}
 }else{
  decision.status=item.status;decision.error=item.error||`Assignment ${item.status}`;if(sessionId===modalSessionId&&!modalPaths.length){let status=document.querySelector('.media-modal-status');if(status)status.textContent=`Move ${item.status}: ${decision.error}`;}
  let tile=[...document.querySelectorAll('#detail .imageSelect')].find(input=>input.dataset.path===path)?.closest('.media-tile');
  if(tile){let note=tile.querySelector('.queued-move-error');if(!note){note=document.createElement('span');note.className='queued-move-error';note.setAttribute('role','alert');note.style.cssText='display:block;color:#b42318;font-weight:600;padding:6px';tile.append(note)}note.textContent=`Move ${item.status}: ${decision.error}`}
  showQueuedMoveStatus(`Move ${item.status}: ${path} — ${decision.error}`,true);
 }
}
async function deleteModalImage(){
 let path=modalPaths[modalIndex];if(!path)return;
 if(!window.confirm('Permanently delete '+path+'? This cannot be undone.'))return;
 let button=document.querySelector('.media-modal-toolbar .delete-media');if(button)button.disabled=true;
 let status=document.querySelector('.media-modal-status');
 try{
  let response=await fetch('/api/media/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,confirm:true,confirm_path:path})});
  let data=await response.json();if(!response.ok||!data.deleted)throw new Error(data.error||'Permanent delete failed');
  deletedModalPaths.add(path);let frame=document.querySelector('.media-preview-frame');if(frame&&!frame.querySelector('.media-deleted-overlay')){let overlay=document.createElement('div');overlay.className='media-deleted-overlay';overlay.textContent='Deleted permanently';frame.append(overlay)}let deletedStatus=document.querySelector('.media-modal-status');if(deletedStatus)deletedStatus.textContent='Deleted permanently — moving to next…';updateAssignmentLabels();window.setTimeout(()=>advanceModalPath(path),500);
 }catch(error){if(status)status.textContent=error.message;if(button)button.disabled=false}
}
function insertRecentIdentityGroup(select,query,current){let recent=matchingRecentIdentityOptions(query),recentKeys=new Set(recent.map(item=>item.canonical.toLocaleLowerCase()));for(let group of [...select.querySelectorAll('optgroup')]){if(group.label==='Recently used'){group.remove();continue}for(let option of group.querySelectorAll('option'))if(recentKeys.has(option.value.toLocaleLowerCase()))option.remove();if(!group.querySelector('option'))group.remove()}if(!recent.length)return;let group=document.createElement('optgroup');group.label='Recently used';for(let item of recent){let value=item.canonical;group.append(new Option(value,value,value.toLocaleLowerCase()===String(current||'').toLocaleLowerCase(),value.toLocaleLowerCase()===String(current||'').toLocaleLowerCase()))}let firstGroup=select.querySelector('optgroup');if(firstGroup)select.insertBefore(group,firstGroup);else select.append(group)}
const originalRenderIdentityOptions=renderIdentityOptions;renderIdentityOptions=function(){originalRenderIdentityOptions();let select=document.querySelector('#identity');if(select)insertRecentIdentityGroup(select,(document.querySelector('#identityFilter')?.value||'').trim().toLocaleLowerCase(),selectedIdentityValue)};
const originalSyncModalIdentityOptions=syncModalIdentityOptions;syncModalIdentityOptions=function(){originalSyncModalIdentityOptions();let select=document.querySelector('#modalIdentity');if(select)insertRecentIdentityGroup(select,(document.querySelector('#modalIdentitySearch')?.value||'').trim().toLocaleLowerCase(),select.value)};
 function renderModalRecentIdentityList(){let list=document.querySelector('#modalRecentIdentityList');if(!list)return;let query=(document.querySelector('#modalIdentitySearch')?.value||'').trim().toLocaleLowerCase(),options=matchingRecentIdentityOptions(query),selected=document.querySelector('#modalIdentity')?.value||'';list.replaceChildren();list.hidden=!options.length;if(options.length){let label=document.createElement('span');label.textContent='Recently used';list.append(label)}for(let item of options){let button=document.createElement('button');button.type='button';button.textContent=item.canonical;button.title=`Recently used · ${item.family||'review'} · click to assign and move`;button.setAttribute('aria-pressed',String(item.canonical.toLocaleLowerCase()===selected.toLocaleLowerCase()));button.onclick=async()=>{let picker=document.querySelector('#modalIdentity'),family=document.querySelector('#modalFamily'),typed=document.querySelector('#modalNewIdentity'),detailPicker=document.querySelector('#identity'),busyButtons=[...list.querySelectorAll('button')];busyButtons.forEach(button=>button.disabled=true);if(picker&&!Array.from(picker.options).some(option=>option.value===item.canonical))picker.add(new Option(item.canonical,item.canonical));if(picker)picker.value=item.canonical;if(family&&Array.from(family.options).some(option=>option.value===item.family))family.value=item.family;if(detailPicker)detailPicker.value=item.canonical;if(typed)typed.value='';if(modalLastIdentity&&item.canonical===modalLastIdentity.identity)modalLastIdentity={identity:item.canonical,family:item.family};try{await assignModalImage()}finally{if(list.isConnected)renderModalRecentIdentityList()}};list.append(button)}}
const originalSyncModalWithRecent=syncModalIdentityOptions;syncModalIdentityOptions=function(){originalSyncModalWithRecent();let select=document.querySelector('#modalIdentity');if(select){let identityTools=select.closest('.modal-identity-tools'),list=document.querySelector('#modalRecentIdentityList');if(identityTools&&!list){list=document.createElement('div');list.id='modalRecentIdentityList';list.className='modal-recent-identities';list.setAttribute('role','group');list.setAttribute('aria-label','Recently used identities');let label=document.createElement('span');label.textContent='Recently used';list.append(label);identityTools.insertBefore(list,select.nextSibling)}renderModalRecentIdentityList()}};
const originalLoadIdentityOptionsForModal=loadIdentityOptions;loadIdentityOptions=async function(selectedIdentity){let result=await originalLoadIdentityOptionsForModal(selectedIdentity);syncModalIdentityOptions();return result};
const originalLoadClusterImagesForModal=loadClusterImages;loadClusterImages=async function(){let result=await originalLoadClusterImagesForModal();modalPaths=gridMediaPaths();return result};
document.addEventListener('keydown',event=>{let modal=document.querySelector('#mediaModal');if(!modal||modal.hidden)return;if(event.key==='ArrowLeft'){event.preventDefault();if(modalIndex>0){modalIndex--;renderMediaModal()}}else if(event.key==='ArrowRight'){event.preventDefault();if(modalIndex<modalPaths.length-1){modalIndex++;renderMediaModal()}}});
function placeReviewForm(){let detail=document.querySelector('#detail'),form=detail?.querySelector(':scope>.form'),grid=detail?.querySelector(':scope>.grid'),tools=detail?.querySelector('#imageAssignTools'),member=detail?.querySelector('#memberTools');if(!detail||!form||!grid)return;let anchor=member||tools;if(anchor&&form.previousElementSibling!==anchor)anchor.after(form)}
const reviewLayoutObserver=new MutationObserver(placeReviewForm);reviewLayoutObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});placeReviewForm();
"""
HTML_PAGE = HTML_PAGE.replace('</script>', MODAL_UI_SCRIPT + '</script>', 1)

RUNTIME_UI_SCRIPT = r"""
function assignmentEndpoint(){return document.body.classList.contains('read-only')?'/api/image-decisions/pending':'/api/image-decisions'}
function updateAssignmentLabels(){let queued=document.body.classList.contains('read-only'),path=modalPaths[modalIndex],assignmentQueued=currentClusterImageDecisions[path]?.status==='queued';let deleted=deletedModalPaths.has(path);document.querySelectorAll('#imageAssignTools button[onclick*="assignSelectedImages"]').forEach(button=>{button.textContent=queued?'Queue selected assignment':'Assign selected and move'});document.querySelectorAll('.media-modal-toolbar .approve').forEach(button=>{if(button.dataset.picorgAssigned!=='1')button.textContent=queued?'Queue assignment':'Assign & move';button.disabled=deleted||assignmentQueued});document.querySelectorAll('.media-modal-toolbar .delete-media').forEach(button=>{button.disabled=queued||deleted;button.title=queued?'Permanent deletion is disabled while a rebuild is running':deleted?'This file is already deleted':'Permanently delete this file'})}
function applyRuntimeState(data){let readOnly=Boolean(data?.read_only);document.body.classList.toggle('read-only',readOnly);let banner=document.querySelector('#runtimeBanner');if(banner){banner.hidden=!readOnly;banner.textContent=readOnly?'Face rebuild running — assignments are queued for later; moves and other writes are disabled until it completes.':''}updateAssignmentLabels();if(data)renderSystemStatus(data)}
let filterRefreshTimer=0;
function queueFilterRefresh(){window.clearTimeout(filterRefreshTimer);filterRefreshTimer=window.setTimeout(()=>{if(viewMode==='clusters')refreshVisibleClusters();else if(viewMode==='identities')renderIdentityList();else if(viewMode==='attention')loadAttention()},180)}
function formatDuration(seconds){if(!Number.isFinite(seconds))return '';let total=Math.max(0,Math.floor(seconds));let hours=Math.floor(total/3600),minutes=Math.floor((total%3600)/60),secs=total%60;return hours?`${hours}h ${minutes}m`:minutes?`${minutes}m ${secs}s`:`${secs}s`}
function renderSystemStatus(data){let panel=document.querySelector('#systemStatus');if(!panel)return;let report=data?.report||{},attention=data?.attention||{},freshness=data?.freshness||{},audit=String(data?.audit||'').split('/').pop()||'unknown';let mode=data?.read_only?'READ-ONLY (rebuild active)':'WRITABLE REVIEW';let attentionTotal=Number(attention.total||0),pending=Number(data?.pending_assignments||0),schedulerData=data?.scheduler||{},schedulerStatus=schedulerData.running?`Job: ${schedulerData.status?.job||'running'} · ${schedulerData.status?.stage||'working'}`:schedulerData.daemon_running?'Scheduler: waiting':'Scheduler: idle',formatFreshness=value=>value?new Date(value).toLocaleString():'not recorded';panel.innerHTML=`<b>System status</b><span><i class="status-dot online"></i>Server online · ${esc(mode)}</span><span>Audit: <code>${esc(audit)}</code></span><span>${Number(data?.clusters||0).toLocaleString()} clusters · ${Number(data?.decisions||0).toLocaleString()} decisions</span><span>Last intake scan: ${esc(formatFreshness(freshness.last_intake_at))}</span><span>Maps refreshed: ${esc(formatFreshness(freshness.last_map_refresh_at))}</span><span class="${pending?'status-warn':''}">Queued assignments: ${pending.toLocaleString()}</span><span>${esc(schedulerStatus)}</span><span class="${attentionTotal?'status-warn':''}">Needs attention: ${attentionTotal.toLocaleString()}</span><small>Updated ${new Date().toLocaleTimeString()}</small>`}
function renderRebuildStatus(data){let panel=document.querySelector('#rebuildStatus');if(!panel)return;let progressData=data?.progress||{},terminal=!data?.running&&progressData.outcome;if(!data?.running&&!terminal){panel.hidden=true;panel.textContent='';return}let ledger=data.repair_ledger||{},progress=progressData.message,parts=[data.running?'Face rebuild: RUNNING':progressData.outcome==='failed'?'Face rebuild: FAILED':'Face rebuild: COMPLETE'];if(data.running){parts.push(data.stage||'stage unknown');if(data.pid)parts.push(`PID ${data.pid}`);if(Number.isFinite(data.elapsed_seconds))parts.push(formatDuration(data.elapsed_seconds))}if(progress)parts.push(progress);if(ledger.records)parts.push(`${ledger.records.toLocaleString()} repair records`);if(ledger.latest_update){let stamp=new Date(ledger.latest_update);if(!Number.isNaN(stamp.valueOf()))parts.push(`last update ${stamp.toLocaleTimeString()}`)}panel.textContent=parts.join(' · ');panel.hidden=false}
async function refreshSystemStatus(){try{let responses=await Promise.all([fetch('/api/summary',{headers:{Accept:'application/json'}}),fetch('/api/scheduler/status',{headers:{Accept:'application/json'}})]);if(responses[0].ok){let summary=await responses[0].json(),schedulerResponse=responses[1].ok?await responses[1].json():{};renderSystemStatus({...summary,scheduler:schedulerResponse})}}catch(_error){}window.setTimeout(refreshSystemStatus,3000)}
async function refreshRebuildStatus(){try{let response=await fetch('/api/rebuild-status',{headers:{Accept:'application/json'}});if(response.ok)renderRebuildStatus(await response.json())}catch(_error){}}
async function refreshRuntimeState(){try{let response=await fetch('/api/runtime',{headers:{Accept:'application/json'}});if(response.ok)applyRuntimeState(await response.json())}catch(_error){}await refreshRebuildStatus();if(viewMode==='settings')await window.picorgRefreshSchedulerSettings?.();window.setTimeout(refreshRuntimeState,3000)}
const originalAssignModalImage=assignModalImage;
assignModalImage=async function(){if(!document.body.classList.contains('read-only'))return originalAssignModalImage();let path=modalPaths[modalIndex],typedIdentity=document.querySelector('#modalNewIdentity')?.value.trim()||'',identity=typedIdentity||document.querySelector('#modalIdentity')?.value.trim()||document.querySelector('#identity')?.value.trim()||'';if(!path||!identity){alert('Choose an identity or enter a new identity name');return}let family=document.querySelector('#modalFamily')?.value||'review',button=document.querySelector('.media-modal-toolbar .approve');if(button)button.disabled=true;try{let endpoint='/api/image-decisions/pending',body={paths:[path],identity,family,status:'confirmed',notes:'Assigned from read-only modal review'};let response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to queue image assignment');rememberIdentityUsed(identity,family);currentClusterImageDecisions[path]={identity,family,status:'queued'};showUndoOption('Queued for later application — '+path,data.move_id,'.media-modal-status');advanceModalPath(path)}catch(error){let status=document.querySelector('.media-modal-status');if(status)status.textContent=error.message;if(button)button.disabled=false}};
"""
HTML_PAGE = HTML_PAGE.replace('</script>', RUNTIME_UI_SCRIPT + '</script>', 1)

# Keep detail observers focused on direct render passes.  Observing the entire
# subtree made a large cluster trigger repeated full-grid scans while each
# thumbnail was inserted, freezing the browser before the reviewer could see
# the detail view.
HTML_PAGE = HTML_PAGE.replace(
    "memberObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "memberObserver.observe(document.querySelector('#detail'),{childList:true});",
)
HTML_PAGE = HTML_PAGE.replace(
    "imageAssignObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "imageAssignObserver.observe(document.querySelector('#detail'),{childList:true});",
)
HTML_PAGE = HTML_PAGE.replace(
    "mediaObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "/* delegated media error handling is installed by the enhancement script */",
)
HTML_PAGE = HTML_PAGE.replace(
    "const reviewLayoutObserver=new MutationObserver(placeReviewForm);reviewLayoutObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "const reviewLayoutObserver=new MutationObserver(placeReviewForm);reviewLayoutObserver.observe(document.querySelector('#detail'),{childList:true});",
)
HTML_PAGE = HTML_PAGE.replace(
    "detailEnhancementObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "detailEnhancementObserver.observe(document.querySelector('#detail'),{childList:true});",
)
HTML_PAGE = HTML_PAGE.replace(
    '<img src="/media?path=${encodedPath}" loading="lazy" title=',
    '<img data-src="/media?path=${encodedPath}" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" loading="lazy" decoding="async" title=',
)


# The review page is intentionally assembled from small replacement scripts so
# the existing UI remains backwards-compatible while these safety/operability
# affordances can evolve independently.
HTML_PAGE = HTML_PAGE.replace(
    '<head><meta charset="utf-8">',
    '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
    1,
)
HTML_PAGE = HTML_PAGE.replace(
    '</style></head><body><main>',
    '</style></head><body><div id="runtimeBanner" class="runtime-banner" role="status" hidden></div><div id="rebuildStatus" class="rebuild-status" role="status" hidden></div><div id="systemStatus" class="system-status" role="status" aria-live="polite"><b>System status</b><span>Loading status…</span></div><main>',
    1,
)

UI_ENHANCEMENT_CSS = r"""
html,body{max-width:100%;overflow-x:hidden}
  .runtime-banner{position:sticky;top:0;z-index:20;padding:10px 14px;background:#7a2e00;color:#fff;font-weight:700;text-align:center}
  .system-status{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 14px;background:#eef3f7;border-bottom:1px solid #c8d2dc;color:#26323d;font-size:12px}.system-status b{font-size:13px}.system-status small{color:#65727d}.system-status code{font-size:11px}.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#2f8f46;margin-right:4px}.status-warn{color:#9b4d00;font-weight:700}
.rebuild-status{position:sticky;top:42px;z-index:19;padding:7px 14px;background:#173b59;color:#fff;font-size:13px;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.18)}
.quick-help{margin:12px 0;padding:8px 10px;background:#2d343c;border:1px solid #46505a;border-radius:6px;color:#dbe4ec}.quick-help summary{cursor:pointer;font-weight:700}.quick-help ol{margin:8px 0 2px;padding-left:20px;font-size:12px;line-height:1.5}.side>label{display:block;margin-top:12px;font-weight:600}.side>#filter,.side>#clusterMode{display:block;width:100%;box-sizing:border-box;margin:6px 0 10px}.side>#nextPage{width:100%;margin-top:8px}
  body.read-only .cluster-actions,body.read-only .image-assignment button,body.read-only .form button[type=submit],body.read-only #accuracyTools button,body.read-only #memberTools,body.read-only .undo-move{display:none!important}
  body.read-only input,body.read-only select,body.read-only textarea{opacity:.85}
.detail{min-width:0}
.selection-summary{font-weight:600;color:#27313a;margin-right:auto}
.risk-warning{margin:8px 0;padding:10px 12px;border:1px solid #d58b22;border-radius:6px;background:#fff4d6;color:#5d3d00;font-weight:600}
.attention-list{display:grid;gap:8px}
.attention-card{background:#fff;border:1px solid #d4dbe2;border-left:4px solid #d58b22;border-radius:6px;padding:10px;overflow-wrap:anywhere}
.attention-card .attention-meta{display:flex;gap:8px;flex-wrap:wrap;color:#59636d;font-size:12px}
.attention-card code{display:block;margin-top:5px;white-space:pre-wrap;overflow-wrap:anywhere}
.attention-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
.attention-toolbar select{max-width:100%}
.history{margin-top:8px;padding:8px;border:1px solid #ccd3da;border-radius:6px;background:#fff;max-height:260px;overflow:auto}
.history-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px;padding:7px 0;border-bottom:1px solid #e3e7eb}
.history-row:last-child{border-bottom:0}
.history-row code{font-size:11px;overflow-wrap:anywhere}
.identity-aliases{display:block;color:#77828d;white-space:normal;overflow-wrap:anywhere}
.scan-progress{font-size:12px;color:#59636d}
.settings-list{display:block}.settings-card,.settings-panel{background:#fff;border:1px solid #dce2e7;border-radius:8px;padding:14px;line-height:1.5}.settings-panel{max-width:900px}.settings-panel label{display:block;margin:12px 0}.settings-panel input[type=number]{width:110px}.settings-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.scheduler-status{display:flex;flex-direction:column;gap:4px;padding:10px;background:#eef5fa;border:1px solid #c9d9e6;border-radius:6px}.scheduler-status small{overflow-wrap:anywhere}.assignment-queue{display:grid;gap:6px;max-height:320px;overflow:auto}.queue-row{display:grid;grid-template-columns:auto minmax(120px,auto) auto minmax(0,1fr) auto;gap:8px;align-items:center;padding:7px;border:1px solid #dce2e7;border-radius:5px}.queue-row small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.media-modal{overflow:auto;align-items:flex-start;padding:12px;box-sizing:border-box}
.media-modal-content{width:min(95vw,1100px);max-height:calc(100vh - 24px);overflow:auto;padding-bottom:8px}
.media-modal-toolbar{position:sticky;bottom:0;z-index:5;padding:10px;background:rgba(32,37,43,.96);border-radius:6px}
.media-modal-toolbar button,.media-modal-toolbar select,.media-modal-toolbar input{min-height:38px}
@media(max-width:760px){
  main{display:block;min-height:0}
  .side{position:static;max-height:none;padding:12px}
  .detail{height:auto;max-height:none;overflow:visible;padding:12px}
  .cluster-grid{grid-template-columns:1fr}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .grid img,.grid video{height:140px}
  .media-modal{padding:8px}
  .media-modal-content{width:100%;max-height:calc(100vh - 16px)}
  .media-modal-toolbar{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
  .media-modal-toolbar select,.media-modal-toolbar input,.media-modal-toolbar .approve{grid-column:span 2;max-width:none;width:100%;box-sizing:border-box}
  .media-modal-toolbar button{min-width:0;width:100%}
}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', UI_ENHANCEMENT_CSS + '</style>', 1)

UI_POLISH_CSS = r"""
:root{color-scheme:light;--ink:#17212b;--muted:#667584;--line:#d8e0e7;--panel:#fff;--panel-soft:#f5f8fb;--nav:#101923;--nav-soft:#1b2a39;--accent:#176b87;--accent-soft:#e4f3f7;--good:#19734a;--warn:#a86500;--danger:#a33d43;--shadow:0 10px 30px rgba(29,48,65,.08)}
body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:#edf2f6;line-height:1.45}
body:before{content:"";display:block;height:4px;background:linear-gradient(90deg,#176b87,#49a8a0 55%,#e0a94f)}
main{grid-template-columns:minmax(300px,360px) minmax(0,1fr);min-height:calc(100vh - 4px);gap:0}
.side{background:linear-gradient(180deg,var(--nav),#172532 72%,#1c2f3e);padding:24px 18px;color:#f5f8fb;box-shadow:8px 0 24px rgba(18,34,48,.12);z-index:2}
.side h1{font-size:22px;letter-spacing:-.02em;margin:0 0 4px}.side h1:after{content:"Review workspace";display:block;color:#9eb4c3;font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;margin-top:5px}
.side label{color:#bed0dc}.side input,.side select{background:#243747;border-color:#466072;color:#f4f8fb}.side input::placeholder{color:#9cb0bd}
.view-tabs{display:grid!important;grid-template-columns:repeat(2,1fr);gap:6px;margin:20px 0 12px!important}.view-tabs button{border:1px solid #466072;border-radius:7px;background:transparent;color:#dce8ef;font-weight:650;transition:background .15s,border-color .15s}.view-tabs button:hover{background:#294354;border-color:#7faabd}.view-tabs button.active{background:#bde8ef!important;border-color:#bde8ef!important;color:#123545!important}
.quick-help{background:rgba(255,255,255,.07);border-color:#466072;color:#dce8ef}.quick-help summary{color:#dce8ef}
#summary{padding:10px 11px;border:1px solid #385265;border-radius:8px;background:rgba(255,255,255,.055);color:#b7cad6!important;font-size:12px}
#accuracyTools{margin-top:14px;border:1px solid #385265;background:rgba(255,255,255,.055);border-radius:9px}.accuracy-tools button{background:#294354;border:1px solid #527186;color:#e6f2f7;border-radius:6px}.accuracy-tools button:hover{background:#35586b}.accuracy-tools pre{color:#acc3cf}
.cluster-grid{gap:10px}.cluster{grid-template-columns:72px minmax(0,1fr);gap:10px;background:rgba(255,255,255,.06);border-color:#385265;border-radius:9px;padding:9px;transition:transform .15s,border-color .15s,background .15s}.cluster:hover,.cluster.active{background:#294354;border-color:#8bc9d8;transform:translateY(-1px)}.cluster-thumb{width:72px;height:72px}.cluster-title{font-weight:700;color:#f4f8fb}.cluster small{color:#aec1cd}.cluster-actions{margin-top:7px}.cluster-actions button{border-radius:6px;border:1px solid transparent;font-weight:650}.approve{background:#258257}.approve:hover{background:#309967}.reject{background:#a6474d}.reject:hover{background:#c05a60}
.detail{padding:34px clamp(18px,4vw,58px);max-width:none;background:#edf2f6}.detail>div{max-width:1180px;margin:0 auto}.detail h2{font-size:clamp(22px,3vw,32px);letter-spacing:-.025em;margin:0 0 14px;color:#172a38}.detail h2:before{content:"REVIEW";display:block;color:var(--accent);font-size:11px;letter-spacing:.14em;margin-bottom:7px}
.meta,.form,.settings-card,.settings-panel{border:1px solid var(--line);border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}.meta{padding:15px 17px}.form{max-width:none;padding:18px}.form label{color:#425364;font-weight:650}.form input,.form select,.form textarea{border:1px solid #c5d1da;border-radius:7px;background:#fbfdfe}.form textarea{min-height:80px}.form button,.identity-toolbar button,.image-assignment button{border:1px solid #b6c7d2;border-radius:7px;background:#f4f8fa;color:#234251;font-weight:650}.form button:hover,.identity-toolbar button:hover{background:var(--accent-soft);border-color:#80b9c7}
.grid{gap:12px}.media-tile{background:#fff;border:1px solid var(--line);border-radius:10px;padding:6px;box-shadow:0 4px 14px rgba(29,48,65,.06);overflow:hidden;min-width:0;content-visibility:auto;contain:layout paint style;contain-intrinsic-size:220px 230px}.grid img,.grid video{height:175px;border-radius:7px}.image-assignment{padding:4px 2px 0;color:#35505f}.image-assignment b{color:var(--accent)}.select-control{border-radius:6px!important;background:rgba(13,27,37,.84)!important}
.identity-card{background:#fff;border:1px solid var(--line);border-radius:10px;color:var(--ink);box-shadow:0 4px 14px rgba(29,48,65,.05);transition:border-color .15s,transform .15s}.identity-card:hover,.identity-card.active{border-color:#55a8b9;transform:translateY(-1px)}.identity-card small{color:var(--muted)}.identity-family{color:var(--accent)}
.identity-toolbar,.settings-actions,.attention-toolbar{padding:10px 0;gap:8px}.selection-summary{color:#234251}.risk-warning{border-radius:9px;box-shadow:0 3px 10px rgba(168,101,0,.08)}
.system-status{padding:9px clamp(14px,3vw,40px);background:#fff;border-color:var(--line);color:#314552}.system-status span{padding:3px 0}.rebuild-status{top:40px}
.media-modal{backdrop-filter:blur(5px);background:rgba(8,20,29,.82)}.media-modal-content{background:#101923;border:1px solid #4d6879;border-radius:12px;padding:10px;box-shadow:0 18px 60px rgba(0,0,0,.35);width:min(96vw,1100px);height:min(96vh,900px);max-height:calc(100vh - 20px);box-sizing:border-box;overflow:auto;display:flex;flex-direction:column;align-items:center}.media-preview-frame{position:relative;display:flex;align-items:center;justify-content:center;max-width:100%;max-height:calc(100vh - 190px);flex:0 1 auto}.media-modal-content img,.media-modal-content video{width:auto;height:auto;max-width:100%;max-height:calc(100vh - 190px);object-fit:contain;flex:0 1 auto}.media-deleted-overlay,.media-queued-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-size:clamp(20px,4vw,48px);font-weight:800;letter-spacing:.04em;text-transform:uppercase;text-shadow:0 2px 4px #000;pointer-events:none}.media-deleted-overlay{background:rgba(150,0,0,.62)}.media-queued-overlay{background:rgba(176,112,0,.58);font-size:clamp(18px,3vw,36px)}.media-tile>a{position:relative;display:block}.media-tile .media-queued-overlay{font-size:16px}.media-modal-toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-top:0;width:100%;flex:0 0 auto}.media-modal-toolbar button,.media-modal-toolbar select,.media-modal-toolbar input{border-radius:7px;border:1px solid #688292}.media-modal-status{color:#c7d8e1;margin-top:8px;font-size:12px;flex:0 0 auto}.media-modal-caption{padding:4px 2px;color:#c7d8e1;max-width:100%;flex:0 0 auto}@media(max-width:760px){.media-modal-content{height:calc(100vh - 16px);padding:8px}.media-preview-frame,.media-modal-content img,.media-modal-content video{max-height:calc(100vh - 250px)}.media-modal-caption{font-size:11px}}
.empty-state{max-width:620px;margin:12vh auto;padding:38px 32px;text-align:center;border:1px solid var(--line);border-radius:16px;background:#fff;box-shadow:var(--shadow)}.empty-state h2:before{display:none}.empty-state p{color:var(--muted)}
@media(max-width:760px){body:before{height:3px}main{display:block}.side{padding:18px 14px;box-shadow:none}.detail{padding:22px 12px}.detail h2{font-size:24px}.grid img,.grid video{height:145px}.system-status{font-size:11px;gap:7px}.system-status small{width:100%}.view-tabs{grid-template-columns:repeat(2,1fr)!important}}
@media(min-width:761px){.side{position:sticky;top:4px;height:calc(100vh - 4px);box-sizing:border-box;overflow-y:auto}.detail{min-height:calc(100vh - 4px)}}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', UI_POLISH_CSS + '</style>', 1)

MODAL_FIT_CSS = r"""
.media-modal{overflow:hidden;align-items:center}.media-modal-content{height:min(96vh,900px);height:min(96dvh,900px);max-height:calc(100vh - 16px);max-height:calc(100dvh - 16px);min-height:0;overflow:hidden}
.media-preview-frame{width:100%;max-height:none;min-height:0;flex:1 1 0}.media-modal-content img,.media-modal-content video{width:auto;height:auto;max-width:100%;max-height:100%;min-height:0;object-fit:contain}
.media-modal-toolbar{position:static;flex:0 0 auto}.media-modal-caption{overflow:hidden;overflow-wrap:anywhere;max-height:2.8em}.modal-recent-identities{display:flex;align-items:center;gap:6px;flex:0 0 100%;max-width:100%;overflow-x:auto;overflow-y:hidden;white-space:nowrap;padding:2px 0}.modal-recent-identities span{font-size:12px;color:#c7d8e1;flex:0 0 auto}.modal-recent-identities button{flex:0 0 auto;min-width:0;padding:6px 10px}.modal-recent-identities button[aria-pressed="true"]{outline:2px solid #8bc9d8}
@media(max-width:760px){.media-modal-content{height:calc(100vh - 16px);height:calc(100dvh - 16px);max-height:calc(100vh - 16px);max-height:calc(100dvh - 16px);width:100%}.media-preview-frame{width:100%;flex:1 1 0;min-height:0}.media-modal-toolbar{max-height:45vh;max-height:45dvh;overflow-y:auto}.media-modal-content img,.media-modal-content video{max-width:100%;max-height:100%}}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', MODAL_FIT_CSS + '</style>', 1)

UI_ENHANCEMENT_SCRIPT = r"""
(function(){
  const originalShowView=showView;
  const originalSelect=select;
  const originalSelectIdentity=selectIdentity;
  let identityScope='active';
  let suppressClusterRerender=false;
  const existingRenderList=renderList;
  renderList=function(){
    if(suppressClusterRerender&&viewMode==='clusters'){
      document.querySelectorAll('.cluster').forEach(card=>card.classList.toggle('active',card.getAttribute('onclick')?.includes(`'${selected}'`)));
      return;
    }
    existingRenderList();
  };

  // The legacy media observer rescanned the entire grid after every DOM
  // mutation.  That becomes quadratic when a large cluster is loaded.  Use a
  // single delegated error handler instead; modal/video handling remains
  // independent of thumbnail rendering.
  try{
    mediaObserver.disconnect();
    document.querySelector('#detail')?.addEventListener('error',event=>{
      const target=event.target;
      if(target instanceof HTMLImageElement||target instanceof HTMLVideoElement)target.closest('.media-tile')?.remove();
    },true);
  }catch(_error){}

  const lazyMediaObserver=new IntersectionObserver(entries=>{
    for(const entry of entries){
      if(!entry.isIntersecting)continue;
      const media=entry.target, source=media.dataset.src;
      if(source){media.src=source;delete media.dataset.src}
      lazyMediaObserver.unobserve(media);
    }
  },{rootMargin:'500px 0px'});
  function observeLazyMedia(root=document){
    root.querySelectorAll?.('[data-src]').forEach(media=>lazyMediaObserver.observe(media));
  }
  window.picorgObserveLazyMedia=observeLazyMedia;
  observeLazyMedia();
  const lazyDetailObserver=new MutationObserver(records=>{
    for(const record of records)for(const node of record.addedNodes){
      if(node.nodeType===1)observeLazyMedia(node);
    }
  });
  lazyDetailObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});

  // Render in small batches so the browser can paint and process input between
  // groups.  All paths are still loaded automatically; this only prevents a
  // large cluster from monopolizing the renderer in one task.
  loadClusterImages=async function(){
    const grid=document.querySelector('#detail .grid');
    if(!grid||!selected)return;
    const generation=++loadGeneration;
    grid.dataset.loaded='0';
    grid.innerHTML='<p class="muted">Loading all cluster images…</p>';
    try{
      const response=await fetch('/api/clusters/'+selected,{headers:{Accept:'application/json'}}), x=await response.json();
      if(!response.ok)throw new Error(x.error||`Request failed (${response.status})`);
      if(generation!==loadGeneration)return;
      currentClusterImageDecisions=x.image_decisions||{};currentClusterAssignment=x.decision||null;
      const paths=Array.isArray(x.paths)?x.paths:[];grid.innerHTML='';
      for(let offset=0;offset<paths.length;offset+=24){
        if(generation!==loadGeneration)return;
        grid.insertAdjacentHTML('beforeend',paths.slice(offset,offset+24).map(path=>mediaTile(path,currentClusterImageDecisions[path]||currentClusterAssignment)).join(''));
        observeLazyMedia(grid);
        grid.dataset.loaded=String(Math.min(offset+24,paths.length));
        // A zero-delay timer is more reliable than requestAnimationFrame in
        // headless/remote Chromium, where a background tab may not receive a
        // frame callback at all.  Yielding still lets input and paint run
        // between batches without leaving the loader stuck at 24 images.
        await new Promise(resolve=>setTimeout(resolve,0));
      }
      if(!paths.length)grid.innerHTML='<p class="muted">No images in this cluster.</p>';
    }catch(error){
      if(generation===loadGeneration)grid.innerHTML=`<p role="alert">${esc(error.message)} <button type="button" onclick="loadClusterImages()">Retry</button></p>`;
    }
  };

  // Replace the initial sample with the complete gallery after a cluster is
  // opened. loadClusterImages renders in animation-frame batches.
  async function loadSelectedClusterImages(){
    await loadClusterImages();
  }
  select=async function(id){
    suppressClusterRerender=true;
    try{
      const result=await originalSelect(id);
      if(selected===id)await loadSelectedClusterImages();
      return result;
    }finally{
      suppressClusterRerender=false;
      document.querySelectorAll('.cluster').forEach(card=>card.classList.toggle('active',card.getAttribute('onclick')?.includes(`'${selected}'`)));
    }
  };

  // Keep the common assignment path short: select images, press Assign,
  // supply an identity if the picker is empty, then explicitly confirm.  The
  // existing writer remains authoritative; this wrapper only supplies the
  // missing identity and confirmation step.
  const originalAssignSelectedImages=assignSelectedImages;
  async function advanceClusterIfHandled(){
    if(viewMode!=='clusters'||!selected)return false;
    const completedId=selected,previousIds=visibleClusterIds(),previousIndex=previousIds.indexOf(completedId);
    const known=clusters.find(item=>item.cluster_id===completedId);
    if(!known?.title)return false;
    try{
      const query=new URLSearchParams({page:'1',page_size:'200',mode:clusterMode,hide_confirmed:hideConfirmed?'1':'0',q:known.title});
      const response=await fetch(`/api/clusters?${query}`,{headers:{Accept:'application/json'}}),payload=await response.json();
      if(!response.ok)throw new Error(payload.error||`Request failed (${response.status})`);
      const current=(payload.clusters||[]).find(item=>item.cluster_id===completedId);
      if(current){Object.assign(known,current);renderList()}
      if(current&&Number(current.unassigned_count)>0)return false;
    }catch(error){console.warn('Could not verify cluster completion before advancing',error);return false}
    if(selected!==completedId||viewMode!=='clusters')return false;
    await refreshVisibleClusters();
    if(document.querySelector('#list [role="alert"]'))return false;
    let ids=visibleClusterIds();
    let target=previousIds.slice(Math.max(0,previousIndex+1)).find(id=>ids.includes(id));
    if(!target&&ids.includes(completedId))target=ids[ids.indexOf(completedId)+1];
    if(!target){const targetIndex=ids.includes(completedId)?ids.indexOf(completedId)+1:Math.max(previousIndex,0);while(hasNext&&ids.length<=targetIndex){const count=ids.length;await loadClustersPage(false);ids=visibleClusterIds();if(ids.length===count)break}target=ids[targetIndex]}
    if(!target){closeMediaModal();showQueuedMoveStatus('Cluster handled — no more clusters remain in this view.');return true}
    closeMediaModal();
    await select(target);
    return true;
  }
  window.advanceClusterIfHandled=advanceClusterIfHandled;
  async function openNextUnassignedClusterImage(excludedPaths){
    if(viewMode!=='clusters'||!selected)return;
    const gridPaths=gridMediaPaths(),excluded=new Set(excludedPaths);
    const unfinished=path=>!['queued','confirmed'].includes(currentClusterImageDecisions[path]?.status);
    const failed=excludedPaths.filter(path=>gridPaths.includes(path)&&unfinished(path));
    const paths=[...new Set([...failed,...gridPaths.filter(path=>!excluded.has(path)&&unfinished(path))])];
    if(!paths.length){if(await advanceClusterIfHandled())return;closeMediaModal();showQueuedMoveStatus('Could not confirm that this cluster is complete. It remains selected for review.');return}
    modalPaths=paths;
    modalIndex=0;
    renderMediaModal();
  }
  const originalAdvanceModalPath=advanceModalPath;
  advanceModalPath=function(path,completionMessage){
    originalAdvanceModalPath(path,completionMessage);
    if(viewMode==='clusters'&&!modalPaths.length)window.setTimeout(async()=>{
      if(await advanceClusterIfHandled())return;
      const status=document.querySelector('.media-modal-status');
      if(status)status.textContent='Could not confirm that this cluster is complete. It remains selected for review.';
    },0);
  };
  assignSelectedImages=async function(){
    const paths=[...document.querySelectorAll('#detail .imageSelect:checked')].map(input=>input.dataset.path).filter(Boolean);
    if(!paths.length){alert('Select at least one image first');return}
    let identity=document.querySelector('#newIdentity')?.value.trim()||document.querySelector('#identity')?.value.trim()||'';
    if(!identity)identity=window.prompt(`Assign ${paths.length} selected image${paths.length===1?'':'s'} to identity:`,'');
    identity=identity?.trim()||'';
    if(!identity)return;
    const queued=document.body.classList.contains('read-only');
    const action=queued?'queue for application after the rebuild':'confirm and move';
    if(!window.confirm(`${action.charAt(0).toUpperCase()+action.slice(1)} ${paths.length} image${paths.length===1?'':'s'} to “${identity}”?`))return;
    const typed=document.querySelector('#newIdentity');
    const picker=document.querySelector('#identity');
    if(picker&&!picker.value&&typed)typed.value=identity;
    if(typed?.value.trim()&&!queued){
      const family=document.querySelector('#family')?.value||'review';
      try{
        const {response,data,cancelled,payload}=await postNewIdentityWithPrompt('/api/identities',{canonical:identity,family,paths,notes:document.querySelector('#notes')?.value||'Created and assigned from cluster review'});
        if(cancelled)return;
        if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
        identity=payload.canonical;
        rememberIdentityUsed(identity,family);lastMoveId=data.move_id||lastMoveId;
        showUndoOption(`Created ${identity}; moved ${(data.moved||[]).length} image(s)${(data.errors||[]).length?`; ${(data.errors||[]).length} move error(s)`:''}`,data.move_id);
        await select(selected);await openNextUnassignedClusterImage(paths);return {...data,paths};
      }catch(error){showUndoOption(error.message);return}
    }
    const result=await originalAssignSelectedImages();
    if(result?.paths?.length)await openNextUnassignedClusterImage(result.paths);
    return result;
  };
  const originalConfirmImage=confirmImage;
  confirmImage=async function(event,path,identity,family){
    if(!window.confirm(`Confirm this image as “${identity}”?`))return;
    const result=await originalConfirmImage(event,path,identity,family);
    if(result&&!result.needs_review)await openNextUnassignedClusterImage([path]);
    return result;
  };

  function polishWorkspaceChrome(){
    const sideTitle=document.querySelector('.side h1');
    if(sideTitle){
      if(sideTitle.textContent!=='PicOrg review')sideTitle.textContent='PicOrg review';
      sideTitle.setAttribute('title','Face-first media review workspace');
    }
    const filter=document.querySelector('#filter');
    if(filter)filter.setAttribute('aria-label','Search the current review queue');
    const next=document.querySelector('#nextPage');
    if(next){const label=document.querySelector('#assortedTab')?.classList.contains('active')?'Load more assorted folders':document.querySelector('#attentionTab')?.classList.contains('active')?'Load more attention items':'Load more clusters';if(next.textContent!==label)next.textContent=label;next.setAttribute('aria-label',label);}
    const detail=document.querySelector('#detail');
    if(detail&&detail.textContent.includes('Select a cluster')){
      detail.innerHTML='<div class="empty-state"><h2>Choose a review queue</h2><p>Start with face groups, open a cluster, then inspect images individually before assigning an identity.</p><p class="muted">Confirmed media stays hidden by default. Use Needs attention for unreadable or deferred files.</p></div>';
    }
  }
  polishWorkspaceChrome();
  const chromeObserver=new MutationObserver(polishWorkspaceChrome);
  chromeObserver.observe(document.querySelector('.side'),{childList:true,subtree:true});

  function urlState(){
    const params=new URLSearchParams();
    params.set('view',viewMode||'clusters');
    if(viewMode==='clusters'&&selected)params.set('cluster',selected);
    if(viewMode==='identities'&&selectedIdentity)params.set('identity',selectedIdentity);
    const query=document.querySelector('#filter')?.value?.trim();
    if(query)params.set('q',query);
    if(viewMode==='clusters'&&!hideConfirmed)params.set('show_confirmed','1');
    history.replaceState(null,'','?'+params.toString());
  }
  window.picorgWriteUrlState=urlState;

  function updateSelectionSummary(){
    const detail=document.querySelector('#detail'), grid=detail?.querySelector(':scope>.grid'), summary=document.querySelector('#selectionSummary');
    if(!summary||!detail)return;
    const selectedCount=detail.querySelectorAll('.imageSelect:checked').length;
    const loaded=Number(grid?.dataset.loaded||detail.querySelectorAll('.imageSelect').length||0);
    const total=Number(detail.dataset.total||loaded);
    summary.textContent=`${selectedCount} selected / ${total} images${loaded<total?' · '+loaded+' loaded':''}`;
  }
  window.picorgUpdateSelectionSummary=updateSelectionSummary;

  async function loadMoveHistory(){
    const panel=document.querySelector('#moveHistory');
    if(!panel)return;
    panel.innerHTML='<span class="muted">Loading move history…</span>';
    try{
      const response=await fetch('/api/moves',{headers:{Accept:'application/json'}}), data=await response.json();
      if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
      const operations=(data.operations||[]).slice().reverse();
      if(!operations.length){panel.innerHTML='<span class="muted">No file moves recorded yet.</span>';return}
      panel.innerHTML=operations.map(op=>{
        const id=esc(String(op.id||'')), count=(op.moves||[]).length, state=op.undone?'undone':'active';
        return `<div class="history-row"><div><b>${esc(op.identity||'Unknown identity')}</b> · ${count} file(s) · ${state}<br><code title="${id}">audit/move ${id}</code><br><span class="muted">${esc(op.saved_at||'')}</span></div>${op.undone?'':'<button type="button" data-undo-move="'+id+'">Undo</button>'}</div>`;
      }).join('');
      panel.querySelectorAll('[data-undo-move]').forEach(button=>button.addEventListener('click',()=>window.picorgUndoMove(button.dataset.undoMove)));
    }catch(error){panel.innerHTML=`<span role="alert">${esc(error.message)}</span>`}
  }
  window.picorgToggleMoveHistory=function(){
    const panel=document.querySelector('#moveHistory');if(!panel)return;
    panel.hidden=!panel.hidden;if(!panel.hidden)loadMoveHistory();
  };
  window.picorgUndoMove=async function(moveId){
    if(!moveId||!confirm('Undo this assignment or move and restore its files?'))return;
    const response=await fetch('/api/moves/'+encodeURIComponent(moveId)+'/undo',{method:'POST'}), data=await response.json();
    if(!response.ok){alert(data.error||'Undo failed');return}
    lastMoveId=null;await loadMoveHistory();
    const status=document.querySelector('#imageAssignTools p');if(status)status.textContent=`Undid ${data.restored?.length||0} file move(s); move ${moveId}`;
    if(selected)await select(selected);else if(selectedIdentity)await selectIdentity(selectedIdentity);
    return data;
  };

  function addHistoryAndSummary(){
    const detail=document.querySelector('#detail'), grid=detail?.querySelector(':scope>.grid');
    if(!detail||!grid)return;
    const tools=document.querySelector('#imageAssignTools');
    if(tools){
      if(!document.querySelector('#selectionSummary')){
        const summary=document.createElement('span');summary.id='selectionSummary';summary.className='selection-summary';summary.textContent='0 selected / 0 images';
        tools.prepend(summary);
      }
      if(!document.querySelector('#moveHistoryToggle')){
        const button=document.createElement('button');button.id='moveHistoryToggle';button.type='button';button.textContent='Move history';button.onclick=window.picorgToggleMoveHistory;tools.append(button);
        const panel=document.createElement('div');panel.id='moveHistory';panel.className='history';panel.hidden=true;tools.append(panel);
      }
    }
    updateSelectionSummary();
  }
  const detailEnhancementObserver=new MutationObserver(addHistoryAndSummary);
  detailEnhancementObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
  document.addEventListener('change',event=>{if(event.target.matches('.imageSelect'))updateSelectionSummary()});
  selectAllImages=(function(original){return function(){original();updateSelectionSummary()}})(selectAllImages);
  selectNoImages=(function(original){return function(){original();updateSelectionSummary()}})(selectNoImages);

  function showRiskWarning(){
    const detail=document.querySelector('#detail');if(!detail||!selected)return;
    const item=clusters.find(cluster=>cluster.cluster_id===selected);if(!item)return;
    let evidence=detail.querySelector('.cluster-evidence-summary');
    if(!evidence){evidence=document.createElement('div');evidence.className='cluster-evidence-summary';evidence.innerHTML=`Evidence coverage: ${Number(item.count||0)} media · ${(item.expected_identities||[]).length} expected identity labels · ${(item.face_cluster_labels||[]).length} face-cluster labels · ${(item.families||[]).length} source families · ${(item.review_methods||[]).length} review methods.`;detail.querySelector('h2')?.after(evidence)}
    if(item.requires_image_review&&!detail.querySelector('.risk-warning')){const flags=(item.purity_flags||[]).map(flag=>flag.reason||flag.code||flag).filter(Boolean).join('; ');const warning=document.createElement('div');warning.className='risk-warning';warning.setAttribute('role','alert');warning.textContent='Image-level review required — this cluster has purity risks'+(flags?': '+flags:'')+'. These counts describe available labels; they are not a face-similarity score. Do not bulk-confirm it.';evidence.after(warning)}
    if(!detail.querySelector('.compare-references')){const compare=document.createElement('button');compare.type='button';compare.className='compare-references';compare.textContent='Compare with confirmed identity examples';compare.title='Browse confirmed reference images across identities. Suggestions are not similarity scores and do not assign automatically.';compare.addEventListener('click',async()=>{compare.disabled=true;compare.textContent='Loading confirmed examples…';try{await loadIdentityGroups();const expected=new Set(item.expected_identities||[]);const known=identityGroups.filter(group=>!expected.has(group.identity)&&Number(group.confirmed||0)>0&&group.sample_paths?.length).slice(0,80);const panel=detail.querySelector('.compare-reference-panel')||document.createElement('section');panel.className='compare-reference-panel';panel.innerHTML='<b>Confirmed identity examples</b><p class="muted">Browse manually. No similarity scores are available here, and choosing an example does not assign the cluster.</p>';const grid=document.createElement('div');grid.className='compare-reference-grid';for(const group of known){const card=document.createElement('article');card.className='compare-reference-card';const name=document.createElement('b');name.textContent=`${group.identity} · ${group.confirmed} confirmed`;card.append(name);for(const path of group.sample_paths.filter(value=>/\.(bmp|gif|jpe?g|png|webp)$/i.test(value)).slice(0,2)){const link=document.createElement('a');link.href='/media?path='+encodeURIComponent(path);link.title=`Open confirmed example for ${group.identity}`;link.addEventListener('click',event=>openMediaModal(event,link));const image=document.createElement('img');image.loading='lazy';image.alt=`Confirmed example for ${group.identity}`;image.src=link.href;link.append(image);link.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path));card.append(link)}grid.append(card)}panel.append(grid);if(!panel.isConnected)detail.querySelector('.cluster-evidence-summary')?.after(panel);compare.after(panel)}catch(error){compare.textContent=`Unable to load examples: ${error.message}`}finally{compare.disabled=false}});evidence.after(compare)}
  }
  const originalSelectForRisk=select;
  select=async function(id){const result=await originalSelectForRisk(id);showRiskWarning();addHistoryAndSummary();urlState();return result};
  const originalLoadAllForSummary=loadClusterImages;
  loadClusterImages=async function(){const result=await originalLoadAllForSummary();addHistoryAndSummary();updateSelectionSummary();return result};

  function ensureIdentityScope(){
    let scope=document.querySelector('#identityScope');
    if(!scope){scope=document.createElement('select');scope.id='identityScope';scope.setAttribute('aria-label','Identity list scope');scope.innerHTML='<option value="active">Active / assigned</option><option value="registry">MD/RD registry</option><option value="all">All non-generic</option>';scope.value=identityScope;scope.addEventListener('change',()=>{identityScope=scope.value;renderIdentityList()});document.querySelector('#filter')?.before(scope)}
    scope.hidden=viewMode!=='identities';
  }
  function renderIdentityListEnhanced(){
    ensureIdentityScope();
    const query=(document.querySelector('#filter')?.value||'').trim().toLowerCase();
    const baseline=['linked','manual','metadaily','redditdaily','review'];
    const visible=identityGroups.filter(group=>isCuratedIdentity(group)&&(group.identity+' '+(group.aliases||[]).join(' ')).toLowerCase().includes(query)&&(
      identityScope==='all'||(identityScope==='registry'&&baseline.includes(group.family))||(identityScope==='active'&&((group.count||0)>0||['manual','review'].includes(group.family)))
    )).sort((a,b)=>((b.count||0)-(a.count||0))||a.identity.localeCompare(b.identity));
    const list=document.querySelector('#list');list.className='identity-grid';
    document.querySelector('#summary').textContent=`${visible.length} identities · ${visible.reduce((total,group)=>total+(group.count||0),0)} assigned media`;
    list.innerHTML=visible.map(group=>{const encoded=encodeURIComponent(group.identity);return `<button type="button" class="identity-card ${selectedIdentity===group.identity?'active':''}" onclick="selectIdentity(decodeURIComponent('${encoded}'))"><span class="identity-family">${esc(group.family||'review')}</span><b>${esc(group.identity)}</b><small>${group.count||0} media · ${group.confirmed||0} approved · ${group.pending||0} pending · ${group.rejected||0} rejected</small></button>`}).join('')||'<p class="muted">No identities match this scope/search.</p>';
  }
  renderIdentityList=renderIdentityListEnhanced;
  const originalSelectIdentityEnhanced=selectIdentity;
  selectIdentity=async function(identity){
    const result=await originalSelectIdentityEnhanced(identity);ensureIdentityScope();
    const group=identityGroups.find(item=>item.identity===identity), detail=document.querySelector('#detail');
    if(group&&detail){detail.dataset.total=String(group.count||0);const grid=detail.querySelector(':scope>.grid');if(grid)grid.dataset.loaded=String(grid.querySelectorAll('.imageSelect').length);const heading=detail.querySelector('h2');if(heading&&!detail.querySelector('.identity-provenance')){const provenance=document.createElement('div');provenance.className='identity-provenance muted';provenance.innerHTML=`<b>${esc(group.family==='review'?'Provisional review identity':group.family==='manual'?'Local manual identity':'Registry identity')}</b><br>${(group.aliases||[]).length?`Aliases: ${esc(group.aliases.join(', '))}`:'No aliases recorded'}<br>${Number(group.count||0)} unique known paths across decisions, markers, queues, clusters, and the sorted-folder scan. This is configured PicOrg evidence, not a completeness guarantee.`;heading.after(provenance)}if(hideConfirmed&&(group.confirmed||0)>0&&!detail.querySelector('.hidden-confirmed-note')){const note=document.createElement('div');note.className='hidden-confirmed-note muted';note.textContent=`${group.confirmed} confirmed images hidden — Show confirmed`;const button=document.createElement('button');button.type='button';button.textContent='Show confirmed';button.onclick=toggleHideConfirmed;note.append(' ',button);detail.querySelector('h2')?.after(note)}}
    addHistoryAndSummary();urlState();return result;
  };

  function addAttentionTab(){
    const tabs=document.querySelector('.view-tabs');if(tabs&&!document.querySelector('#attentionTab')){const button=document.createElement('button');button.id='attentionTab';button.type='button';button.textContent='Needs attention';button.onclick=()=>window.picorgNavigate('attention');tabs.append(button)}
  }
  function addSettingsTab(){
    const tabs=document.querySelector('.view-tabs');if(tabs&&!document.querySelector('#settingsTab')){const button=document.createElement('button');button.id='settingsTab';button.type='button';button.textContent='Settings / pipeline';button.onclick=()=>window.picorgNavigate('settings');tabs.append(button)}
  }
  function addReviewQueueTab(){
    const tabs=document.querySelector('.view-tabs');if(tabs&&!document.querySelector('#reviewQueueTab')){const button=document.createElement('button');button.id='reviewQueueTab';button.type='button';button.textContent='Review queue';button.onclick=()=>window.picorgNavigate('review-queue');tabs.append(button)}
  }
  function enhanceSettingsWorkflow(){
    const card=document.querySelector('.settings-list .settings-card');
    if(card&&!card.querySelector('.recommended-workflow')){
      const workflow=document.createElement('div');workflow.className='recommended-workflow';workflow.innerHTML='<b>Recommended workflow</b><ol><li>Build the canonical face baseline after MD/RD or organized identities change.</li><li>Run the full cycle for daily ingest, confirmed migration, and new-content matching.</li><li>Review only the remaining unconfirmed clusters; assignments remain queued safely during rebuilds.</li></ol><small>MD/RD media and registries are read-only inputs.</small>';
      card.prepend(workflow);
    }
    const form=document.querySelector('#schedulerForm');
    if(form&&!document.querySelector('#updateBaseline')){
      const label=document.createElement('label');label.innerHTML='<input id="updateBaseline" type="checkbox" checked> Refresh canonical face baseline during each cycle (incremental; protected sources stay read-only)';
      form.querySelector('.settings-actions')?.before(label);
    }
    const actions=document.querySelector('.settings-actions');
    if(actions&&!actions.querySelector('[data-job="canonical_baseline"]')){
      const button=document.createElement('button');button.type='button';button.dataset.job='canonical_baseline';button.textContent='Build canonical face baseline';button.onclick=()=>launchScheduler('run','canonical_baseline').catch(error=>alert(error.message));
      actions.insertBefore(button,actions.firstElementChild?.nextElementSibling||null);
    }
    const panel=document.querySelector('#detail .settings-panel');
    if(panel&&!document.querySelector('#identityPickerSettings')){
      const section=document.createElement('section');section.id='identityPickerSettings';section.className='settings-card';
      section.innerHTML='<h3>Generic identity picker</h3><p class="muted">Choose identity name prefixes shown in the image picker. Separate prefixes with commas or new lines.</p><label for="identityPickerPrefixes">Identity prefixes</label><textarea id="identityPickerPrefixes" rows="4" style="width:100%;box-sizing:border-box"></textarea><div class="picker-settings-actions"><button type="button" onclick="saveIdentityPickerSettings().catch(error=>{document.querySelector(\'#identityPickerSettingsStatus\').textContent=error.message})">Save picker prefixes</button><span id="identityPickerSettingsStatus" role="status"></span></div>';
      panel.append(section);loadIdentityPickerSettings().catch(error=>{const status=document.querySelector('#identityPickerSettingsStatus');if(status)status.textContent=error.message});
    }
  }
  function schedulerStatusText(data){
    const status=data?.status||{};const config=data?.config||{};
    const active=data?.running?`Running ${status.job||'job'} · ${status.stage||'starting'} · PID ${status.pid||'?'}`:'No active job';
    const daemon=data?.daemon_running?'Scheduler daemon running':'Scheduler daemon stopped';
    const schedule=config.enabled?`enabled every ${config.interval_minutes} minutes`:'disabled';
    const next=status.next_run_at?` · next ${new Date(status.next_run_at).toLocaleString()}`:'';
    const run=status.run_id?` · run ${status.run_id}`:'';
    return `${active}${run} · ${daemon} · ${schedule}${next}`;
  }
  async function loadSchedulerSettings(){
    const panel=document.querySelector('#schedulerStatus');if(!panel)return;
    try{const response=await fetch('/api/scheduler/status',{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
      const config=data.config||{};for(const id of ['scheduleEnabled','runIngest','updateBaseline','checkEvidence','rebuildFaces','migrateConfirmed','applyHighConfidence']){const el=document.querySelector('#'+id);if(el)el.checked=Boolean(config[{scheduleEnabled:'enabled',runIngest:'run_ingest',updateBaseline:'update_baseline',checkEvidence:'check_evidence',rebuildFaces:'rebuild_faces',migrateConfirmed:'migrate_confirmed',applyHighConfidence:'apply_high_confidence'}[id]])}const interval=document.querySelector('#scheduleInterval');if(interval)interval.value=config.interval_minutes||360;
      const evidence=data.evidence_health||{};const evidenceText=evidence.available?`Evidence DB: ${evidence.healthy?'healthy':'needs attention'} · SQLite ${evidence.runtime_sqlite||'unknown'}${evidence.backup?' · backup written':''}`:'Evidence DB health has not run';
      panel.innerHTML=`<b>${esc(schedulerStatusText(data))}</b><small>${esc(data.status?.stage||'Waiting for output')}</small><small>${esc((data.status?.output_tail||[]).slice(-1)[0]||'No recent progress output')}</small><small>${esc(evidenceText)}</small>`;
      document.querySelectorAll('.settings-actions button').forEach(button=>{if(button.textContent!=='Stop active job')button.disabled=Boolean(data.running)});
    }catch(error){panel.innerHTML=`<span role="alert">${esc(error.message)}</span>`}
  }
  window.picorgRefreshSchedulerSettings=loadSchedulerSettings;
  async function saveSchedulerSettings(){
    const body={enabled:document.querySelector('#scheduleEnabled')?.checked,interval_minutes:Number(document.querySelector('#scheduleInterval')?.value||360),run_ingest:document.querySelector('#runIngest')?.checked,update_baseline:document.querySelector('#updateBaseline')?.checked,check_evidence:document.querySelector('#checkEvidence')?.checked,rebuild_faces:document.querySelector('#rebuildFaces')?.checked,migrate_confirmed:document.querySelector('#migrateConfirmed')?.checked,apply_high_confidence:document.querySelector('#applyHighConfidence')?.checked};
    const response=await fetch('/api/scheduler/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to save scheduler settings');document.querySelector('#schedulerStatus').textContent='Settings saved; '+(data.config.enabled?'automatic cycle enabled':'automatic cycle disabled');await loadSchedulerSettings();
  }
  window.saveSchedulerSettings=saveSchedulerSettings;
  async function loadIdentityPickerSettings(){
    const input=document.querySelector('#identityPickerPrefixes');if(!input)return;
    const response=await fetch('/api/identity-picker/settings',{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to load picker prefixes');
    input.value=(data.prefixes||[]).join('\n');window.picorgSetIdentityPickerPrefixes?.(data.prefixes||[]);
  }
  async function saveIdentityPickerSettings(){
    const input=document.querySelector('#identityPickerPrefixes'),status=document.querySelector('#identityPickerSettingsStatus');if(!input)return;
    const prefixes=input.value.split(/[\n,]+/).map(value=>value.trim().toLowerCase()).filter(Boolean);
    const response=await fetch('/api/identity-picker/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prefixes})}),data=await response.json();
    if(!response.ok)throw new Error(data.error||'Unable to save picker prefixes');
    input.value=(data.prefixes||[]).join('\n');window.picorgSetIdentityPickerPrefixes?.(data.prefixes||[]);if(status)status.textContent='Saved '+data.prefixes.length+' picker prefixes.';
  }
  window.saveIdentityPickerSettings=saveIdentityPickerSettings;
  async function launchScheduler(command,job){
    const response=await fetch('/api/scheduler/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,job})}),data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to launch pipeline step');await loadSchedulerSettings();
  }
  window.launchScheduler=launchScheduler;
  async function stopScheduler(){const response=await fetch('/api/scheduler/stop',{method:'POST'}),data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to stop scheduler');await loadSchedulerSettings()}
  window.stopScheduler=stopScheduler;
  async function loadAssignmentQueue(){const panel=document.querySelector('#assignmentQueue');if(!panel)return;panel.textContent='Loading queued assignments…';try{const response=await fetch('/api/assignment-queue?status=pending,applying,error,conflict',{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);const items=data.assignments||[];panel.innerHTML=items.length?items.map(item=>`<div class="queue-row"><code>${esc(item.assignment_id)}</code><b>${esc(item.identity)}</b><span>${esc(item.status)}</span><small>${esc(item.path)}</small>${item.status==='pending'?`<button type="button" onclick="rejectQueuedAssignment(${Number(item.assignment_id)})">Reject</button>`:''}</div>`).join(''):'<span class="muted">No queued assignments.</span>'}catch(error){panel.innerHTML=`<span role="alert">${esc(error.message)}</span>`}}
  async function rejectQueuedAssignment(id){if(!confirm('Reject this queued assignment? The source file will not be changed.'))return;const response=await fetch('/api/assignment-queue/'+encodeURIComponent(id)+'/reject',{method:'POST'}),data=await response.json();if(!response.ok){alert(data.error||'Unable to reject queued assignment');return}await loadAssignmentQueue()}
  window.rejectQueuedAssignment=rejectQueuedAssignment;
  let reviewQueueItems=[];
  let reviewQueueOpenItems=[];
  let reviewQueueModalItems=[];
  let queueSearchTimer=null;
  const queueResumeKey='picorg.review-queue.resume.v1';
  const originalRenderMediaModalForQueue=renderMediaModal;
  renderMediaModal=function(){originalRenderMediaModalForQueue();if(viewMode==='review-queue'){const current=reviewQueueModalItems[modalIndex];if(current)try{sessionStorage.setItem(queueResumeKey,String(current.assignment_id))}catch(_error){}}};
  function queueCounts(items){return items.reduce((counts,item)=>{const key=String(item.status||'unknown');counts[key]=(counts[key]||0)+1;return counts},{})}
  function openQueuedImage(id){
    const index=reviewQueueOpenItems.findIndex(item=>Number(item.assignment_id)===Number(id));if(index<0)return;
    try{sessionStorage.setItem(queueResumeKey,String(id))}catch(_error){}
    const path=reviewQueueOpenItems[index].path;if(!path)return;
    reviewQueueModalItems=reviewQueueOpenItems.slice(index).filter(item=>item.path);modalPaths=reviewQueueModalItems.map(item=>item.path);modalIndex=0;renderMediaModal();
  }
  async function activateReviewQueue(){
    const list=document.querySelector('#list'),detail=document.querySelector('#detail'),next=document.querySelector('#nextPage');
    viewMode='review-queue';selected=null;selectedIdentity=null;
    document.querySelector('#reviewQueueTab')?.classList.add('active');document.querySelector('#identityTab')?.classList.remove('active');document.querySelector('#clusterTab')?.classList.remove('active');document.querySelector('#attentionTab')?.classList.remove('active');document.querySelector('#settingsTab')?.classList.remove('active');
    if(next){next.hidden=true;next.style.display='none'}document.querySelector('#clusterMode').style.display='none';document.querySelector('#hideConfirmed')?.closest('label')?.setAttribute('hidden','hidden');document.querySelector('#attentionCategory')?.setAttribute('hidden','hidden');
    document.querySelector('label[for="filter"]').textContent='Search queued assignments';document.querySelector('#filter').placeholder='Search identity or path';list.className='settings-list';list.innerHTML='<p class="muted">Loading durable review assignments…</p>';detail.innerHTML='';
    try{
      const response=await fetch('/api/assignment-queue',{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);
      reviewQueueItems=data.assignments||[];const counts=queueCounts(reviewQueueItems),open=reviewQueueItems.filter(item=>['pending','applying','error','conflict'].includes(item.status));reviewQueueOpenItems=open;
      let resumeId=null;try{resumeId=Number(sessionStorage.getItem(queueResumeKey))||null}catch(_error){}
      const resume=open.find(item=>Number(item.assignment_id)===resumeId)||open[0];
      document.querySelector('#summary').textContent=`${open.length} to review · ${counts.applied||0} applied · ${counts.error||0} errors · ${counts.conflict||0} conflicts · ${counts.rejected||0} rejected`;
      const query=(document.querySelector('#filter').value||'').trim().toLocaleLowerCase();
      list.innerHTML=`<section class="settings-panel"><h2>Durable assignment queue</h2><p class="muted">Resume opens the saved item and the remaining queue in order. Inspecting an image does not apply or change its queued assignment.</p>${resume?`<button type="button" class="queue-resume" data-assignment="${Number(resume.assignment_id)}">Resume at #${Number(resume.assignment_id)}</button>`:'<span class="muted">No assignments need review.</span>'}<div class="assignment-queue">${reviewQueueItems.filter(item=>!query||`${item.identity} ${item.path} ${item.status}`.toLocaleLowerCase().includes(query)).map(item=>{
        const id=Number(item.assignment_id),active=['pending','applying','error','conflict'].includes(item.status),hash=String(item.expected_sha256||'').slice(0,12),source=String(item.source||'unknown');
        return `<article class="queue-row"><code>#${id}</code><b>${esc(item.identity)}</b><span>${esc(item.status)}</span><small title="${esc(item.path)}">${esc(item.path)}</small>${item.path?`<button type="button" data-open-assignment="${id}">Inspect image</button>`:''}${item.status==='pending'?`<button type="button" onclick="rejectQueuedAssignment(${id})">Reject</button>`:''}<div class="queue-preview"><b>Move preview:</b> ${esc(item.identity)} · source ${esc(source)} · SHA-256 ${esc(hash||'unavailable')}<br>Destination and family are unresolved from this queue record. Stable source database row key is not recorded. This preview causes no source DB or media changes.</div>${item.error?`<small role="alert">${esc(item.error)}</small>`:''}</article>`
      }).join('')||'<span class="muted">No assignments match this search.</span>'}</div></section>`;
      list.querySelector('.queue-resume')?.addEventListener('click',event=>openQueuedImage(event.currentTarget.dataset.assignment));list.querySelectorAll('[data-open-assignment]').forEach(button=>button.addEventListener('click',()=>openQueuedImage(button.dataset.openAssignment)));
      detail.innerHTML='<div class="empty-state"><h2>Queue progress</h2><p>Open an item to inspect its media. Apply or reject it only after checking the identity and evidence.</p><p class="muted">Queue state is stored with assignment IDs and expected file hashes. Source database updates are not part of this workflow.</p></div>';
    }catch(error){list.innerHTML=`<p role="alert">${esc(error.message)}</p>`}
    urlState();
  }
  function activateSettings(){
    const list=document.querySelector('#list'),detail=document.querySelector('#detail');viewMode='settings';selected=null;selectedIdentity=null;const next=document.querySelector('#nextPage'),clusterMode=document.querySelector('#clusterMode');if(next){next.hidden=true;next.disabled=true;next.style.display='none'}if(clusterMode)clusterMode.style.display='none';document.querySelector('#hideConfirmed')?.closest('label')?.setAttribute('hidden','hidden');if(document.querySelector('#attentionCategory'))document.querySelector('#attentionCategory').hidden=true;list.className='settings-list';list.innerHTML='<div class="settings-card"><b>Pipeline steps</b><ul><li><b>Ingest:</b> move completed downloads and intake new media.</li><li><b>Name audit:</b> dry-run alias/name matching only; use it to inspect suggestions.</li><li><b>Reconcile confirmed:</b> migrate only confirmed images into canonical identity folders and refresh their face markers.</li><li><b>Rebuild face data:</b> accuracy-first reference coalescing and face database rebuild; this makes the review UI read-only.</li><li><b>Refresh matches:</b> reuse the validated face database for faster new-content matching.</li><li><b>Full cycle:</b> ingest, reconcile, rebuild/match, then reload the newest audit in the UI.</li></ul><p class="muted">Automatic scheduling is disabled until you enable it below. High-confidence moves are opt-in and remain gated by the existing safety checks.</p></div>';
    detail.innerHTML='<section class="settings-panel"><h2>Pipeline settings</h2><div id="schedulerStatus" class="scheduler-status" role="status">Loading scheduler status…</div><form id="schedulerForm" onsubmit="event.preventDefault();saveSchedulerSettings().catch(error=>{document.querySelector(\'#schedulerStatus\').textContent=error.message})"><label><input id="scheduleEnabled" type="checkbox"> Enable automatic cycle</label><label>Interval (minutes) <input id="scheduleInterval" type="number" min="5" max="10080" step="5" value="360"></label><label><input id="runIngest" type="checkbox" checked> Ingest completed downloads before each cycle</label><label><input id="checkEvidence" type="checkbox" checked> Check/backup evidence database each cycle</label><label><input id="rebuildFaces" type="checkbox" checked> Rebuild face references/database (accuracy-first)</label><label><input id="migrateConfirmed" type="checkbox" checked> Migrate confirmed media and refresh face markers</label><label><input id="applyHighConfidence" type="checkbox"> Apply high-confidence name matches (safety-gated)</label><div class="settings-actions"><button type="submit">Save settings</button><button type="button" onclick="launchScheduler(\'cycle\').catch(error=>alert(error.message))">Run full cycle now</button><button type="button" onclick="launchScheduler(\'run\',\'evidence_health\').catch(error=>alert(error.message))">Check/backup evidence DB</button><button type="button" onclick="launchScheduler(\'run\',\'ingest\').catch(error=>alert(error.message))">Run ingest</button><button type="button" onclick="launchScheduler(\'run\',\'name_audit\').catch(error=>alert(error.message))">Run name audit (dry-run)</button><button type="button" onclick="launchScheduler(\'run\',\'reconcile_confirmed\').catch(error=>alert(error.message))">Reconcile confirmed</button><button type="button" onclick="launchScheduler(\'run\',\'rebuild_faces\').catch(error=>alert(error.message))">Rebuild face data</button><button type="button" onclick="launchScheduler(\'run\',\'refresh_matches\').catch(error=>alert(error.message))">Refresh matches</button><button type="button" onclick="launchScheduler(\'run\',\'refresh_ui\').catch(error=>alert(error.message))">Reload UI audit</button><button type="button" onclick="launchScheduler(\'daemon\').catch(error=>alert(error.message))">Start scheduler</button><button type="button" onclick="stopScheduler().catch(error=>alert(error.message))">Stop active job</button></div></form><h3>Queued assignments</h3><p class="muted">Assignments are durable and queue-only until Reconcile confirmed applies them with SHA-256 verification.</p><div id="assignmentQueue" class="assignment-queue"></div></section>';
    loadSchedulerSettings();loadAssignmentQueue();urlState();
  }
  let attentionPage=0, attentionHasNext=false, attentionRequest=0, attentionLoading=false;
  async function loadAttention(reset=true){
    const list=document.querySelector('#list'), detail=document.querySelector('#detail'), category=document.querySelector('#attentionCategory')?.value||'';
    if(!list||!detail||(!reset&&(attentionLoading||!attentionHasNext)))return;
    const requestId=++attentionRequest, page=reset?1:attentionPage+1;
    if(reset){attentionPage=0;attentionHasNext=false;list.innerHTML='<p class="muted scan-progress">Loading needs-attention queue…</p>'}
    attentionLoading=true;
    const next=document.querySelector('#nextPage');if(next&&!reset){next.disabled=true;next.textContent='Loading attention items…'}
    const query=encodeURIComponent(document.querySelector('#filter')?.value||'');let response,data;
    try{response=await fetch(`/api/attention?page=${page}&page_size=500&category=${encodeURIComponent(category)}&q=${query}`,{headers:{Accept:'application/json'}});data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`)}catch(error){if(requestId===attentionRequest)attentionLoading=false;throw error}
    if(requestId!==attentionRequest)return;
    const cards=(data.items||[]).map(item=>`<article class="attention-card"><div class="attention-meta"><b>${esc(item.category||'attention')}</b><span>${esc(item.status||'')}</span>${item.count?`<span>${item.count} occurrence(s)</span>`:''}</div><code>${esc(item.path||item.message||'')}</code><div>${esc(item.reason||'Review before retrying the pipeline.')}</div></article>`).join('');
    if(reset)list.innerHTML=cards||'<p class="muted">No files currently need attention.</p>';else list.insertAdjacentHTML('beforeend',cards);
    attentionPage=page;attentionHasNext=Boolean(data.has_next);
    const visible=list.querySelectorAll('.attention-card').length;
    document.querySelector('#summary').textContent=`${data.total} items need attention · showing ${visible} of ${data.filtered} filtered · ${Object.entries(data.counts||{}).map(([key,value])=>key+': '+value).join(' · ')}`;
    if(next){next.hidden=!attentionHasNext;next.disabled=!attentionHasNext;next.style.display='block';next.textContent='Load more attention items';next.setAttribute('aria-label','Load more attention items');next.onclick=()=>loadAttention(false).catch(error=>{list.insertAdjacentHTML('afterbegin',`<p role="alert">${esc(error.message)}</p>`);next.disabled=false;next.textContent='Load more attention items'})}
    detail.innerHTML='<h2>Needs attention</h2><p class="muted">These entries were missing, unreadable, deferred, oversized, unsupported, or conflicting during the last audit. Repair or retry them in the pipeline; this queue never moves files.</p>';
    attentionLoading=false;
  }
  function activateAttention(){
    addAttentionTab();document.querySelector('#identityTab')?.classList.remove('active');document.querySelector('#clusterTab')?.classList.remove('active');document.querySelector('#attentionTab')?.classList.add('active');const next=document.querySelector('#nextPage');if(next){next.hidden=true;next.disabled=true;next.style.display='block';next.textContent='Loading attention items…';next.setAttribute('aria-label','Loading attention items')}document.querySelector('#clusterMode').style.display='none';document.querySelector('#hideConfirmed')?.closest('label')?.setAttribute('hidden','hidden');document.querySelector('label[for="filter"]').textContent='Search attention queue';document.querySelector('#filter').placeholder='Search paths or reasons';
    let selector=document.querySelector('#attentionCategory');if(!selector){selector=document.createElement('select');selector.id='attentionCategory';selector.setAttribute('aria-label','Attention category');selector.innerHTML='<option value="">All attention types</option><option value="missing">Missing</option><option value="unreadable">Unreadable</option><option value="deferred">Deferred</option><option value="conflict">Conflict</option><option value="low-quality">Low quality</option><option value="no-face">No face</option><option value="unsupported">Unsupported</option>';selector.addEventListener('change',loadAttention);document.querySelector('#filter')?.before(selector)}selector.hidden=false;
  }
  const originalShowViewEnhanced=showView;
  showView=async function(mode){
    if(mode==='review-queue'){addReviewQueueTab();return activateReviewQueue()}
    if(mode==='settings'){activateSettings();enhanceSettingsWorkflow();addSettingsTab();document.querySelector('#settingsTab')?.classList.add('active');urlState();return}
    if(mode==='attention'){viewMode='attention';selected=null;selectedIdentity=null;activateAttention();document.querySelector('#list').innerHTML='<p class="muted scan-progress">Loading needs-attention queue…</p>';try{await loadAttention()}catch(error){document.querySelector('#list').innerHTML=`<p role="alert">${esc(error.message)}</p>`}urlState();return}
    const selector=document.querySelector('#attentionCategory');if(selector)selector.hidden=true;document.querySelector('#clusterMode').style.display='';document.querySelector('#hideConfirmed')?.closest('label')?.removeAttribute('hidden');
    let scanTimer=null;
    if(mode==='identities'){
      const started=Date.now();scanTimer=setInterval(()=>{const list=document.querySelector('#list');if(list&&list.textContent.includes('Scanning'))list.innerHTML=`<p class="muted scan-progress">Scanning identity registry and assigned folders… ${Math.floor((Date.now()-started)/1000)}s elapsed. You can leave this view and return safely.</p>`},500);
    }
    try{const result=await originalShowViewEnhanced(mode);addAttentionTab();addSettingsTab();addReviewQueueTab();document.querySelector('#reviewQueueTab')?.classList.remove('active');document.querySelector('#attentionTab')?.classList.remove('active');document.querySelector('#settingsTab')?.classList.remove('active');ensureIdentityScope();urlState();return result}
    finally{if(scanTimer)clearInterval(scanTimer)}
  };
  document.querySelector('#filter')?.addEventListener('input',()=>{if(viewMode==='attention')loadAttention();if(viewMode==='review-queue'){clearTimeout(queueSearchTimer);queueSearchTimer=setTimeout(activateReviewQueue,180)}urlState()});
  document.querySelector('#hideConfirmed')?.addEventListener('change',urlState);

  async function restoreUrlState(){
    const params=new URLSearchParams(location.search), requestedView=params.get('view');
    if(!requestedView&&!params.get('cluster')&&!params.get('identity'))return;
    const query=params.get('q')||'';const filter=document.querySelector('#filter');if(filter)filter.value=query;
    if(params.get('show_confirmed')==='1'){hideConfirmed=false;const toggle=document.querySelector('#hideConfirmed');if(toggle)toggle.checked=false}
    if(requestedView==='settings'){await showView('settings');return}
    if(requestedView==='attention'){await showView('attention');return}
    if(requestedView==='review-queue'){await showView('review-queue');return}
    if(requestedView==='identities'||params.get('identity')){await showView('identities');if(params.get('identity'))await selectIdentity(params.get('identity'));return}
    await showView('clusters');if(params.get('cluster'))await select(params.get('cluster'));
  }
  window.addEventListener('popstate',restoreUrlState);
  addAttentionTab();addSettingsTab();addReviewQueueTab();ensureIdentityScope();
  setTimeout(()=>{if(location.search)restoreUrlState();else urlState()},350);
})();
"""
HTML_PAGE = HTML_PAGE.replace('</script>', UI_ENHANCEMENT_SCRIPT + '</script>', 1)

ASSORTED_UI_CSS = r"""
.view-tabs{grid-template-columns:repeat(3,minmax(0,1fr))!important}
.assorted-card{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;width:100%;text-align:left;background:rgba(255,255,255,.06);color:#eaf4f8;border:1px solid #385265;border-radius:8px;padding:9px;box-sizing:border-box}
.assorted-card:hover,.assorted-card:has(input:checked){border-color:#8bc9d8;background:#294354}
.assorted-card input{width:20px;height:20px;margin:2px 0;accent-color:#7cc4ff}
.assorted-card b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.assorted-card small{display:block;color:#aec1cd;margin-top:3px;overflow-wrap:anywhere}
.assorted-actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.assorted-table{display:grid;gap:8px}
.assorted-table label{font-weight:650;color:#425364}
.assorted-table input,.assorted-table select,.assorted-table textarea{width:100%;box-sizing:border-box;border:1px solid #c5d1da;border-radius:7px;background:#fbfdfe}
@media(max-width:760px){.view-tabs{grid-template-columns:repeat(2,minmax(0,1fr))!important}}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', ASSORTED_UI_CSS + '</style>', 1)

ASSORTED_UI_SCRIPT = r"""
(function(){
  const priorShowView=window.showView;
  let assortedFolders=[], assortedSelected=new Set(), assortedIdentities=[], assortedVisibleCount=50;
  function addAssortedTab(){
    const tabs=document.querySelector('.view-tabs');
    if(!tabs||document.querySelector('#assortedTab'))return;
    const button=document.createElement('button');button.id='assortedTab';button.type='button';button.textContent='Assorted folders';button.onclick=()=>window.picorgNavigate('assorted');tabs.append(button);
  }
  function setAssortedActive(active){document.querySelector('#assortedTab')?.classList.toggle('active',active);if(active){document.querySelector('#identityTab')?.classList.remove('active');document.querySelector('#clusterTab')?.classList.remove('active');document.querySelector('#attentionTab')?.classList.remove('active');document.querySelector('#settingsTab')?.classList.remove('active')}}
  function selectedAssortedFolders(){return assortedFolders.filter(item=>assortedSelected.has(item.folder))}
  function updateAssortedLoadButton(total){const next=document.querySelector('#nextPage');if(!next)return;next.hidden=total<=assortedVisibleCount;next.disabled=total<=assortedVisibleCount;next.style.display='';next.textContent='Load more assorted folders';next.setAttribute('aria-label','Load more assorted folders');next.onclick=()=>{assortedVisibleCount+=50;renderAssortedList()}}
  function renderAssortedList(){
    const list=document.querySelector('#list');if(!list)return;list.className='assorted-table';
    const query=(document.querySelector('#filter')?.value||'').trim().toLowerCase();
    const matching=assortedFolders.filter(item=>(item.label+' '+item.folder).toLowerCase().includes(query));
    const visible=matching.slice(0,assortedVisibleCount);updateAssortedLoadButton(matching.length);
    document.querySelector('#summary').textContent=`Showing ${visible.length} of ${matching.length} candidate folders · ${matching.reduce((n,item)=>n+Number(item.media_count||0),0)} images`;
    list.innerHTML=visible.map(item=>{const association=item.associated;const assigned=association?` · ${esc(association.identity)}`:'';return `<label class="assorted-card"><input type="checkbox" data-assorted-folder="${esc(item.folder)}" ${assortedSelected.has(item.folder)?'checked':''} onchange="window.picorgToggleAssorted(this)"><span><b>${esc(item.label)}</b><small>${Number(item.media_count||0)} images${assigned}</small><small>${esc(item.folder)}</small></span></label>`}).join('')||'<p class="muted">No non-generic assorted folders match this search.</p>';
  }
  function renderAssortedDetail(){
    const detail=document.querySelector('#detail');if(!detail)return;
    const selected=selectedAssortedFolders();
    detail.innerHTML=`<h2>Assorted folder identity association</h2><div class="meta"><b>${selected.length} folder(s) selected</b><br>${selected.reduce((n,item)=>n+Number(item.media_count||0),0)} image(s) represented<br><span class="muted">This records identity metadata only. It never moves, renames, hashes, or rewrites files.</span></div><form class="form assorted-table" onsubmit="event.preventDefault();window.picorgAssociateAssorted()"><label>Existing canonical identity<select id="assortedIdentity"><option value="">Choose an identity</option></select></label><label>Or create local identity<input id="assortedNewIdentity" placeholder="Only for a new person identity"></label><label>Family<select id="assortedFamily"><option value="review">review (local)</option><option value="manual">manual</option></select></label><label>Notes<textarea id="assortedNotes" placeholder="Why this folder label represents the identity"></textarea></label><div class="assorted-actions"><button type="button" onclick="window.picorgSelectAllAssorted()">Select all visible</button><button type="button" onclick="window.picorgSelectNoneAssorted()">Select none</button><button class="approve" type="submit" ${selected.length?'':'disabled'}>Save association (no moves)</button></div><div class="status" id="assortedStatus" aria-live="polite"></div></form>`;
    const select=document.querySelector('#assortedIdentity');for(const item of assortedIdentities){const option=new Option(item.canonical||'',item.canonical||'');select?.append(option)}
  }
  async function loadAssorted(){
    const list=document.querySelector('#list'),detail=document.querySelector('#detail');if(list)list.innerHTML='<p class="muted">Scanning assorted folders…</p>';if(detail)detail.innerHTML='<p class="muted">Loading folder inventory…</p>';
    assortedVisibleCount=50;try{const [folderResponse,identityResponse]=await Promise.all([fetch('/api/assorted-folders',{headers:{Accept:'application/json'}}),fetch('/api/identities?scope=canonical',{headers:{Accept:'application/json'}})]);const folders=await folderResponse.json(), identities=await identityResponse.json();if(!folderResponse.ok)throw new Error(folders.error||`Request failed (${folderResponse.status})`);if(!identityResponse.ok)throw new Error(identities.error||`Request failed (${identityResponse.status})`);assortedFolders=folders.folders||[];assortedIdentities=identities||[];assortedSelected=new Set([...assortedSelected].filter(path=>assortedFolders.some(item=>item.folder===path)));renderAssortedList();renderAssortedDetail();}catch(error){if(list)list.innerHTML=`<p role="alert">${esc(error.message)} <button type="button" onclick="window.picorgLoadAssorted()">Retry</button></p>`}
  }
  window.picorgToggleAssorted=input=>{const folder=input?.dataset.assortedFolder;if(!folder)return;if(input.checked)assortedSelected.add(folder);else assortedSelected.delete(folder);renderAssortedDetail()};
  window.picorgSelectAllAssorted=()=>{const query=(document.querySelector('#filter')?.value||'').trim().toLowerCase();assortedFolders.filter(item=>(item.label+' '+item.folder).toLowerCase().includes(query)).slice(0,assortedVisibleCount).forEach(item=>assortedSelected.add(item.folder));renderAssortedList();renderAssortedDetail()};
  window.picorgSelectNoneAssorted=()=>{assortedSelected.clear();renderAssortedList();renderAssortedDetail()};
  window.picorgLoadAssorted=loadAssorted;
  window.picorgAssociateAssorted=async()=>{
    const selected=selectedAssortedFolders(), newIdentity=document.querySelector('#assortedNewIdentity')?.value.trim()||'', identity=newIdentity||document.querySelector('#assortedIdentity')?.value||'', family=document.querySelector('#assortedFamily')?.value||'review', notes=document.querySelector('#assortedNotes')?.value.trim()||'';
    const status=document.querySelector('#assortedStatus');if(!selected.length||!identity){if(status)status.textContent='Select at least one folder and choose or enter an identity.';return}
    if(!window.confirm(`Save ${selected.length} folder association(s) for ${identity}? No files will be moved.`))return;
    const button=document.querySelector('.assorted-table button[type="submit"]');if(button)button.disabled=true;let saved=0,creatingIdentity=Boolean(newIdentity);
    try{for(const item of selected){const result=await postNewIdentityWithPrompt('/api/assorted-folder-associations',{folder:item.folder,identity,family,create_identity:creatingIdentity,notes});if(result.cancelled)return;if(!result.response.ok)throw new Error(result.data.error||`Request failed (${result.response.status})`);identity=result.payload.identity;creatingIdentity=false;saved++}if(status)status.textContent=`Saved ${saved} association(s). No files were moved.`;await loadAssorted();}
    catch(error){if(status)status.textContent=`Saved ${saved}; ${error.message}`;}finally{if(button)button.disabled=false}
  };
  const oldShowView=window.showView;
  window.showView=async function(mode){
    if(mode!=='assorted'){setAssortedActive(false);const next=document.querySelector('#nextPage');if(next){next.hidden=false;next.disabled=!hasNext;next.style.display='';next.textContent='Load more clusters';next.setAttribute('aria-label','Load more clusters');next.onclick=()=>loadPage(false)}document.querySelector('#clusterMode')?.removeAttribute('hidden');document.querySelector('#hideConfirmed')?.closest('label')?.removeAttribute('hidden');return oldShowView(mode)}
    viewMode='assorted';selected=null;selectedIdentity=null;setAssortedActive(true);const next=document.querySelector('#nextPage');if(next){next.hidden=true;next.disabled=true;next.style.display='';next.textContent='Loading assorted folders…';next.setAttribute('aria-label','Loading assorted folders')};document.querySelector('#clusterMode')?.setAttribute('hidden','hidden');document.querySelector('#hideConfirmed')?.closest('label')?.setAttribute('hidden','hidden');const filter=document.querySelector('#filter');if(filter){filter.value='';filter.placeholder='Search assorted folders'}const label=document.querySelector('label[for="filter"]');if(label)label.textContent='Search assorted folders';history.replaceState(null,'',`${location.pathname}?view=assorted`);await loadAssorted();
  };
  document.querySelector('#filter')?.addEventListener('input',()=>{if(viewMode==='assorted'){assortedVisibleCount=50;renderAssortedList()}});
  addAssortedTab();
    setTimeout(()=>{addAssortedTab();if(new URLSearchParams(location.search).get('view')==='assorted')window.picorgNavigate('assorted')},500);
})();
"""
HTML_PAGE = HTML_PAGE.replace('</script>', ASSORTED_UI_SCRIPT + '</script>', 1)
HTML_PAGE = HTML_PAGE.replace(
    'Assign selected to identity',
    'Assign selected…',
)

# The enhancement script is appended after the legacy observer replacements
# above.  Apply the same guard to its observers after assembly; otherwise the
# late script reintroduces subtree scans and a large cluster can monopolize
# the renderer while the detail pane is being populated.
HTML_PAGE = HTML_PAGE.replace(
    "const lazyDetailObserver=new MutationObserver((records)=>{",
    "const lazyDetailObserver=new MutationObserver((records)=>{",
)
HTML_PAGE = HTML_PAGE.replace(
    "lazyDetailObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "lazyDetailObserver.observe(document.querySelector('#detail'),{childList:true});",
)
HTML_PAGE = HTML_PAGE.replace(
    "detailEnhancementObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});",
    "detailEnhancementObserver.observe(document.querySelector('#detail'),{childList:true});",
)

# Selecting a cluster only needs to toggle the active card.  Re-rendering the
# entire 50-card list at the start of every selection synchronously creates a
# second thumbnail wave and can lock up Chromium before the detail fetch
# completes.  Keep the direct DOM update in the base function so this remains
# true even when later compatibility wrappers are added.
HTML_PAGE = HTML_PAGE.replace(
    "async function select(id){if(!id)return;selected=id;renderList();let x=await fetch(",
    "async function select(id){if(!id)return;selected=id;document.querySelectorAll('.cluster').forEach(card=>card.classList.toggle('active',card.getAttribute('onclick')?.includes(`'${id}'`)));let x=await fetch(",
    1,
)

RELATED_IDENTITY_PICKER_CSS = r"""
#relatedIdentityDialog{width:min(860px,calc(100vw - 28px));max-width:none;max-height:calc(100dvh - 28px);padding:0;border:1px solid #526a7a;border-radius:12px;background:#14212b;color:#edf5f8;box-shadow:0 16px 60px #000b}
#relatedIdentityDialog:not([open]){display:none}
#relatedIdentityDialog[open]{display:grid}
#relatedIdentityDialog::backdrop{background:#000b}
.related-identity-picker{display:grid;grid-template-rows:auto auto minmax(0,1fr);max-height:calc(100dvh - 28px)}
.related-identity-picker header{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:12px;padding:14px 18px;background:#192a36;border-bottom:1px solid #405563}
.related-identity-picker h2{flex:1;margin:0;font-size:1.1rem}
.related-identity-picker input{margin:12px 16px;padding:10px;border:1px solid #526a7a;border-radius:7px;background:#0e1920;color:inherit}
.related-identity-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(240px,100%),1fr));gap:10px;overflow:auto;padding:0 16px 16px}
.related-identity-card{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;width:100%;padding:9px;text-align:left;color:inherit;background:#1b2d39;border:1px solid #405563;border-radius:8px;cursor:pointer}
.related-identity-card:hover,.related-identity-card:focus-visible{border-color:#92d8ef;outline:2px solid #92d8ef}
.related-identity-card strong,.related-identity-card small{grid-column:1/-1;overflow-wrap:anywhere}
.related-identity-card small{color:#b4c7d0}
.related-identity-card img{width:100%;height:150px;object-fit:contain;background:#091117;border-radius:4px}
.modal-pinned-identities{display:flex;flex-wrap:wrap;align-items:center;gap:6px;width:100%;padding:6px 8px;border:1px solid #526a7a;border-radius:7px;background:#182a35}
.modal-pinned-identities[hidden]{display:none}
.modal-pinned-identities>span{width:100%;font-weight:600;color:#c3dce8}
.modal-pinned-identity{display:flex;gap:2px}
.modal-pinned-identity button{padding:5px 8px;color:inherit;background:#294958;border:1px solid #527789;border-radius:5px;cursor:pointer}
.modal-pinned-identity .modal-unpin-identity{padding-inline:6px;color:#d2e4eb}
.related-identity-empty{grid-column:1/-1;padding:24px;text-align:center;color:#b4c7d0}
@media(max-width:520px){.related-identity-results{grid-template-columns:1fr}.related-identity-card img{height:120px}}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', RELATED_IDENTITY_PICKER_CSS + '</style>', 1)

RELATED_IDENTITY_PICKER_SCRIPT = r"""
(()=>{
  const defaultPickerPrefixes=['fbhottie','redhottie','reddcutie','frecklehottie','gothbaddie','twins'];
  const pinnedStorageKey='picorg.pinned-identities.v1';
  let pinnedIdentities=[];
  let pickerPrefixes=[...defaultPickerPrefixes];
  let pickerSettingsLoaded=false;
  let candidatesPromise=null;
  function updatePickerButtonLabel(){
    const button=document.querySelector('#mediaModalContent .related-identity-open');if(!button)return;
    const labels=pickerPrefixes.map(prefix=>`${prefix}*`);button.textContent=`Find ${labels.join(' / ')}`;button.title=`Show identities beginning with: ${labels.join(', ')}`;
  }
  try{const stored=JSON.parse(localStorage.getItem(pinnedStorageKey)||'[]');if(Array.isArray(stored))pinnedIdentities=stored.filter(item=>item&&typeof item.identity==='string'&&item.identity.trim()).map(item=>({identity:item.identity.trim(),family:item.family||'review'}))}catch(_error){}
  const dialog=document.createElement('dialog');dialog.id='relatedIdentityDialog';
  dialog.className='related-identity-picker';
  dialog.setAttribute('aria-labelledby','relatedIdentityTitle');
  const header=document.createElement('header'),title=document.createElement('h2'),close=document.createElement('button');
  title.id='relatedIdentityTitle';title.textContent='Choose a related identity';
  close.type='button';close.textContent='Close';close.addEventListener('click',()=>dialog.close());header.append(title,close);
  const search=document.createElement('input');search.type='search';search.placeholder='Filter identities or aliases';search.setAttribute('aria-label','Filter identities or aliases');
  const results=document.createElement('div');results.className='related-identity-results';results.setAttribute('aria-live','polite');
  dialog.append(header,search,results);document.body.append(dialog);
  async function loadPickerPrefixes(){
    try{const response=await fetch('/api/identity-picker/settings',{headers:{Accept:'application/json'}}),data=await response.json();if(response.ok&&Array.isArray(data.prefixes)&&data.prefixes.length){pickerPrefixes=data.prefixes;pickerSettingsLoaded=true}}catch(_error){}
    updatePickerButtonLabel();
  }
  function setPickerPrefixes(values){
    if(!Array.isArray(values))return;
    const clean=[...new Set(values.map(value=>String(value||'').trim().toLowerCase()).filter(value=>/^[a-z0-9][a-z0-9_-]{0,63}$/.test(value)))];
    if(!clean.length)return;
    pickerPrefixes=clean;pickerSettingsLoaded=true;candidatesPromise=null;window.relatedIdentityCandidates=[];
    updatePickerButtonLabel();
    if(dialog.open)openPicker();
  }
  window.picorgSetIdentityPickerPrefixes=setPickerPrefixes;
  function pickerEndpoint(){const params=new URLSearchParams();pickerPrefixes.forEach(prefix=>params.append('prefix',prefix));params.set('verified_examples','1');return '/api/identity-groups?'+params.toString()}
  function useIdentity(item){
    const picker=document.querySelector('#modalIdentity'),family=document.querySelector('#modalFamily'),typed=document.querySelector('#modalNewIdentity'),detailPicker=document.querySelector('#identity');
    if(picker){if(!Array.from(picker.options).some(option=>option.value===item.identity))picker.add(new Option(item.identity,item.identity));picker.value=item.identity}
    if(family){if(!Array.from(family.options).some(option=>option.value===item.family))family.add(new Option(item.family||'review',item.family||'review'));family.value=item.family||'review'}
    if(detailPicker&&Array.from(detailPicker.options).some(option=>option.value===item.identity))detailPicker.value=item.identity;
    if(typed)typed.value='';
    const pinButton=document.querySelector('#mediaModalContent .modal-pin-current');if(pinButton)updateCurrentPinButton(pinButton);
    dialog.close();
    const status=document.querySelector('#mediaModalContent .media-modal-status');
    if(status)status.textContent=`Selected ${item.identity}. Review the image, then use Assign & confirm.`;
  }
  function draw(){
    const query=search.value.trim().toLocaleLowerCase();results.replaceChildren();
    const matches=(window.relatedIdentityCandidates||[]).filter(item=>`${item.identity} ${(item.aliases||[]).join(' ')}`.toLocaleLowerCase().includes(query));
    if(!matches.length){const empty=document.createElement('div');empty.className='related-identity-empty';empty.textContent='No matching identities with saved examples were found.';results.append(empty);return}
    for(const item of matches){
      const card=document.createElement('button');card.type='button';card.className='related-identity-card';card.addEventListener('click',()=>useIdentity(item));
      const name=document.createElement('strong');name.textContent=item.identity;card.append(name);
      const aliases=(item.aliases||[]).filter(Boolean);if(aliases.length){const note=document.createElement('small');note.textContent=`Aliases: ${aliases.join(', ')}`;card.append(note)}
      const paths=(item.sample_paths||[]).filter(path=>/\.(bmp|gif|jpe?g|png|webp)$/i.test(path)).slice(0,2);
      for(const path of paths){const frame=document.createElement('span');frame.className='manual-group-thumb';const image=document.createElement('img');image.loading='lazy';image.decoding='async';image.alt=`Example for ${item.identity}`;image.src='/media?path='+encodeURIComponent(path);image.addEventListener('error',()=>frame.remove(),{once:true});frame.append(image);frame.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path));card.append(frame)}
      if(!paths.length){const note=document.createElement('small');note.textContent='No saved image examples available';card.append(note)}
      results.append(card);
    }
  }
  function isPinned(identity){return pinnedIdentities.some(item=>item.identity.toLocaleLowerCase()===identity.toLocaleLowerCase())}
  function savePinned(){try{localStorage.setItem(pinnedStorageKey,JSON.stringify(pinnedIdentities))}catch(_error){const status=document.querySelector('#mediaModalContent .media-modal-status');if(status)status.textContent='Could not save pinned identities in this browser.'}}
  function togglePinned(item){if(isPinned(item.identity))pinnedIdentities=pinnedIdentities.filter(value=>value.identity.toLocaleLowerCase()!==item.identity.toLocaleLowerCase());else pinnedIdentities=[...pinnedIdentities,{identity:item.identity,family:item.family||'review'}];savePinned();renderPinnedIdentities()}
  function currentModalIdentity(){const typed=document.querySelector('#modalNewIdentity')?.value.trim()||'',selected=document.querySelector('#modalIdentity')?.value.trim()||'',identity=typed||selected;if(!identity)return null;const known=(identityOptions||[]).find(item=>String(item.canonical||'').toLocaleLowerCase()===identity.toLocaleLowerCase());if(typed&&!known)return null;return{identity:known?.canonical||identity,family:known?.family||document.querySelector('#modalFamily')?.value||'review'}}
  function applyPinnedIdentity(item){const picker=document.querySelector('#modalIdentity'),family=document.querySelector('#modalFamily'),typed=document.querySelector('#modalNewIdentity'),detailPicker=document.querySelector('#identity');if(picker){if(!Array.from(picker.options).some(option=>option.value===item.identity))picker.add(new Option(item.identity,item.identity));picker.value=item.identity}if(family){if(!Array.from(family.options).some(option=>option.value===item.family))family.add(new Option(item.family,item.family));family.value=item.family}if(detailPicker&&Array.from(detailPicker.options).some(option=>option.value===item.identity))detailPicker.value=item.identity;if(typed)typed.value='';if(modalLastIdentity&&modalLastIdentity.identity===item.identity)modalLastIdentity={identity:item.identity,family:item.family};assignModalImage()}
  function renderPinnedIdentities(){
    const toolbar=document.querySelector('#mediaModalContent .media-modal-toolbar'),identityTools=toolbar?.querySelector('.modal-identity-tools');if(!toolbar||!identityTools)return;
    let row=identityTools.querySelector('#modalPinnedIdentities');if(!row){row=document.createElement('div');row.id='modalPinnedIdentities';row.className='modal-pinned-identities';row.setAttribute('role','group');row.setAttribute('aria-label','Pinned identities');const recent=identityTools.querySelector('#modalRecentIdentityList');identityTools.insertBefore(row,recent||identityTools.querySelector('#modalIdentity')?.nextSibling)}
    row.replaceChildren();row.hidden=!pinnedIdentities.length;if(!pinnedIdentities.length)return;
    const label=document.createElement('span');label.textContent='Pinned identities';row.append(label);
    for(const item of pinnedIdentities){const group=document.createElement('div');group.className='modal-pinned-identity';const use=document.createElement('button');use.type='button';use.textContent=item.identity;use.title=`Assign and move to ${item.identity}`;use.addEventListener('click',()=>applyPinnedIdentity(item));const remove=document.createElement('button');remove.type='button';remove.className='modal-unpin-identity';remove.textContent='×';remove.setAttribute('aria-label',`Unpin ${item.identity}`);remove.addEventListener('click',()=>togglePinned(item));group.append(use,remove);row.append(group)}
  }
  function updateCurrentPinButton(button){const item=currentModalIdentity();button.disabled=!item;button.textContent=item&&isPinned(item.identity)?'Unpin selected identity':'Pin selected identity';button.title=item?'Keep this identity in the pinned list, independently of recent use.':'Select an identity above to pin it';button.setAttribute('aria-pressed',String(Boolean(item&&isPinned(item.identity))))}
  function toggleCurrentPin(button){const item=currentModalIdentity();if(!item)return;togglePinned(item);updateCurrentPinButton(button)}
  async function openPicker(){
    search.value='';results.replaceChildren();const loading=document.createElement('div');loading.className='related-identity-empty';loading.textContent='Loading identities…';results.append(loading);
    dialog.showModal();search.focus();
    try{if(!pickerSettingsLoaded)await loadPickerPrefixes();candidatesPromise=null;window.relatedIdentityCandidates=[];candidatesPromise=fetch(pickerEndpoint(),{cache:'no-store',headers:{Accept:'application/json'}}).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);return data});window.relatedIdentityCandidates=await candidatesPromise;for(const item of window.relatedIdentityCandidates)manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(item.manual_groups_by_path||{})};draw()}
    catch(error){candidatesPromise=null;results.replaceChildren();const message=document.createElement('div');message.className='related-identity-empty';message.textContent=`Unable to load identities: ${error.message}`;results.append(message)}
  }
  search.addEventListener('input',draw);
  dialog.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight')event.stopPropagation()});
  dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close()});
  const originalRenderMediaModal=renderMediaModal;
  renderMediaModal=function(){originalRenderMediaModal();const toolbar=document.querySelector('#mediaModalContent .media-modal-toolbar'),identityTools=toolbar?.querySelector('.modal-identity-tools');if(!toolbar||!identityTools)return;let picker=toolbar.querySelector('.related-identity-open');if(!picker){picker=document.createElement('button');picker.type='button';picker.className='related-identity-open';picker.setAttribute('aria-haspopup','dialog');picker.addEventListener('click',openPicker);identityTools.append(picker)}updatePickerButtonLabel();let pin=toolbar.querySelector('.modal-pin-current');if(!pin){pin=document.createElement('button');pin.type='button';pin.className='modal-pin-current';pin.addEventListener('click',()=>toggleCurrentPin(pin))}const identitySelect=toolbar.querySelector('#modalIdentity');if(identitySelect)identitySelect.after(pin);else identityTools.append(pin);updateCurrentPinButton(pin);identitySelect?.addEventListener('change',()=>updateCurrentPinButton(pin));toolbar.querySelector('#modalNewIdentity')?.addEventListener('input',()=>updateCurrentPinButton(pin));renderPinnedIdentities()};
})();
"""
HTML_PAGE = HTML_PAGE.replace('</script>', RELATED_IDENTITY_PICKER_SCRIPT + '</script>', 1)

FULL_SIZE_IMAGE_VIEWER_CSS = r"""
#fullSizeImageDialog{position:fixed;inset:0;width:100vw;height:100vh;height:100dvh;max-width:none;max-height:none;margin:0;padding:0;border:0;background:#05080b;color:#fff;overflow:hidden}
#fullSizeImageDialog:not([open]){display:none}
#fullSizeImageDialog[open]{display:block}
#fullSizeImageDialog::backdrop{background:#000d}
.full-size-image-scroller{position:absolute;inset:0;overflow:auto;overscroll-behavior:contain}
.full-size-image-scroller img{display:block;width:auto;height:auto;max-width:none;max-height:none;min-width:0;object-fit:initial}
.full-size-image-close{position:fixed;z-index:2;top:12px;right:12px;padding:9px 13px;color:#fff;background:#182630eF;border:1px solid #8fa3ad;border-radius:7px;cursor:pointer}
.full-size-image-help{position:fixed;z-index:2;left:12px;bottom:10px;margin:0;padding:6px 9px;color:#fff;background:#182630d9;border-radius:5px;font-size:12px;pointer-events:none}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', FULL_SIZE_IMAGE_VIEWER_CSS + '</style>', 1)

MEDIA_MODAL_CLOSE_CSS = r"""
.media-modal-close{z-index:1002;pointer-events:auto;min-width:44px;min-height:44px;display:grid;place-items:center}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', MEDIA_MODAL_CLOSE_CSS + '</style>', 1)

FULL_SIZE_IMAGE_VIEWER_SCRIPT = r"""
(()=>{
  const dialog=document.createElement('dialog');dialog.id='fullSizeImageDialog';dialog.setAttribute('aria-label','Full-size image view');
  const scroller=document.createElement('div');scroller.className='full-size-image-scroller';
  const image=document.createElement('img');image.alt='Full-size image';
  const close=document.createElement('button');close.type='button';close.className='full-size-image-close';close.textContent='Close';
  const help=document.createElement('p');help.className='full-size-image-help';help.textContent='Full size · scroll to inspect · Esc to close';
  help.id='fullSizeImageHelp';dialog.setAttribute('aria-describedby',help.id);scroller.append(image);dialog.append(scroller,close,help);document.body.append(dialog);
  let returnFocus=null;
  function closeViewer(){dialog.close()}
  function openViewer(source){returnFocus=source;help.textContent='Full size · scroll to inspect · Esc to close';image.src=source.currentSrc||source.src;image.alt=source.alt||'Full-size image';scroller.scrollTop=0;scroller.scrollLeft=0;dialog.showModal();close.focus()}
  document.addEventListener('click',event=>{const source=event.target.closest?.('img');if(!source||source.closest('#detail .media-tile,#detail .manual-group-member,#mediaModalContent,#fullSizeImageDialog'))return;let url;try{url=new URL(source.currentSrc||source.src,location.href)}catch(_error){return}if(url.pathname!=='/media'||!url.searchParams.has('path'))return;event.preventDefault();event.stopImmediatePropagation();openViewer(source)},true);
  close.addEventListener('click',closeViewer);
  scroller.addEventListener('click',event=>{if(event.target===scroller)closeViewer()});
  image.addEventListener('error',()=>{help.textContent='Unable to load the full-size image';});
  dialog.addEventListener('close',()=>{if(returnFocus?.isConnected)returnFocus.focus()});
  dialog.addEventListener('click',event=>{if(event.target===dialog)closeViewer()});
  dialog.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight')event.stopPropagation()});
  const originalRenderMediaModal=renderMediaModal;
  renderMediaModal=function(){originalRenderMediaModal();const preview=document.querySelector('#mediaModalContent .media-preview-frame img');if(!preview)return;preview.tabIndex=0;preview.setAttribute('role','button');preview.title='Open full-size view';preview.style.cursor='zoom-in';preview.addEventListener('click',()=>openViewer(preview));preview.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openViewer(preview)}})};
})();
"""
HTML_PAGE = HTML_PAGE.replace('</script>', FULL_SIZE_IMAGE_VIEWER_SCRIPT + '</script>', 1)

MANUAL_GROUPS_CSS = r"""
.manual-group-tools{display:grid;grid-template-columns:minmax(120px,1fr) auto;gap:8px;margin:12px 0;padding:12px;background:#182a35;border:1px solid #405563;border-radius:8px}
.manual-group-badges{position:absolute;top:6px;left:6px;right:6px;display:flex;flex-wrap:wrap;gap:4px;pointer-events:none;z-index:2}.cluster-thumb-wrap,.manual-group-thumb,.compare-reference-card a,.manual-group-member>a{position:relative;display:block}.manual-group-thumb{min-width:0}.manual-group-thumb img{display:block;width:100%}
.manual-group-badge{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 6px;border:1px solid rgba(255,255,255,.38);border-radius:999px;background:rgba(18,37,49,.88);color:#f4fbff;font-size:10px;line-height:1.4;text-shadow:0 1px 2px #000}
.media-modal-content .manual-group-badges{top:8px;left:8px}
.manual-group-tools label,.manual-group-tools .manual-group-status{grid-column:1/-1}
.manual-group-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.manual-group-card{display:grid;gap:7px;text-align:left;background:#1b2d39;color:inherit;border:1px solid #405563;border-radius:8px;padding:10px}
.manual-group-card:hover{border-color:#92d8ef}
.manual-group-member{display:grid;gap:5px;align-content:start;padding:7px;background:#1b2d39;border-radius:7px;min-width:0}
.manual-group-member img{width:100%;height:150px;object-fit:contain;background:#091117}
.manual-group-member label{overflow-wrap:anywhere;font-size:12px}
.manual-group-modal{display:flex;flex:1 0 100%;align-items:center;gap:7px;flex-wrap:wrap;width:100%;padding-top:5px;border-top:1px solid #405563}
.manual-group-modal input{flex:1 1 180px;min-width:0}
.manual-group-recent{display:flex;align-items:center;gap:7px;flex:1 1 100%;font-size:12px;flex-wrap:wrap}
.manual-group-recent label{flex:0 0 auto}
.manual-group-recent select{flex:1;min-width:140px}
.manual-group-tools .manual-group-recent{grid-column:1/-1}
.recent-manual-group-buttons{display:flex;flex:1 1 100%;gap:3px;flex-wrap:wrap;align-content:start}
.recent-manual-group-buttons[hidden]{display:none}
.recent-manual-group-buttons button{min-width:0;max-width:100%;min-height:24px;padding:3px 6px;border-radius:999px;font-size:11px;line-height:1.15;white-space:normal;overflow-wrap:anywhere}
.recent-manual-group-buttons button[aria-pressed="true"]{outline:2px solid #8bc9d8}
.manual-group-modal small,.manual-group-modal .status{flex-basis:100%}
.recent-identity-shortlist{display:flex;align-items:center;gap:6px;flex-wrap:wrap;max-width:100%;padding:2px 0}
.recent-identity-shortlist[hidden]{display:none}
.recent-identity-shortlist span{font-size:12px;color:#c7d8e1;flex:0 0 auto}
.recent-identity-shortlist button{flex:0 0 auto;min-width:0;padding:6px 10px}
.recent-identity-shortlist button[aria-pressed="true"]{outline:2px solid #8bc9d8}
.detail{max-width:1600px;min-width:0}
#detail>.form{max-width:none;grid-template-columns:repeat(2,minmax(0,1fr));align-items:end;gap:8px 12px;padding:12px}
#detail>.form>label{display:grid;grid-template-columns:minmax(105px,.36fr) minmax(0,1fr);align-items:center;gap:8px;min-width:0}
#detail>.form>label>input,#detail>.form>label>select,#detail>.form>label>textarea{width:100%;min-width:0;box-sizing:border-box}
#detail>.form>#recentIdentityList,#detail>.form>#status{grid-column:1/-1}
#detail>.form>#recentIdentityList{color:#202124}
#detail>.form>#recentIdentityList span{color:#59636d}
#detail>.form>button{justify-self:start;min-height:34px;padding:5px 10px}
#detail>.form textarea{min-height:54px;resize:vertical}
#imageAssignTools{display:flex;align-items:center;gap:6px;padding:9px;line-height:1.35}
#imageAssignTools b,#imageAssignTools p{flex:1 0 100%;margin:0}
#imageAssignTools br{display:none}
#imageAssignTools button{min-height:32px;padding:5px 8px;font-size:13px}
.media-modal{padding:12px}
.media-modal-content{width:min(96vw,1500px);max-width:none;max-height:calc(100vh - 24px);display:flex;flex-direction:column;gap:6px}
.media-modal-content img,.media-modal-content video{max-width:96vw}
.media-modal-toolbar{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:6px;align-items:center;padding:8px}
.media-modal-toolbar>button,.media-modal-toolbar>select,.media-modal-toolbar>input{width:100%;min-width:0;min-height:34px;box-sizing:border-box;padding:5px 7px;font-size:13px}
.media-modal-toolbar>.modal-recent-identities,.media-modal-toolbar>.modal-pinned-identities,.media-modal-toolbar>.manual-group-modal{grid-column:1/-1}
.media-modal-toolbar .recent-identity-shortlist{max-height:72px;overflow:auto}
.media-modal-toolbar .recent-identity-shortlist button{padding:4px 8px;font-size:12px}
.media-modal-toolbar .modal-pinned-identities{max-height:72px;overflow:auto;align-content:start}
.media-modal-toolbar .manual-group-recent .recent-manual-group-buttons{max-height:58px;overflow:auto;align-content:start;padding:2px}
.media-modal-toolbar .manual-group-modal{gap:5px;padding-top:5px}
.media-modal-toolbar .manual-group-modal input,.media-modal-toolbar .manual-group-modal select{min-height:32px;padding:4px 7px}
.media-modal-toolbar .manual-group-modal .manual-group-recent{gap:5px}
@media(max-width:900px){.media-modal-toolbar{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:600px){#detail>.form{grid-template-columns:minmax(0,1fr)}#detail>.form>label{grid-template-columns:minmax(0,1fr);gap:4px}#detail>.form>#recentIdentityList,#detail>.form>#status{grid-column:auto}.media-modal-toolbar{grid-template-columns:repeat(2,minmax(0,1fr))}.media-modal-toolbar>.modal-recent-identities,.media-modal-toolbar>.modal-pinned-identities,.media-modal-toolbar>.manual-group-modal{grid-column:1/-1}}
.media-modal-toolbar{grid-template-columns:minmax(76px,.45fr) minmax(250px,2fr) minmax(190px,.8fr) minmax(108px,.42fr);align-items:start}
.modal-control-group{display:flex;flex-wrap:wrap;align-content:start;align-items:center;gap:6px;min-width:0;padding:7px;border:1px solid #465764;border-radius:7px;background:#17242c}
.modal-control-group button,.modal-control-group input,.modal-control-group select{min-width:0;min-height:34px;box-sizing:border-box;padding:5px 7px;font-size:13px}
.modal-navigation{justify-content:center}
.modal-identity-tools{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}
.modal-identity-tools input,.modal-identity-tools select{width:100%}
.modal-identity-tools #modalIdentitySearch,.modal-identity-tools #modalIdentity,.modal-identity-tools #modalNewIdentity,.modal-identity-tools .related-identity-open{grid-column:span 2}
.modal-identity-tools .modal-recent-identities,.modal-identity-tools .modal-pinned-identities{grid-column:1/-1;width:100%;min-width:0}
#modalRecentIdentityList{display:grid;grid-template-columns:repeat(auto-fill,minmax(82px,1fr));gap:3px;max-height:88px;overflow:auto;align-content:start;white-space:normal}
#modalRecentIdentityList span{grid-column:1/-1}
#modalRecentIdentityList button{min-width:0;min-height:25px;padding:3px 5px;font-size:11px;line-height:1.15;text-align:center;white-space:normal;overflow-wrap:anywhere}
.media-modal-toolbar>.modal-recent-identities{display:grid;grid-template-columns:repeat(auto-fill,minmax(125px,1fr));gap:5px;max-height:min(30dvh,260px);overflow-x:hidden;overflow-y:auto;align-content:start;white-space:normal}
.media-modal-toolbar>.modal-recent-identities span{grid-column:1/-1}
.media-modal-toolbar>.modal-recent-identities button{min-width:0;white-space:normal;overflow-wrap:anywhere;text-align:left}
.modal-primary-actions .approve{flex:1 1 100%;min-height:40px;padding:6px 10px;font-size:14px;font-weight:700}
.modal-primary-actions .modal-undo{flex:1 1 100%;min-height:34px}
.modal-more-tools{min-width:0}
.modal-more-tools>summary,.cluster-more-actions>summary{padding:7px 9px;border:1px solid #526775;border-radius:6px;background:#263943;color:#f4f8fb;font-weight:600;cursor:pointer}
.modal-more-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.modal-more-actions button{min-height:32px;padding:5px 8px;font-size:13px}
.manual-group-modal .modal-section-label{flex:0 0 100%;font-size:13px;color:#d1e1e8}
#imageAssignTools{flex-wrap:wrap}
.cluster-selection-actions,.cluster-primary-actions{display:flex;flex-wrap:wrap;align-items:center;gap:6px;min-width:0}
.cluster-primary-actions{padding-left:8px;border-left:1px solid #c5cdd3}
.cluster-primary-actions .approve{min-height:36px;padding:5px 10px;font-weight:700}
.cluster-primary-actions .undo-action{min-height:34px;padding:5px 9px}
.cluster-more-actions button{margin:6px 0 0 8px;min-height:32px;padding:5px 8px}
@media(max-width:980px){.media-modal-toolbar{grid-template-columns:minmax(0,1fr) minmax(0,2fr)}.modal-navigation{grid-column:1}.modal-identity-tools{grid-column:1/-1}.modal-primary-actions{grid-column:1}.modal-more-tools{grid-column:2}}
@media(max-width:600px){.media-modal-toolbar{grid-template-columns:minmax(0,1fr)}.modal-navigation,.modal-identity-tools,.modal-primary-actions,.modal-more-tools{grid-column:1}.modal-identity-tools{grid-template-columns:repeat(2,minmax(0,1fr))}.modal-identity-tools #modalIdentitySearch,.modal-identity-tools #modalIdentity,.modal-identity-tools #modalNewIdentity,.modal-identity-tools .related-identity-open{grid-column:span 2}.cluster-primary-actions{padding-left:0;border-left:0}}
.media-modal-content{width:min(97vw,1600px);height:min(96dvh,1000px);max-height:calc(100dvh - 24px);display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-rows:auto minmax(0,1fr) auto;gap:6px 10px;overflow:hidden}
.media-modal-toolbar{grid-column:1;grid-row:1/4;display:grid;grid-template-columns:minmax(0,1fr);align-content:start;gap:8px;max-height:100%;overflow-y:auto;overscroll-behavior:contain;position:static;min-width:0}
.modal-identity-tools{grid-template-columns:repeat(2,minmax(0,1fr))}
.modal-identity-tools #modalIdentitySearch,.modal-identity-tools #modalIdentity,.modal-identity-tools #modalNewIdentity,.modal-identity-tools .related-identity-open{grid-column:1/-1}
.media-modal-status{grid-column:2;grid-row:1;align-self:center;min-width:0}
.media-preview-frame{grid-column:2;grid-row:2;display:flex;align-items:center;justify-content:center;width:100%;height:100%;min-width:0;min-height:0;overflow:hidden}
.media-modal-content img,.media-modal-content video{width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain}
.media-modal-caption{grid-column:2;grid-row:3;min-width:0}
.media-modal-toolbar>.manual-group-modal{width:100%;box-sizing:border-box}
.cluster-action-panel{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,.85fr);gap:10px;margin:12px 0}
.cluster-action-section{min-width:0;padding:12px;background:#fff;border:1px solid #d8dfe5;border-radius:9px;box-shadow:0 1px 2px #17242c12}
.cluster-action-section h3{margin:0 0 8px;font-size:15px;color:#202a31}
.cluster-action-section>summary{font-size:14px;font-weight:650;color:#202a31;cursor:pointer;list-style-position:inside}
.cluster-action-section>.form{max-width:none;margin:0;padding:0;background:transparent;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 12px}
.cluster-action-section>.form>label{display:grid;grid-template-columns:minmax(95px,.4fr) minmax(0,1fr);align-items:center;gap:7px;min-width:0}
.cluster-action-section>.form>label>input,.cluster-action-section>.form>label>select,.cluster-action-section>.form>label>textarea{width:100%;min-width:0;box-sizing:border-box}
.cluster-action-section>.form>#recentIdentityList,.cluster-action-section>.form>#status{grid-column:1/-1}
.cluster-action-section>.form>#recentIdentityList{color:#202124}
.cluster-action-section>.form>#recentIdentityList span{color:#59636d}
.cluster-action-section>.form>button{justify-self:start;min-height:36px;padding:6px 11px}
.cluster-action-section>#imageAssignTools{margin:0;padding:0;background:transparent;border:0}
.cluster-action-section>.manual-group-tools{margin:0;background:#f4f7f9;border-color:#d8dfe5}
.cluster-action-panel>.cluster-group-section,.cluster-action-panel>.cluster-membership-section{grid-column:1/-1}
.cluster-group-section>.manual-group-tools label{color:#26333b}
.cluster-membership-section summary{font-weight:600;cursor:pointer}
.cluster-membership-section>#memberTools{margin:8px 0 0;padding:0;background:transparent}
@media(max-width:760px){.media-modal{align-items:center;padding:8px}.media-modal-content{width:100%;height:calc(100dvh - 16px);max-height:calc(100dvh - 16px);grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto auto minmax(0,.8fr);gap:4px}.media-modal-toolbar{grid-column:1;grid-row:4;max-height:100%;padding:6px}.media-modal-toolbar>.modal-recent-identities{max-height:min(24dvh,190px)}.media-modal-status{grid-column:1;grid-row:2}.media-preview-frame{grid-column:1;grid-row:1}.media-modal-caption{grid-column:1;grid-row:3}}
@media(max-width:720px){.cluster-action-panel{grid-template-columns:minmax(0,1fr)}.cluster-action-panel>.cluster-group-section,.cluster-action-panel>.cluster-membership-section{grid-column:1}.cluster-action-section>.form{grid-template-columns:minmax(0,1fr)}.cluster-action-section>.form>label{grid-template-columns:minmax(0,1fr);gap:4px}.cluster-action-section>.form>#recentIdentityList,.cluster-action-section>.form>#status{grid-column:auto}}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', MANUAL_GROUPS_CSS + '</style>', 1)

MANUAL_GROUPS_SCRIPT = r"""
(()=>{
  let manualGroupsActive=false,manualGroups=[],activeManualGroupName="";
  const RECENT_MANUAL_GROUPS_KEY='picorg.recent-manual-groups.v1';
  let recentManualGroups=[];
  try{const stored=JSON.parse(localStorage.getItem(RECENT_MANUAL_GROUPS_KEY)||'[]');if(Array.isArray(stored))recentManualGroups=[...new Set(stored.filter(name=>typeof name==='string'&&name.trim()).map(name=>name.trim()))].slice(0,20)}catch(_error){}
  const list=document.querySelector('#list'),detail=document.querySelector('#detail'),filter=document.querySelector('#filter');
  function syncRecentGroupPickers(){for(const wrapper of document.querySelectorAll('.manual-group-recent')){const select=wrapper.querySelector('.recent-manual-group');if(!select)continue;const selected=select.value;select.replaceChildren(new Option(recentManualGroups.length?'Choose a recent group':'No recent groups',''));for(const name of recentManualGroups)select.append(new Option(name,name));select.value=recentManualGroups.includes(selected)?selected:'';select.disabled=!recentManualGroups.length;const buttons=wrapper.querySelector('.recent-manual-group-buttons');if(!buttons)continue;buttons.replaceChildren();buttons.hidden=!recentManualGroups.length;for(const name of recentManualGroups){const button=document.createElement('button');button.type='button';button.textContent=name;button.setAttribute('aria-pressed',String(document.getElementById(select.dataset.targetId)?.value===name));button.onclick=()=>applyRecentManualGroup(select,name,button);buttons.append(button)}}}
  function rememberManualGroup(name){const value=String(name||'').trim();if(!value)return;recentManualGroups=[value,...recentManualGroups.filter(item=>item.toLocaleLowerCase()!==value.toLocaleLowerCase())].slice(0,20);try{localStorage.setItem(RECENT_MANUAL_GROUPS_KEY,JSON.stringify(recentManualGroups))}catch(_error){}persistRecentChoice('group',value);syncRecentGroupPickers()}
  async function restoreRecentManualGroups(){let local=[...recentManualGroups];try{const response=await fetch('/api/recent-choices'),data=await response.json(),remote=Array.isArray(data.groups)?data.groups:[];if(remote.length)recentManualGroups=remote;else if(local.length){recentManualGroups=local;for(const name of local.slice().reverse())await fetch('/api/recent-choices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'group',name})})}try{localStorage.setItem(RECENT_MANUAL_GROUPS_KEY,JSON.stringify(recentManualGroups))}catch(_error){}syncRecentGroupPickers()}catch(_error){}}
  restoreRecentManualGroups();
  function createRecentGroupPicker(input,id){const wrapper=document.createElement('div');wrapper.className='manual-group-recent';const label=document.createElement('label');label.htmlFor=id;label.textContent='Recently used groups';const select=document.createElement('select');select.id=id;select.className='recent-manual-group';select.dataset.targetId=input.id;select.setAttribute('aria-label','Recently used groups');select.onchange=()=>{if(select.value){input.value=select.value;syncRecentGroupPickers()}};const buttons=document.createElement('div');buttons.className='recent-manual-group-buttons';wrapper.append(label,select,buttons);syncRecentGroupPickers();return wrapper}
  function clusterSection(panel,className,title,tag='details'){let section=panel.querySelector(':scope >.'+className);if(!section){section=document.createElement(tag);section.className='cluster-action-section '+className;const heading=document.createElement(tag==='details'?'summary':'h3');heading.textContent=title;section.append(heading);panel.append(section)}return section}
  function arrangeClusterControls(){if(viewMode!=='clusters'||manualGroupsActive)return;const grid=detail.querySelector(':scope>.grid'),form=detail.querySelector('.form'),imageTools=detail.querySelector('#imageAssignTools'),groupTools=detail.querySelector('#manualGroupTools'),memberTools=detail.querySelector('#memberTools');if(!grid||(!form&&!imageTools&&!groupTools))return;let panel=detail.querySelector(':scope>.cluster-action-panel');if(!panel){panel=document.createElement('section');panel.className='cluster-action-panel';panel.setAttribute('aria-label','Cluster review controls')}const decision=clusterSection(panel,'cluster-decision-section','Cluster identity'),images=clusterSection(panel,'cluster-image-section','Image selection and assignment'),groups=clusterSection(panel,'cluster-group-section','Visual group'),membership=clusterSection(panel,'cluster-membership-section','Cluster membership tools','details');if(form&&form.parentElement!==decision)decision.append(form);if(imageTools&&imageTools.parentElement!==images){imageTools.querySelector(':scope>b')?.remove();images.append(imageTools)}if(groupTools&&groupTools.parentElement!==groups)groups.append(groupTools);if(memberTools&&memberTools.parentElement!==membership){memberTools.querySelector(':scope>b')?.remove();membership.append(memberTools)}if(panel.parentElement!==detail||panel.nextElementSibling!==grid)detail.insertBefore(panel,grid)}
  const clusterControlObserver=new MutationObserver(arrangeClusterControls);clusterControlObserver.observe(detail,{childList:true,subtree:true});
  function advanceAfterManualGroupAssignment(path,isModal){if(isModal)advanceModalPath(path,'Group assignment complete — no unassigned images remain in this cluster.')}
  async function applyRecentManualGroup(select,name,button){const input=document.getElementById(select.dataset.targetId);if(!input)return;input.value=name;select.value=name;for(const shortcut of select.closest('.manual-group-recent')?.querySelectorAll('.recent-manual-group-buttons button')||[])shortcut.setAttribute('aria-pressed',String(shortcut.textContent===name));const isModal=select.id==='modalRecentManualGroups',paths=isModal?[modalPaths[modalIndex]].filter(Boolean):[...document.querySelectorAll('#detail .imageSelect:checked')].map(item=>item.dataset.path),status=document.querySelector(isModal?'.manual-group-modal .status':'#manualGroupTools .manual-group-status');if(!paths.length){if(status)status.textContent=isModal?'No image is open. Select images in the cluster first.':'Select the images to add using their checkboxes.';return}if(paths.length>500){if(status)status.textContent='Add up to 500 selected images at a time.';return}if(!isModal&&!status){return}if(button)button.disabled=true;try{const response=await fetch('/api/manual-groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,paths})}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);for(const path of paths)updateManualGroupMembership(path,data.name);rememberManualGroup(data.name);if(status)status.textContent=`Added ${data.added} image(s) to ${data.name}; ${data.count} images in collection.`;if(isModal)advanceAfterManualGroupAssignment(paths[0],true);else if(viewMode==='clusters')await window.advanceClusterIfHandled?.();fetchGroups().then(()=>{for(const options of document.querySelectorAll('#manualGroupNames,#modalManualGroupNames'))options.replaceChildren(...manualGroups.map(group=>new Option(group.name,group.name)))}).catch(error=>{if(!isModal&&status)status.textContent+=` Collection list refresh failed: ${error.message}`})}catch(error){if(status)status.textContent=error.message}finally{if(button)button.disabled=false}}
  function addTab(){const tabs=document.querySelector('.view-tabs');if(!tabs||document.querySelector('#manualGroupsTab'))return;const button=document.createElement('button');button.id='manualGroupsTab';button.type='button';button.textContent='Manual collections';button.onclick=()=>window.picorgNavigate('manual-groups');tabs.append(button)}
  function setTab(){document.querySelector('#manualGroupsTab')?.classList.toggle('active',manualGroupsActive);if(manualGroupsActive){document.querySelector('#identityTab')?.classList.remove('active');document.querySelector('#clusterTab')?.classList.remove('active');document.querySelector('#attentionTab')?.classList.remove('active');document.querySelector('#settingsTab')?.classList.remove('active');document.querySelector('#reviewQueueTab')?.classList.remove('active')}}
  async function fetchGroups(){const response=await fetch('/api/manual-groups',{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);manualGroups=data;syncRecentGroupPickers();return data}
  function renderGroups(){const query=(filter?.value||'').trim().toLowerCase(),visible=manualGroups.filter(group=>group.name.toLowerCase().includes(query));list.className='identity-grid';document.querySelector('#summary').textContent=`${visible.length} manual collections · ${visible.reduce((sum,group)=>sum+group.count,0)} images`;list.replaceChildren();if(!visible.length){list.innerHTML='<p class="muted">No manual collections yet. In a cluster, select images with their checkboxes, then add those images to a collection.</p>';return}for(const group of visible){const button=document.createElement('button');button.type='button';button.className='identity-card manual-group-card';const title=document.createElement('b');title.textContent=group.name;const count=document.createElement('span');count.textContent=`${group.count} images`;button.append(title,count);button.onclick=()=>openGroup(group.name);list.append(button)}}
  async function openGroup(name){activeManualGroupName=name;detail.innerHTML='<p class="muted">Loading collection…</p>';try{const response=await fetch('/api/manual-groups/'+encodeURIComponent(name),{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);detail.replaceChildren();for(const [path,names] of Object.entries(data.manual_groups_by_path||{}))manualGroupMembershipsByPath[path]=names;for(const path of data.paths)updateManualGroupMembership(path,data.name);const title=document.createElement('h2');title.textContent=data.name;const explanation=document.createElement('p');explanation.className='muted';explanation.textContent=`${data.count} images. This is a visual collection; membership does not identify a person, assign an identity, or move files.`;const remove=document.createElement('button');remove.type='button';remove.textContent='Remove selected from collection';remove.onclick=()=>removeMembers(data.name);const status=document.createElement('div');status.className='status';status.id='manualGroupDetailStatus';const grid=document.createElement('div');grid.className='manual-group-grid';for(const path of data.paths){const card=document.createElement('div');card.className='manual-group-member';const link=document.createElement('a');link.href='/media?path='+encodeURIComponent(path);link.onclick=async event=>{event.preventDefault();try{if(!identityOptions.length)await loadIdentityOptions();window.openReviewModalForPaths(event,link,data.paths)}catch(error){const status=document.querySelector('#manualGroupDetailStatus');if(status)status.textContent=`Unable to load identities: ${error.message}`}};const image=document.createElement('img');image.loading='lazy';image.src=link.href;image.alt=path;link.append(image);link.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path));const label=document.createElement('label');const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.className='manual-group-remove';checkbox.dataset.path=path;label.append(checkbox,document.createTextNode(' Remove'));const caption=document.createElement('small');caption.textContent=path;card.append(link,label,caption);grid.append(card)}detail.append(title,explanation,remove,status,grid)}catch(error){detail.innerHTML=`<p role="alert">${esc(error.message)}</p>`}}
  window.picorgRefreshManualGroupView=async function(){if(viewMode!=='manual-groups'||!activeManualGroupName)return;await fetchGroups();renderGroups();await openGroup(activeManualGroupName)};
  async function removeMembers(name){const paths=[...detail.querySelectorAll('.manual-group-remove:checked')].map(input=>input.dataset.path);const status=document.querySelector('#manualGroupDetailStatus');if(!paths.length){if(status)status.textContent='Select images to remove.';return}try{const response=await fetch('/api/manual-groups/'+encodeURIComponent(name)+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);await fetchGroups();renderGroups();await openGroup(name)}catch(error){if(status)status.textContent=error.message}}
  async function showManualGroups(){manualGroupsActive=true;viewMode='manual-groups';selected=null;selectedIdentity=null;setTab();const next=document.querySelector('#nextPage');if(next){next.hidden=true;next.disabled=true}document.querySelector('#clusterMode')?.setAttribute('hidden','hidden');document.querySelector('#hideConfirmed')?.closest('label')?.setAttribute('hidden','hidden');if(filter){filter.value='';filter.placeholder='Search manual collections'}const label=document.querySelector('label[for="filter"]');if(label)label.textContent='Search manual collections';history.replaceState(null,'',`${location.pathname}?view=manual-groups`);detail.innerHTML='<p class="muted">Choose a collection.</p>';await fetchGroups();renderGroups()}
  const priorModalRender=renderMediaModal;renderMediaModal=function(){priorModalRender();const frame=document.querySelector('#mediaModalContent .media-preview-frame'),path=modalPaths[modalIndex];if(frame&&path&&!frame.querySelector('.manual-group-badges'))frame.insertAdjacentHTML('beforeend',manualGroupBadgeMarkup(path));const toolbar=document.querySelector('#mediaModalContent .media-modal-toolbar');if(!toolbar||toolbar.querySelector('.manual-group-modal'))return;const row=document.createElement('div');row.className='manual-group-modal';const heading=document.createElement('strong');heading.className='modal-section-label';heading.textContent='Visual collection';const input=document.createElement('input');input.type='text';input.setAttribute('list','modalManualGroupNames');input.id='modalManualGroupName';input.placeholder='Choose or create a visual collection';input.setAttribute('aria-label','Manual collection for this image');const options=document.createElement('datalist');options.id='modalManualGroupNames';const recent=createRecentGroupPicker(input,'modalRecentManualGroups');const button=document.createElement('button');button.type='button';button.textContent='Add image to collection';const status=document.createElement('div');status.className='status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');const note=document.createElement('small');note.textContent='Collection tag only; individual identity matching and face markers are unchanged.';row.append(heading,input,options,recent,button,status,note);toolbar.append(row);fetchGroups().then(groups=>{for(const group of groups){const option=document.createElement('option');option.value=group.name;options.append(option)}}).catch(error=>{status.textContent=error.message});button.onclick=async()=>{const name=input.value.trim(),path=modalPaths[modalIndex];if(!name){status.textContent='Choose or enter a collection name.';return}if(!path){status.textContent='No image is open.';return}button.disabled=true;try{const response=await fetch('/api/manual-groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,paths:[path]})}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);status.textContent=`Added this image to ${data.name}.`;updateManualGroupMembership(path,data.name);rememberManualGroup(data.name);advanceAfterManualGroupAssignment(path,true);fetchGroups().then(groups=>options.replaceChildren(...groups.map(group=>{const option=document.createElement('option');option.value=group.name;return option}))).catch(error=>{status.textContent+=` Collection list refresh failed: ${error.message}`})}catch(error){status.textContent=error.message}finally{button.disabled=false}}};
  addTab();
  const priorShow=window.showView;window.showView=async function(mode){if(mode==='manual-groups')return showManualGroups();manualGroupsActive=false;setTab();return priorShow(mode)};
  filter?.addEventListener('input',()=>{if(manualGroupsActive)renderGroups()});
  const priorSelect=window.select;window.select=async function(id){const result=await priorSelect(id);if(!manualGroupsActive&&selected===id){const form=document.querySelector('#detail .form'),host=document.querySelector('#imageAssignTools')||form?.parentElement;if(form&&host&&!document.querySelector('#manualGroupTools')){const box=document.createElement('section');box.id='manualGroupTools';box.className='manual-group-tools';const label=document.createElement('label');label.textContent='Manual collection (new or existing)';const input=document.createElement('input');input.id='manualGroupName';input.setAttribute('list','manualGroupNames');input.placeholder='e.g. gothgroup';const options=document.createElement('datalist');options.id='manualGroupNames';const recent=createRecentGroupPicker(input,'recentManualGroups');try{for(const group of await fetchGroups()){const option=document.createElement('option');option.value=group.name;options.append(option)}}catch(_error){}const button=document.createElement('button');button.type='button';button.textContent='Add selected images';button.onclick=async()=>{const name=input.value.trim(),paths=[...document.querySelectorAll('#detail .imageSelect:checked')].map(item=>item.dataset.path);if(!name)return status.textContent='Enter or choose a collection name.';if(!paths.length)return status.textContent='Select the images to add using their checkboxes.';if(paths.length>500)return status.textContent='Add up to 500 selected images at a time.';if(!confirm(`Add ${paths.length} selected image(s) to ${name}? This saves collection membership only; it does not assign an identity or move files.`))return;button.disabled=true;try{const response=await fetch('/api/manual-groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,paths})}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);status.textContent=`Added ${data.added} selected image(s) to ${data.name}; ${data.count} images in collection.`;for(const path of paths)updateManualGroupMembership(path,data.name);rememberManualGroup(data.name);await fetchGroups();options.replaceChildren(...manualGroups.map(group=>{const option=document.createElement('option');option.value=group.name;return option}))}catch(error){status.textContent=error.message}finally{button.disabled=false}};const status=document.createElement('div');status.className='manual-group-status';status.setAttribute('role','status');box.append(label,input,options,recent,button,status);host.append(box)}}return result};
  addTab();
})();
"""
IDENTITY_RECONCILIATION_NAV_SCRIPT = r"""
(()=>{const tabs=document.querySelector('.view-tabs');if(!tabs||document.querySelector('#identityReconciliationTab'))return;const button=document.createElement('button');button.id='identityReconciliationTab';button.type='button';button.textContent='Registry review';button.title='Review local manual identities against the shared registry';button.addEventListener('click',()=>{window.location.href='/identity-reconciliation'});tabs.append(button)})();
"""
HTML_PAGE = HTML_PAGE.replace('</script>', MANUAL_GROUPS_SCRIPT + IDENTITY_RECONCILIATION_NAV_SCRIPT + '</script>', 1)

REVIEW_WORKFLOW_CSS = r"""
.view-tabs{display:flex!important;flex-wrap:wrap;gap:6px}
.view-tabs button{flex:1 1 130px;min-width:0}
.queue-preview,.identity-provenance,.cluster-evidence-summary{grid-column:1/-1;padding:8px 10px;border-radius:6px;background:#172832;color:#c8d7df;line-height:1.45;overflow-wrap:anywhere}
.queue-preview{font-size:.9rem;border-left:3px solid #d6a85e}
.queue-resume,.compare-references{margin:8px 0}
.compare-reference-panel{margin:10px 0;padding:10px;border:1px solid #405563;border-radius:8px}
.compare-reference-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(180px,100%),1fr));gap:8px;margin-top:8px}
.compare-reference-card{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;padding:8px;border:1px solid #405563;border-radius:7px;overflow:hidden}
.compare-reference-card b{grid-column:1/-1;overflow-wrap:anywhere}
.compare-reference-card img{width:100%;height:130px;object-fit:contain;background:#091117}
.assignment-queue .queue-row{grid-template-columns:auto minmax(100px,auto) auto minmax(0,1fr) auto auto}
@media(max-width:620px){.assignment-queue .queue-row{grid-template-columns:repeat(2,minmax(0,1fr))}.queue-row small,.queue-preview{grid-column:1/-1}.compare-reference-card img{height:100px}}
"""
HTML_PAGE = HTML_PAGE.replace('</style>', REVIEW_WORKFLOW_CSS + '</style>', 1)
HTML_PAGE = HTML_PAGE.replace(
    "${x.count} files · ${status}",
    "${x.hidden_count} hidden · ${x.grouped_unassigned_count} grouped, unassigned · ${x.total_count} total · ${x.unassigned_count} to review · ${status}",
)


HTML_PAGE = HTML_PAGE.replace(
    "let currentClusterImageDecisions={},",
    "let currentFaceLinkScores={};let currentClusterImageDecisions={},",
    1,
)
HTML_PAGE = HTML_PAGE.replace(
    "manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(x.manual_groups_by_path||{})};currentClusterImageDecisions=",
    "manualGroupMembershipsByPath={...manualGroupMembershipsByPath,...(x.manual_groups_by_path||{})};currentFaceLinkScores=x.face_link_scores||{};currentClusterImageDecisions=",
    1,
)
HTML_PAGE = HTML_PAGE.replace("</style>", ".face-link-score{font-size:11px;font-weight:650;color:#c4d8e4;padding:3px 2px}.face-link-score.weak{color:#ffe09a}</style>", 1)
FACE_LINK_SCORE_SCRIPT = r"""
function addFaceLinkScores(){
  const scores=Object.entries(currentFaceLinkScores||{}).filter(([,score])=>Number.isFinite(Number(score)));
  if(!scores.length)return;
  const weakest=Math.min(...scores.map(([,score])=>Number(score)));
  document.querySelectorAll('#detail .media-tile').forEach(tile=>{
    if(tile.querySelector('.face-link-score'))return;
    const path=tile.querySelector('.imageSelect')?.dataset.path;
    const score=Number(currentFaceLinkScores[path]);
    if(!Number.isFinite(score))return;
    const weak=Math.abs(score-weakest)<0.000001;
    const label=document.createElement('div');
    label.className='face-link-score'+(weak?' weak':'');
    label.textContent=(weak?'Weakest link · ':'Face match · ')+(score*100).toFixed(1)+'%';
    label.title='Cosine similarity to cluster members already present when this image joined. This is grouping evidence, not an identity confidence.';
    tile.append(label);
  });
}
new MutationObserver(addFaceLinkScores).observe(document.querySelector('#detail'),{childList:true,subtree:true});
const originalFaceLinkModal=openMediaModal;
openMediaModal=function(event,anchor){
  originalFaceLinkModal(event,anchor);
  const caption=document.querySelector('#mediaModalContent .media-modal-caption');
  if(!caption||caption.dataset.faceLinkScore==='shown')return;
  const path=new URL(anchor.getAttribute('href'),location.href).searchParams.get('path')||'';
  const score=Number(currentFaceLinkScores[path]);
  if(Number.isFinite(score))caption.textContent+=' · Face match '+(score*100).toFixed(1)+'%';
  caption.dataset.faceLinkScore='shown';
};
"""
HTML_PAGE = HTML_PAGE.replace("</script>", FACE_LINK_SCORE_SCRIPT + "</script>", 1)

HTML_PAGE = HTML_PAGE.replace(
    "</style>",
    ".cluster-navigation{display:flex;align-items:center;gap:8px;margin:8px 0 12px}.cluster-navigation button{padding:5px 10px;font-size:12px}.cluster-navigation button[data-direction=\"1\"]{min-height:34px;font-weight:700}.cluster-navigation-label{font-size:12px;color:#aec1cd}",
    1,
)
CLUSTER_NAVIGATION_SCRIPT = r"""
function visibleClusterIds(){
  return [...document.querySelectorAll('#list .cluster')]
    .map(card=>card.dataset.clusterId||card.getAttribute('onclick')?.match(/select\('([^']+)'\)/)?.[1])
    .filter(Boolean);
}
function updateClusterNavigation(){
  const detail=document.querySelector('#detail');
  if(!selected||!detail?.querySelector('.grid')||viewMode==='identities'||viewMode==='settings'){
    detail?.querySelector('#clusterNavigation')?.remove();
    return;
  }
  const heading=detail.querySelector('h2');
  if(!heading)return;
  let nav=detail.querySelector('#clusterNavigation');
  if(!nav){
    nav=document.createElement('nav');nav.id='clusterNavigation';nav.className='cluster-navigation';nav.setAttribute('aria-label','Cluster navigation');
    nav.innerHTML='<button type="button" data-direction="-1">← Previous cluster</button><span class="cluster-navigation-label" aria-live="polite"></span><button type="button" data-direction="1">Next cluster →</button>';
    nav.querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>navigateCluster(Number(button.dataset.direction))));
    heading.after(nav);
  }
  const ids=visibleClusterIds(),index=ids.indexOf(selected);
  const buttons=nav.querySelectorAll('button');
  buttons[0].disabled=index<=0;
  buttons[1].disabled=index<0||(!(index<ids.length-1)&&!hasNext);
  const label=nav.querySelector('.cluster-navigation-label');
  const text=index>=0?`${index+1} of ${ids.length}${hasNext&&index===ids.length-1?'+':''}`:'Cluster not in filtered list';
  if(label.textContent!==text)label.textContent=text;
}
async function navigateCluster(direction){
  const ids=visibleClusterIds(),index=ids.indexOf(selected),target=ids[index+direction];
  if(target){await select(target);return}
  if(direction>0&&index===ids.length-1&&hasNext){
    const prior=new Set(ids);
    await loadClustersPage(false);
    const next=visibleClusterIds().find(id=>!prior.has(id));
    if(next)await select(next);
  }
}
new MutationObserver(updateClusterNavigation).observe(document.querySelector('#detail'),{childList:true,subtree:true});
new MutationObserver(updateClusterNavigation).observe(document.querySelector('#list'),{childList:true,subtree:true});
"""
UNDO_LAST_ACTION_SCRIPT = r"""
async function undoLastAction(){
  try{
    const latestResponse=await fetch('/api/undo/latest',{headers:{Accept:'application/json'}}),latest=await latestResponse.json();
    if(!latestResponse.ok)throw new Error(latest.error||`Request failed (${latestResponse.status})`);
    if(!latest.available){alert('There is no assignment or group addition to undo.');return}
    const action=latest.action,kind=action.kind==='group'?'group assignment':'identity assignment and move';
    if(!window.confirm(`Undo the last ${kind} for ${action.identity||'this item'} (${action.count} image(s))?`))return;
    const response=await fetch('/api/undo/latest',{method:'POST'}),result=await response.json();
    if(!response.ok)throw new Error(result.error||`Undo failed (${response.status})`);
    lastMoveId=null;modalUndoMoveId=null;
    if(result.kind==='move'){
      const restored=result.restored_paths||(result.restored||[]).map(item=>item.destination);
      for(const path of restored){if(path)currentClusterImageDecisions[path]={status:'pending'}}
      if(!document.querySelector('#mediaModal')?.hidden){modalPaths=[...restored.filter(Boolean),...modalPaths.filter(path=>!restored.includes(path))];modalIndex=0}
    }else{
      for(const path of result.removed_paths||[]){
        const names=(manualGroupMembershipsByPath[path]||[]).filter(name=>name!==result.identity);
        if(names.length)manualGroupMembershipsByPath[path]=names;else delete manualGroupMembershipsByPath[path];
      }
    }
    if(viewMode==='manual-groups'&&window.showView)await window.showView('manual-groups');
    else if(selected)await select(selected);
    if(!document.querySelector('#mediaModal')?.hidden)renderMediaModal();
    const message=result.kind==='group'?`Removed ${result.removed_paths?.length||0} image(s) from ${result.identity}.`:`Restored ${result.restored_paths?.length||result.restored?.length||0} image(s) to the review collection.`;
    const status=document.querySelector('.media-modal-status')||document.querySelector('#imageAssignTools p')||document.querySelector('#status');
    if(status)status.textContent=message;else alert(message);
    return result;
  }catch(error){alert(error.message||'Undo failed');return null}
}
window.picorgUndoLatest=undoLastAction;
undoLastMove=undoLastAction;
undoModalMove=undoLastAction;
const priorModalUndoUpdate=updateModalUndoControl;
updateModalUndoControl=function(){priorModalUndoUpdate();document.querySelectorAll('.modal-undo').forEach(button=>button.disabled=false)};
const priorUndoModalRender=renderMediaModal;
renderMediaModal=function(){priorUndoModalRender();const button=document.querySelector('#mediaModalContent .modal-undo'),actions=document.querySelector('#mediaModalContent .modal-primary-actions');if(button){button.textContent='Undo last action';button.disabled=false;button.onclick=undoLastAction;if(actions&&!actions.contains(button))actions.append(button)}};
function installGlobalUndoButton(){
  if(document.querySelector('#globalUndoLastAction'))return;
  const button=document.createElement('button');button.id='globalUndoLastAction';button.type='button';button.className='undo-last-action';button.textContent='Undo last action';button.title='Undo the most recent identity move or group assignment';button.onclick=undoLastAction;
  const host=document.querySelector('.view-tabs')||document.querySelector('.side');
  if(host){if(host.classList.contains('view-tabs'))host.append(button);else host.insertBefore(button,host.children[1]||null)}
}
installGlobalUndoButton();
new MutationObserver(installGlobalUndoButton).observe(document.body,{childList:true,subtree:true});
"""
IDENTITY_PREVIEW_CSS = r"""
#identityHoverPreview{position:fixed;z-index:10001;width:min(440px,calc(100vw - 24px));padding:10px;border:1px solid #60798a;border-radius:9px;background:#14212b;color:#edf5f8;box-shadow:0 8px 30px #000b;pointer-events:none}#identityHoverPreview[hidden]{display:none}.identity-hover-preview>strong{display:block;margin-bottom:7px;overflow-wrap:anywhere}.identity-hover-preview-images{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.identity-hover-preview-images img{width:100%;height:min(210px,32vh);object-fit:contain;background:#091117;border-radius:5px}
#identityPreviewDialog{width:min(1000px,calc(100vw - 28px));max-width:none;max-height:calc(100dvh - 28px);padding:0;border:1px solid #526a7a;border-radius:12px;background:#14212b;color:#edf5f8;box-shadow:0 16px 60px #000b}
#identityPreviewDialog:not([open]){display:none}#identityPreviewDialog[open]{display:grid;grid-template-rows:auto minmax(0,1fr)}#identityPreviewDialog::backdrop{background:#000b}
.identity-preview-heading{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:12px;padding:14px 18px;background:#192a36;border-bottom:1px solid #405563}.identity-preview-heading h2{flex:1;margin:0;font-size:1.1rem}.identity-preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(180px,100%),1fr));gap:10px;overflow:auto;padding:14px 16px 16px}.identity-preview-grid button{background:#1b2d39;color:inherit;border:1px solid #405563;border-radius:8px;padding:6px;cursor:zoom-in}.identity-preview-grid button:hover,.identity-preview-grid button:focus-visible{border-color:#92d8ef;outline:2px solid #92d8ef}.identity-preview-grid img{width:100%;height:150px;object-fit:contain;background:#091117;border-radius:4px}.identity-preview-open{font-size:12px;padding:4px 8px;margin-left:6px;white-space:nowrap}
@media(max-width:520px){.identity-preview-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px}.identity-preview-grid img{height:120px}}
"""
MODAL_COMPACT_CONTROLS_CSS = r"""
#mediaModalContent .media-modal-toolbar{gap:5px;padding:7px}
#mediaModalContent .media-modal-toolbar .modal-control-group{gap:4px;padding:5px}
#mediaModalContent .media-modal-toolbar :is(button,select,input,summary){min-width:0;min-height:28px;padding:3px 6px;font-size:12px;line-height:1.2}
#mediaModalContent .media-modal-toolbar input,#mediaModalContent .media-modal-toolbar select{min-height:30px}
#mediaModalContent .media-modal-toolbar .modal-primary-actions .approve{min-height:32px;padding:4px 8px;font-size:13px}
#mediaModalContent .media-modal-toolbar .modal-primary-actions .modal-undo{min-height:28px}
#mediaModalContent .media-modal-toolbar .modal-more-actions{gap:4px;margin-top:4px}
#mediaModalContent .media-modal-toolbar .modal-recent-identities{gap:3px}
#mediaModalContent .media-modal-toolbar .modal-recent-identities button{min-height:25px;padding:3px 5px;font-size:11px}
#mediaModalContent .media-modal-toolbar .manual-group-modal{gap:4px;padding-top:4px}
#mediaModalContent .media-modal-toolbar .manual-group-modal input,#mediaModalContent .media-modal-toolbar .manual-group-modal select{min-height:30px;padding:3px 6px}
.recent-manual-group-buttons button{flex:0 1 auto;width:fit-content;align-self:flex-start}
#mediaModalContent .recent-manual-group-buttons button{flex:0 1 auto;width:fit-content;align-self:flex-start}
"""
IDENTITY_PREVIEW_SCRIPT = r"""
(()=>{
  const dialog=document.createElement('dialog');dialog.id='identityPreviewDialog';dialog.className='identity-preview-dialog';dialog.setAttribute('aria-labelledby','identityPreviewTitle');
  const heading=document.createElement('header');heading.className='identity-preview-heading';const title=document.createElement('h2');title.id='identityPreviewTitle';title.textContent='Sorted identity images';const count=document.createElement('span');const close=document.createElement('button');close.type='button';close.textContent='Close';close.setAttribute('aria-label','Close identity preview');heading.append(title,count,close);const grid=document.createElement('div');grid.className='identity-preview-grid';dialog.append(heading,grid);document.body.append(dialog);
  function closePreview(){if(dialog.open)dialog.close()}
  close.addEventListener('click',closePreview);dialog.addEventListener('close',()=>grid.replaceChildren());dialog.addEventListener('click',event=>{if(event.target===dialog)closePreview()});
  async function showPreview(button){const picker=document.getElementById(button.dataset.picker),identity=picker?.value?.trim();if(!identity){alert('Choose a saved identity first');return}button.disabled=true;try{const response=await fetch('/api/identity-preview/'+encodeURIComponent(identity),{headers:{Accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);title.textContent=data.identity;count.textContent=`${data.count} sorted image${data.count===1?'':'s'} · showing ${data.paths.length}`;grid.replaceChildren();if(!data.paths.length){const empty=document.createElement('p');empty.className='related-identity-empty';empty.textContent='No sorted images found for this identity yet.';grid.append(empty)}for(const path of data.paths){const tile=document.createElement('button');tile.type='button';tile.title=path;const image=document.createElement('img');image.src='/media?path='+encodeURIComponent(path);image.alt='Sorted image for '+data.identity;image.loading='lazy';tile.append(image);tile.addEventListener('click',()=>{closePreview();const anchor=document.createElement('a');anchor.href='/media?path='+encodeURIComponent(path);anchor.append(image.cloneNode());openMediaModal({target:anchor,preventDefault(){}},anchor)});grid.append(tile)}dialog.showModal();close.focus()}catch(error){alert(error.message)}finally{button.disabled=false}}
 function attachPreviewButtons(){for(const picker of [document.getElementById('identity'),document.getElementById('modalIdentity')]){if(!picker||picker.dataset.previewAttached==='1')continue;picker.dataset.previewAttached='1';const button=document.createElement('button');button.type='button';button.className='identity-preview-open';button.dataset.picker=picker.id;button.textContent='Preview sorted';button.setAttribute('aria-label','Preview images already sorted for selected identity');button.addEventListener('click',()=>showPreview(button));picker.after(button)}}
 const observer=new MutationObserver(attachPreviewButtons);observer.observe(document.getElementById('detail'),{childList:true,subtree:true});observer.observe(document.body,{childList:true,subtree:true});attachPreviewButtons();
})();
"""
IDENTITY_HOVER_PREVIEW_SCRIPT = r"""
(()=>{
  const cache=new Map(),popover=document.createElement('div');popover.id='identityHoverPreview';popover.className='identity-hover-preview';popover.setAttribute('role','tooltip');popover.setAttribute('aria-hidden','true');popover.hidden=true;document.body.append(popover);
  let timer=null,active=null,request=0;
  function decorate(){document.querySelectorAll('#recentIdentityList button,#modalRecentIdentityList button,#modalPinnedIdentities .modal-pinned-identity > button:not(.modal-unpin-identity),.related-identity-card').forEach(button=>{if(button.dataset.identityPreview)return;const name=button.matches('.related-identity-card')?button.querySelector('strong')?.textContent:button.textContent;if(name?.trim()){button.dataset.identityPreview=name.trim();button.setAttribute('aria-describedby','identityHoverPreview')}})}
  function hide(){clearTimeout(timer);timer=null;active=null;request++;popover.hidden=true;popover.setAttribute('aria-hidden','true');popover.replaceChildren()}
  function place(button){const r=button.getBoundingClientRect(),w=popover.offsetWidth,h=popover.offsetHeight;let left=Math.max(12,Math.min(window.innerWidth-w-12,r.left+(r.width-w)/2)),top=r.bottom+8;if(top+h>window.innerHeight-12)top=Math.max(12,r.top-h-8);popover.style.left=`${left}px`;popover.style.top=`${top}px`}
  async function show(button){active=button;const id=++request,name=button.dataset.identityPreview;let paths=cache.get(name);if(!paths){try{const response=await fetch('/api/identity-preview/'+encodeURIComponent(name),{headers:{Accept:'application/json'}});if(!response.ok)return;const data=await response.json();paths=(data.paths||[]).slice(0,2);if(paths.length)cache.set(name,paths)}catch(_){return}}if(active!==button||id!==request||!paths?.length)return;popover.replaceChildren();const label=document.createElement('strong');label.textContent=name;popover.append(label);const row=document.createElement('div');row.className='identity-hover-preview-images';for(const path of paths){const image=document.createElement('img');image.src='/media?path='+encodeURIComponent(path);image.alt=`Example image for ${name}`;image.loading='eager';row.append(image)}popover.append(row);popover.hidden=false;popover.setAttribute('aria-hidden','false');place(button)}
  function schedule(button){if(!button?.dataset.identityPreview)return;clearTimeout(timer);active=button;timer=setTimeout(()=>show(button),1800)}
  document.addEventListener('pointerover',event=>{const button=event.target.closest?.('[data-identity-preview]');if(button&&!button.contains(event.relatedTarget))schedule(button)});
  document.addEventListener('pointerout',event=>{const button=event.target.closest?.('[data-identity-preview]');if(button&&!button.contains(event.relatedTarget))hide()});
  document.addEventListener('focusin',event=>schedule(event.target.closest?.('[data-identity-preview]')));document.addEventListener('focusout',event=>{if(event.target.closest?.('[data-identity-preview]'))hide()});
  window.addEventListener('scroll',hide,true);window.addEventListener('resize',hide);new MutationObserver(decorate).observe(document.body,{childList:true,subtree:true});decorate();
})();
"""
HTML_PAGE = HTML_PAGE.replace("</script>", CLUSTER_NAVIGATION_SCRIPT + "\n" + UNDO_LAST_ACTION_SCRIPT + "\n" + IDENTITY_HOVER_PREVIEW_SCRIPT + "\n" + IDENTITY_PREVIEW_SCRIPT + "init();</script>", 1)
HTML_PAGE = HTML_PAGE.replace("</style>", MODAL_COMPACT_CONTROLS_CSS + "</style>", 1)
if "</style>" not in HTML_PAGE:
    HTML_PAGE = HTML_PAGE.replace("</head>", "</style></head>", 1)
HTML_PAGE = HTML_PAGE.replace("</style>", IDENTITY_PREVIEW_CSS + MODAL_COMPACT_CONTROLS_CSS + "</style>", 1)

IDENTITY_RECONCILIATION_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Identity registry review · PicOrg</title>
<style>
:root{color-scheme:dark;font:15px/1.45 system-ui,sans-serif;background:#101820;color:#eaf1f5}*{box-sizing:border-box}body{margin:0;padding:18px;max-width:1500px;margin-inline:auto}header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}h1{font-size:1.35rem;margin:0;flex:1}button,input,select{font:inherit;color:inherit;background:#1c2c38;border:1px solid #526879;border-radius:6px;padding:7px 10px}button{cursor:pointer}button:hover,button:focus-visible{border-color:#93d7ed;outline:2px solid #93d7ed}a{color:#a8dff2}.notice{padding:10px 12px;border-left:4px solid #e2b758;background:#2b281e;margin:12px 0}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.toolbar input{flex:1;min-width:220px}.toolbar select{min-width:185px}.summary{color:#b7cad4;margin:10px 0}.cards{display:grid;gap:10px}.card{border:1px solid #405663;border-radius:9px;padding:12px;background:#172630}.card h2{font-size:1.05rem;margin:0 0 3px;overflow-wrap:anywhere}.aliases,.notes,.candidate-meta{color:#b6c6cf;font-size:.9rem;overflow-wrap:anywhere}.candidate-search{width:100%;margin:10px 0 6px}.suggestions{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr));gap:6px}.suggestion{text-align:left;display:grid;gap:3px}.suggestion.selected{border-color:#8fcf9d;outline:1px solid #8fcf9d}.controls{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.controls input{flex:1;min-width:180px}.primary{background:#274559}.status{margin-top:8px;color:#a7d8b0}.decision{margin-top:8px;padding:7px;border-radius:5px;background:#202f39;color:#cddce4}.empty{padding:22px;text-align:center;color:#b7cad4}@media(max-width:600px){body{padding:10px}.suggestions{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Manual identity reconciliation</h1><a href="/">Back to PicOrg</a><button id="export" type="button">Download review manifest</button></header>
<div class="notice"><strong>Name matches are suggestions only.</strong> Confirm a link or a genuinely new identity below. Decisions are saved to a PicOrg review file for the registry owner; this page does not edit the local or shared registry.</div>
<div class="toolbar"><input id="filter" type="search" placeholder="Filter manual identities, aliases, or notes"><select id="state"><option value="all">All candidates</option><option value="unreviewed">Unreviewed</option><option value="ready_for_registry_owner">Confirmed for registry owner</option><option value="deferred">Deferred</option></select><button id="reload" type="button">Refresh</button></div><div id="summary" class="summary">Loading registry candidates…</div><main id="cards" class="cards"></main>
<script>
(()=>{
const cards=document.getElementById('cards'),summary=document.getElementById('summary'),filter=document.getElementById('filter'),state=document.getElementById('state');let data={manual_entries:[],shared_identities:[],decisions:{}};
const norm=value=>String(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase().replace(/[^a-z0-9]+/g,'');
const names=item=>[item.id,item.primary_folder,...(item.display_names||[]),...(item.aliases||[])].filter(Boolean);
function score(source,target){const q=norm(source),targets=names(target).map(norm).filter(Boolean);if(!q)return 0;let best=0;for(const value of targets){if(value===q)best=Math.max(best,1);else if(value.includes(q)||q.includes(value))best=Math.max(best,.78*Math.min(value.length,q.length)/Math.max(value.length,q.length));const a=new Set(q.match(/[a-z0-9]{2,}/g)||[]),b=new Set(value.match(/[a-z0-9]{2,}/g)||[]);if(a.size&&b.size){let n=0;for(const t of a)if(b.has(t))n++;best=Math.max(best,.55*n/(a.size+b.size-n))}}return best}
function text(tag,value,cls){const node=document.createElement(tag);node.textContent=value||'';if(cls)node.className=cls;return node}
async function save(source,action,extra){const response=await fetch('/api/identity-reconciliation',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify({source,action,...extra})});const result=await response.json();if(!response.ok)throw new Error(result.error||`Save failed (${response.status})`);data.decisions[source]=result.decision;render()}
function render(){const query=norm(filter.value),status=state.value;const rows=data.manual_entries.filter(item=>{const d=item.decision||data.decisions[item.canonical];const s=d?.status||'unreviewed';return(!query||norm([item.canonical,...item.aliases,item.notes].join(' ')).includes(query))&&(status==='all'||s===status||status==='unreviewed'&&!d)});const decided=Object.values(data.decisions).filter(item=>item?.status==='ready_for_registry_owner').length;summary.textContent=`${rows.length} shown · ${data.manual_entries.length} unmatched manual entries · ${data.shared_identities.length} confirmed shared identities · ${decided} decisions ready for registry-owner review`;cards.replaceChildren();if(!rows.length){cards.append(text('div','No candidates match this filter.','empty'));return}for(const item of rows)cards.append(renderCard(item))}
function renderCard(item){const card=document.createElement('section');card.className='card';card.append(text('h2',item.canonical));if(item.aliases.length)card.append(text('div','Local aliases: '+item.aliases.join(', '),'aliases'));if(item.notes)card.append(text('div','Notes: '+item.notes,'notes'));const decision=data.decisions[item.canonical]||item.decision;if(decision){const detail=decision.action==='link'?`Linked to ${decision.target}`:decision.action==='new'?`Proposed new identity ${decision.proposed_id}`:'Left unresolved';card.append(text('div',`${decision.status}: ${detail}`, 'decision'))}
 const search=document.createElement('input');search.className='candidate-search';search.type='search';search.placeholder='Search confirmed shared identities';search.setAttribute('aria-label',`Search shared identities for ${item.canonical}`);const suggestions=document.createElement('div');suggestions.className='suggestions';const actions=document.createElement('div');actions.className='controls';const proposed=document.createElement('input');proposed.value=item.canonical;proposed.setAttribute('aria-label',`Proposed new shared identity for ${item.canonical}`);const makeNew=document.createElement('button');makeNew.textContent='Confirm as genuinely new';makeNew.title='Save a proposal for the shared registry owner; does not create the identity';makeNew.onclick=async()=>{if(!window.confirm(`Record ${proposed.value.trim()} as a proposed new shared identity?`))return;try{await save(item.canonical,'new',{proposed_id:proposed.value.trim()})}catch(error){alert(error.message)}};const defer=document.createElement('button');defer.textContent='Leave unresolved';defer.onclick=async()=>{try{await save(item.canonical,'defer',{})}catch(error){alert(error.message)}};actions.append(proposed,makeNew,defer);let selected=null;
 function showSuggestions(){const query=search.value.trim(),needle=norm(query||item.canonical);suggestions.replaceChildren();const matches=data.shared_identities.map(target=>({target,score:Math.max(score(item.canonical,target),query?score(query,target):0)})).filter(row=>query?names(row.target).some(name=>norm(name).includes(needle))||row.score>=.22:row.score>=.12).sort((a,b)=>b.score-a.score||a.target.id.localeCompare(b.target.id)).slice(0,8);for(const {target,score:value} of matches){const b=document.createElement('button');b.type='button';b.className='suggestion'+(selected?.id===target.id?' selected':'');b.append(text('strong',target.id));const metadata=[target.primary_folder,...target.display_names.slice(0,4),...target.sources.slice(0,3)].filter(Boolean);b.append(text('span',metadata.join(' · '),'candidate-meta'));b.append(text('small',`Name similarity hint · ${Math.round(value*100)}% · not identity evidence`,'candidate-meta'));b.onclick=()=>{selected=target;showSuggestions()};suggestions.append(b)}if(!matches.length)suggestions.append(text('div','No name-based suggestions. Search by a known alias or leave unresolved.','empty'));if(selected){const confirmLink=document.createElement('button');confirmLink.type='button';confirmLink.className='primary';confirmLink.textContent=`Confirm link to ${selected.id}`;confirmLink.onclick=async()=>{if(!window.confirm(`Record ${item.canonical} → ${selected.id} for registry-owner review?`))return;try{await save(item.canonical,'link',{target:selected.id})}catch(error){alert(error.message)}};suggestions.append(confirmLink)}}
 search.addEventListener('input',()=>{selected=null;showSuggestions()});showSuggestions();card.append(search,suggestions,actions);return card}
async function load(){summary.textContent='Loading registry candidates…';try{const response=await fetch('/api/identity-reconciliation',{headers:{Accept:'application/json'},cache:'no-store'});data=await response.json();if(!response.ok)throw new Error(data.error||`Load failed (${response.status})`);render()}catch(error){cards.replaceChildren(text('p',error.message,'empty'));summary.textContent='Unable to load candidates'}}
filter.addEventListener('input',render);state.addEventListener('change',render);document.getElementById('reload').onclick=load;document.getElementById('export').onclick=async()=>{try{const response=await fetch('/api/identity-reconciliation',{headers:{Accept:'application/json'}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'Export failed');const blob=new Blob([JSON.stringify({schema_version:1,generated_at:new Date().toISOString(),manual_entries:payload.manual_entries.map(({canonical,aliases,notes})=>({canonical,aliases,notes})),decisions:payload.decisions},null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='picorg-identity-reconciliation-review.json';a.click();URL.revokeObjectURL(url)}catch(error){alert(error.message)}};load();
})();
</script></body></html>"""

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, help="audit JSON; defaults to newest file in /tmp/picorg_sorted_audit")
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--image-decisions", type=Path, default=DEFAULT_IMAGE_DECISIONS)
    parser.add_argument("--review-identities", type=Path, default=DEFAULT_REVIEW_IDENTITIES)
    parser.add_argument("--review-ledger", type=Path, default=DEFAULT_REVIEW_LEDGER, help="append-only JSONL review event ledger")
    parser.add_argument("--evidence-db", type=Path, default=DEFAULT_EVIDENCE_DB, help="durable SQLite evidence/assignment store")
    parser.add_argument("--assorted-root", type=Path, default=DEFAULT_ASSORTED_ROOT, help="read-only assorted folder root")
    parser.add_argument("--assorted-associations", type=Path, default=DEFAULT_ASSORTED_ASSOCIATIONS, help="folder-to-identity association ledger")
    parser.add_argument("--export-registry", action="store_true", help="promote confirmed decisions into the registry")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address; use 127.0.0.1 for local-only access")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None, help="token for non-LAN UI/API clients; prefer PICORG_UI_TOKEN (PICORG_UI_AUTH=1 forces it everywhere)")
    parser.add_argument("--server", choices=("waitress", "flask"), default=os.environ.get("PICORG_UI_SERVER", "waitress"), help="WSGI server (default: waitress; flask is development-only)")
    args = parser.parse_args()
    if args.export_registry:
        print(f"promoted {export_confirmed_decisions(args.decisions, args.registry)} confirmed decisions")
        return 0
    audit_path = args.audit or latest_audit()
    app = create_app(
        audit_path,
        args.decisions,
        DEFAULT_OVERRIDES,
        args.image_decisions,
        args.review_identities,
        ui_token=args.token,
        ledger_path=args.review_ledger,
        evidence_db_path=args.evidence_db,
        assorted_root=args.assorted_root,
        assorted_associations_path=args.assorted_associations,
    )
    if args.server == "flask":
        LOGGER.warning("using Flask development server; use --server waitress for LAN operation")
        app.run(host=args.host, port=args.port, debug=False)
        return 0
    try:
        from waitress import serve
    except ImportError as exc:
        print("error: Waitress is required for the production UI; install requirements-review.txt or set --server flask for local development", flush=True)
        raise SystemExit(2) from exc
    try:
        threads = max(1, min(16, int(os.environ.get("PICORG_UI_THREADS", "4"))))
    except ValueError:
        threads = 4
    serve(app, host=args.host, port=args.port, threads=threads, ident="picorg-review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
