#!/usr/bin/env python3
"""Reconcile durable confirmed assignments into canonical folders and markers.

This is intentionally idempotent and fail-closed: only records with
``status=confirmed`` are considered, protected MD/RD roots are never moved,
and a missing/unreadable source is reported rather than deleted or retried
blindly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import picorg_sorter as sorter
from picorg_health import health_report
from identity_evidence_store import list_assignment_queue, update_assignment_status
from review_ui import (
    DEFAULT_FACE_MARKERS,
    DEFAULT_DECISIONS,
    DEFAULT_IMAGE_DECISIONS,
    DEFAULT_PENDING_ASSIGNMENTS,
    DEFAULT_REVIEW_DEST_ROOT,
    _atomic_json_write,
    _image_decision_key,
    _read_decisions,
    _read_face_markers,
    _read_image_decisions,
    _read_pending_assignments,
    _write_pending_assignments,
    _relink_path_records,
    move_review_paths,
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _cluster_paths(audit_path: Path | None) -> dict[str, list[str]]:
    if audit_path is None:
        return {}
    payload = _load_json(audit_path)
    clusters = payload.get("clusters") or payload.get("results") or []
    if isinstance(clusters, dict):
        clusters = clusters.values()
    result: dict[str, list[str]] = {}
    for item in clusters:
        if not isinstance(item, dict) or not item.get("cluster_id"):
            continue
        result[str(item["cluster_id"])] = [str(path) for path in item.get("paths", []) if str(path)]
    return result


def _confirmed_rows(
    image_decisions_path: Path,
    decisions_path: Path,
    audit_path: Path | None,
    pending_assignments_path: Path = DEFAULT_PENDING_ASSIGNMENTS,
    evidence_db_path: Path | None = None,
) -> list[dict[str, Any]]:
    # A queued review is the newest operator intent. Resolve it before older
    # durable decisions so a reassignment cannot be applied to the old identity
    # first (which would move the source away before the queued row is seen).
    by_path: dict[str, dict[str, Any]] = {}
    if evidence_db_path is not None:
        # Errors/conflicts are retained for inspection and explicit retry; do
        # not move them again on every scheduler cycle.
        for item in list_assignment_queue(evidence_db_path, statuses=("pending", "retry")):
            if item.get("path") and item.get("identity"):
                by_path[_image_decision_key(str(item["path"]))] = {
                    **item,
                    "status": "confirmed",
                    "sha256": item.get("expected_sha256"),
                    "queued": True,
                    "source": item.get("source") or "identity_evidence_store",
                }
    for item in _read_pending_assignments(pending_assignments_path).values():
        if item.get("status") == "confirmed" and item.get("path"):
            by_path[_image_decision_key(str(item["path"]))] = {**item, "queued": True}
    for item in _read_image_decisions(image_decisions_path).values():
        if item.get("status") == "confirmed" and item.get("path"):
            by_path.setdefault(_image_decision_key(str(item["path"])), item)
    rows: list[dict[str, Any]] = list(by_path.values())
    clusters = _cluster_paths(audit_path)
    for item in _read_decisions(decisions_path).values():
        if item.get("status") != "confirmed":
            continue
        for path in clusters.get(str(item.get("cluster_id")), []):
            rows.append({**item, "path": path, "scope": "cluster"})
    return rows


def _marker_record(path: str, identity: str, family: str, status: str) -> dict[str, Any]:
    media = Path(path)
    try:
        digest = sorter.file_sha256(media)
    except OSError:
        digest = None
    return {
        "key": _image_decision_key(path),
        "path": path,
        "identity": identity,
        "family": family,
        "status": status,
        "sha256": digest,
        "source": "reconcile_confirmed",
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def reconcile(
    *,
    image_decisions_path: Path = DEFAULT_IMAGE_DECISIONS,
    decisions_path: Path = DEFAULT_DECISIONS,
    markers_path: Path = DEFAULT_FACE_MARKERS,
    audit_path: Path | None = None,
    pending_assignments_path: Path = DEFAULT_PENDING_ASSIGNMENTS,
    evidence_db_path: Path | None = None,
    apply: bool = False,
    allow_partial_health: bool = False,
) -> dict[str, Any]:
    protected_roots = (
        Path("/mnt/elements16a/Pron/metadaily/downloads"),
        Path("/mnt/elements16a/Pron/redditdaily/downloads"),
    )
    protected_health = health_report(protected_roots)
    rows = _confirmed_rows(image_decisions_path, decisions_path, audit_path, pending_assignments_path, evidence_db_path)
    if apply and not protected_health["healthy"] and (not allow_partial_health or not rows):
        return {
            "confirmed_records": 0,
            "pending_records": 0,
            "candidates": 0,
            "moved": 0,
            "missing": 0,
            "errors": [{"path": path, "error": "protected reference root unavailable; apply disabled"} for path in protected_health["unavailable_paths"]],
            "applied_pending": 0,
            "apply": False,
            "blocked": "protected_reference_health",
            "protected_health": protected_health,
        }
    markers = _read_face_markers(markers_path)
    allowed_roots = {path.resolve() for path in sorter.DEFAULT_INTAKE_ROOTS}
    destination_root = DEFAULT_REVIEW_DEST_ROOT.resolve()
    allowed_roots.add(destination_root)
    candidates = moved = 0
    missing = 0
    errors: list[dict[str, str]] = []
    if apply and not protected_health["healthy"]:
        # Protected roots are read-only reference sources and are never move
        # targets. Local queued assignments can still be safely reconciled
        # while an unrelated protected mount is offline.
        errors.extend({"path": path, "error": "protected reference root unavailable; local assignments continued"} for path in protected_health["unavailable_paths"])
    applied_pending: set[str] = set()
    seen: set[tuple[str, str]] = set()
    durable_results: dict[int, tuple[str, str | None]] = {}
    durable_moves: dict[int, dict[str, Any]] = {}
    started = time.monotonic()
    last_progress = started
    try:
        progress_interval = max(1.0, float(os.environ.get("PICORG_PROGRESS_SECONDS", "15")))
    except ValueError:
        progress_interval = 15.0
    for index, row in enumerate(rows, 1):
        now = time.monotonic()
        if index == 1 or now - last_progress >= progress_interval:
            elapsed = max(0.1, now - started)
            print(
                f"[reconcile] progress {index}/{len(rows)} candidates={candidates} "
                f"moved={moved} missing={missing} elapsed={elapsed:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            last_progress = now
        identity = str(row.get("identity") or "").strip()
        family = str(row.get("family") or "review").strip()
        raw_path = str(row.get("canonical_path") or row.get("path") or "").strip()
        if not identity or not raw_path:
            continue
        key = (raw_path, identity)
        if key in seen:
            continue
        seen.add(key)
        source = Path(raw_path)
        if not source.is_file():
            missing += 1
            errors.append({"path": raw_path, "error": "missing or unreadable"})
            if row.get("assignment_id"):
                durable_results[int(row["assignment_id"])] = ("error", "missing or unreadable")
            continue
        expected_sha256 = str(row.get("sha256") or "").strip()
        if row.get("queued") and not expected_sha256:
            errors.append({"path": raw_path, "error": "queued fingerprint unavailable; refusing unverifiable assignment"})
            if row.get("assignment_id"):
                durable_results[int(row["assignment_id"])] = ("conflict", "queued fingerprint unavailable")
            continue
        if row.get("queued") and expected_sha256:
            try:
                actual_sha256 = sorter.file_sha256(source)
            except OSError as exc:
                errors.append({"path": raw_path, "error": f"queued fingerprint unreadable: {exc}"})
                if row.get("assignment_id"):
                    durable_results[int(row["assignment_id"])] = ("error", str(exc))
                continue
            if actual_sha256 != expected_sha256:
                errors.append({"path": raw_path, "error": "queued fingerprint changed; refusing stale assignment"})
                if row.get("assignment_id"):
                    durable_results[int(row["assignment_id"])] = ("conflict", "queued fingerprint changed")
                continue
        candidates += 1
        source = source.resolve()
        if apply or not row.get("queued"):
            markers[_image_decision_key(str(source))] = _marker_record(str(source), identity, family, "confirmed")
        if apply and row.get("queued") and (source == destination_root or destination_root in source.parents):
            applied_pending.add(_image_decision_key(str(row.get("path") or source)))
            if row.get("assignment_id"):
                assignment_id = int(row["assignment_id"])
                durable_results[assignment_id] = ("applied", None)
                durable_moves[assignment_id] = {
                    "path": str(source),
                    "identity": identity,
                    "family": family,
                    "expected_sha256": expected_sha256,
                    "moves": [],
                }
        if not apply or source == destination_root or destination_root in source.parents:
            continue
        result = move_review_paths([str(source)], identity, family, allowed_roots, destination_root)
        errors.extend({"path": item.get("source", str(source)), "error": item.get("error", "move failed")} for item in result["errors"])
        if result["moved"]:
            _relink_path_records(result["moved"], markers)
            moved += len(result["moved"])
            if row.get("queued"):
                applied_pending.add(_image_decision_key(str(row.get("path") or source)))
                if row.get("assignment_id"):
                    assignment_id = int(row["assignment_id"])
                    durable_results[assignment_id] = ("applied" if apply else "pending", None)
                    if apply:
                        durable_moves[assignment_id] = {
                            "path": str(source),
                            "identity": identity,
                            "family": family,
                            "expected_sha256": expected_sha256,
                            "moves": result["moved"],
                        }
        elif row.get("queued") and row.get("assignment_id"):
            durable_results[int(row["assignment_id"])] = ("error" if apply else "pending", "move failed" if apply else None)
    if apply and evidence_db_path is not None:
        for assignment_id, (status, error) in durable_results.items():
            if status in {"applied", "error", "conflict"}:
                update_assignment_status(
                    evidence_db_path,
                    assignment_id,
                    status,
                    error,
                    applied_move=durable_moves.get(assignment_id),
                )
    # Report-only mode must not mutate an existing marker ledger. Create an
    # empty schema file only when the caller supplied a new path, preserving
    # the historical idempotent output for empty runs.
    if apply or not markers_path.exists():
        _atomic_json_write(
            markers_path,
            {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "markers": list(markers.values()) if apply else []},
        )
    if apply and applied_pending:
        pending = _read_pending_assignments(pending_assignments_path)
        for key in applied_pending:
            pending.pop(key, None)
        _write_pending_assignments(pending_assignments_path, pending)
    print(
        f"[reconcile] complete records={len(rows)} candidates={candidates} moved={moved} "
        f"missing={missing} errors={len(errors)} elapsed={time.monotonic() - started:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return {"confirmed_records": len(rows), "pending_records": sum(bool(row.get("queued")) for row in rows), "candidates": candidates, "moved": moved, "missing": missing, "errors": errors, "applied_pending": len(applied_pending), "apply": apply, "degraded": bool(apply and not protected_health["healthy"]), "protected_health": protected_health}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-decisions", dest="image_decisions_path", type=Path, default=DEFAULT_IMAGE_DECISIONS)
    parser.add_argument("--decisions", dest="decisions_path", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--markers", dest="markers_path", type=Path, default=DEFAULT_FACE_MARKERS)
    parser.add_argument("--pending-assignments", dest="pending_assignments_path", type=Path, default=DEFAULT_PENDING_ASSIGNMENTS)
    parser.add_argument("--evidence-db", dest="evidence_db_path", type=Path, default=Path(".cache/picorg/identity_evidence.sqlite3"))
    parser.add_argument("--audit", dest="audit_path", type=Path)
    parser.add_argument("--apply", action="store_true", help="move confirmed media; default is report-only")
    parser.add_argument("--allow-partial-health", action="store_true", help="continue local assignments when protected MD/RD roots are unavailable")
    args = parser.parse_args()
    print(json.dumps(reconcile(**vars(args)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
