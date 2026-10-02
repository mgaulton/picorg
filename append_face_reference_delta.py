#!/usr/bin/env python3
"""Resumable, batched face-reference append for a path delta."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PHOTO_REORG = Path("/opt/photo_reorg")
DEFAULT_CONFIG = DEFAULT_PHOTO_REORG / "config_enhanced_accurate.json"
DEFAULT_DATABASE = DEFAULT_PHOTO_REORG / "data/high_accuracy_faces.db"


def _atomic_status(path: Path, **values: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(values, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _backup_database(database: Path, backup: Path) -> None:
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(backup, timeout=30)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def _source_rows(manifest: dict[str, Any]):
    rows = manifest.get("added_paths_by_identity")
    if not isinstance(rows, dict):
        raise ValueError("manifest lacks added_paths_by_identity")
    for identity, paths in sorted(rows.items()):
        if not isinstance(paths, list):
            raise ValueError(f"invalid path list for {identity}")
        for path in paths:
            if isinstance(path, str) and path:
                yield str(identity), Path(path)


def _read_completed(journal: Path, manifest_hash: str) -> set[tuple[str, str]]:
    completed: set[tuple[str, str]] = set()
    if not journal.exists():
        return completed
    lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
    for line_number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            if line_number == len(lines) and not line.endswith(("\n", "\r")):
                # Drop an interrupted final append before later appending records.
                journal.write_text("".join(lines[:line_number - 1]), encoding="utf-8")
                break
            raise ValueError(f"corrupt completion journal line {line_number}") from exc
        if entry.get("manifest_sha256") != manifest_hash:
            raise ValueError("completion journal belongs to a different manifest")
        if entry.get("outcome") not in {"error", "missing"}:
            completed.add((entry["identity"], entry["path"]))
    return completed


def _append_completed(journal: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _decode_image(cv2: Any, image_path: Path):
    image = cv2.imread(str(image_path))
    if image is not None or image_path.suffix.lower() != ".gif":
        return image
    # OpenCV rejects some valid GIF disposal methods; use the first frame as
    # its image reader otherwise would, while retaining the BGR convention.
    from PIL import Image
    import numpy as np

    try:
        with Image.open(image_path) as source:
            source.seek(0)
            rgb = np.asarray(source.convert("RGB"))
    except (OSError, EOFError):
        return None
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _process_image(rebuilder: Any, cv2: Any, image_path: Path, person_name: str,
                   pending_encodings: list[tuple[str, bytes, str, float, str]]) -> tuple[int, int, str | None]:
    """Decode once, detect with the configured stack, and encode each accepted face once."""
    detected = accepted = 0
    try:
        image = _decode_image(cv2, image_path)
        if image is None:
            logging.warning("Could not decode image %s", image_path)
            return 0, 0, "decode_failed"
        rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        detector = rebuilder.face_detector
        recognizer = rebuilder.face_recognizer
        threshold = rebuilder.config["quality_threshold"]
        for detection in detector.detect_faces_multi_model(image):
            if detection.quality_score < threshold:
                continue
            detected += 1
            encoding = recognizer.extract_face_encoding_advanced(rgb_image, detection.location)
            if encoding is None:
                continue
            quality = recognizer.assess_face_quality(image, detection.location).overall_score
            if quality < threshold:
                continue
            pending_encodings.append((person_name, encoding.tobytes(), str(image_path),
                                      float(quality), "face_recognition"))
            recognizer.face_database.setdefault(person_name, []).append({
                "encoding": encoding, "quality": quality, "model": "face_recognition"
            })
            accepted += 1
    except Exception as exc:
        logging.exception("Error processing image %s for %s", image_path, person_name)
        return detected, accepted, f"{type(exc).__name__}: {exc}"
    return detected, accepted, None


def _commit_encodings(connection: sqlite3.Connection,
                      rows: list[tuple[str, bytes, str, float, str]]) -> None:
    if not rows:
        return
    cursor = connection.cursor()
    cursor.executemany(
        "INSERT INTO face_encodings (person_name, encoding, image_path, face_quality, model_used) "
        "VALUES (?, ?, ?, ?, ?)", rows)
    for person, encoding, _path, quality, _model in rows:
        cursor.execute("""
            INSERT OR REPLACE INTO person_metadata
                (person_name, total_faces, avg_quality, best_encoding, updated_at)
            VALUES (?,
                COALESCE((SELECT total_faces FROM person_metadata WHERE person_name=?), 0) + 1,
                COALESCE((SELECT avg_quality FROM person_metadata WHERE person_name=?), 0) *
                COALESCE((SELECT total_faces FROM person_metadata WHERE person_name=?), 0) /
                    (COALESCE((SELECT total_faces FROM person_metadata WHERE person_name=?), 0) + 1) +
                ? / (COALESCE((SELECT total_faces FROM person_metadata WHERE person_name=?), 0) + 1),
                ?, CURRENT_TIMESTAMP)
        """, (person, person, person, person, person, quality, person, encoding))
    connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--status", type=Path, default=Path("/tmp/picorg-incremental-refresh-status.json"))
    parser.add_argument("--journal", type=Path, help="completed-path journal; defaults beside status")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="paths per durable SQLite commit and resume checkpoint")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    rows = list(_source_rows(manifest))
    if not args.database.is_file():
        raise SystemExit(f"database does not exist: {args.database}")
    if not args.config.is_file():
        raise SystemExit(f"photo_reorg config does not exist: {args.config}")
    journal = args.journal or args.status.with_suffix(".completed.jsonl")
    completed = _read_completed(journal, manifest_hash)

    stop_requested = False

    def stop(signum, _frame):
        nonlocal stop_requested
        stop_requested = True
        logging.warning("received signal %s; will checkpoint after the current image", signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sys.path.insert(0, str(DEFAULT_PHOTO_REORG))
    import cv2
    from rebuild_face_database import FaceDatabaseRebuilder

    logging.getLogger().setLevel(logging.WARNING)
    rebuilder = FaceDatabaseRebuilder(str(args.config))
    connection = sqlite3.connect(args.database, timeout=60)
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA journal_mode=WAL")
    completed.update(connection.execute(
        "SELECT DISTINCT person_name, image_path FROM face_encodings"
    ).fetchall())
    pending_encodings: list[tuple[str, bytes, str, float, str]] = []
    pending_paths: list[dict[str, Any]] = []
    processed = missing = errors = face_detections = accepted_faces = skipped = 0
    started = time.monotonic()
    initial_count = connection.execute("SELECT count(*) FROM face_encodings").fetchone()[0]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(f"{args.database}.before-resumable-{timestamp}")
    connection.close()
    _backup_database(args.database, backup)
    connection = sqlite3.connect(args.database, timeout=60)
    connection.execute("PRAGMA busy_timeout=60000")

    def flush() -> None:
        nonlocal pending_encodings, pending_paths
        _commit_encodings(connection, pending_encodings)
        _append_completed(journal, pending_paths)
        completed.update((item["identity"], item["path"]) for item in pending_paths)
        pending_encodings = []
        pending_paths = []

    _atomic_status(args.status, running=True, stage="append", processed=0, skipped=0,
                   total=len(rows), faces_before=initial_count, manifest=str(args.manifest),
                   manifest_sha256=manifest_hash, journal=str(journal), backup=str(backup),
                   updated_at=datetime.now(timezone.utc).isoformat())
    try:
        for identity, path in rows:
            key = (identity, str(path))
            if key in completed:
                skipped += 1
                continue
            if not path.is_file():
                missing += 1
                pending_paths.append({"manifest_sha256": manifest_hash, "identity": identity,
                                      "path": str(path), "face_count": 0, "outcome": "missing"})
            else:
                found, accepted, error = _process_image(rebuilder, cv2, path, identity, pending_encodings)
                face_detections += found
                accepted_faces += accepted
                processed += 1
                if error:
                    errors += 1
                pending_paths.append({"manifest_sha256": manifest_hash, "identity": identity,
                                      "path": str(path), "face_count": accepted,
                                      "outcome": "error" if error else "done", "error": error})
            if len(pending_paths) >= args.batch_size:
                flush()
                _atomic_status(args.status, running=True, stage="append", processed=processed,
                               skipped=skipped, missing=missing, total=len(rows),
                               errors=errors,
                               face_detections=face_detections, accepted_faces=accepted_faces,
                               current_identity=identity, current_path=str(path),
                               elapsed_seconds=round(time.monotonic() - started, 1),
                               faces_before=initial_count, manifest=str(args.manifest),
                               manifest_sha256=manifest_hash, journal=str(journal), backup=str(backup),
                               updated_at=datetime.now(timezone.utc).isoformat())
            if stop_requested:
                break
        flush()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        final_count = connection.execute("SELECT count(*) FROM face_encodings").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"database integrity check failed: {integrity}")
        complete = all((identity, str(path)) in completed for identity, path in rows)
        stage = "interrupted" if stop_requested else ("complete" if complete else "paused")
        _atomic_status(args.status, running=False, stage=stage, processed=processed, skipped=skipped,
                       missing=missing, errors=errors, total=len(rows), face_detections=face_detections,
                       accepted_faces=accepted_faces, faces_before=initial_count,
                       faces_after=final_count, database_integrity=integrity,
                       manifest=str(args.manifest), manifest_sha256=manifest_hash,
                       journal=str(journal), backup=str(backup),
                       updated_at=datetime.now(timezone.utc).isoformat())
        print(json.dumps({"stage": stage, "processed": processed, "skipped": skipped,
                          "missing": missing, "errors": errors, "face_detections": face_detections,
                          "accepted_faces": accepted_faces, "faces_before": initial_count,
                          "faces_after": final_count, "database_integrity": integrity,
                          "journal": str(journal), "backup": str(backup)}, sort_keys=True))
        return 0
    except BaseException as exc:
        connection.rollback()
        _atomic_status(args.status, running=False, stage="failed", error=str(exc),
                       manifest=str(args.manifest), manifest_sha256=manifest_hash,
                       journal=str(journal), backup=str(backup),
                       updated_at=datetime.now(timezone.utc).isoformat())
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
