#!/usr/bin/env python3
"""Append faces from a path delta without clearing the existing photo_reorg DB."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
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
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def _restore_database(database: Path, backup: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{database.name}.restore-", dir=database.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--status", type=Path, default=Path("/tmp/picorg-incremental-refresh-status.json"))
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = list(_source_rows(manifest))
    if not args.database.is_file():
        raise SystemExit(f"database does not exist: {args.database}")
    if not args.config.is_file():
        raise SystemExit(f"photo_reorg config does not exist: {args.config}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(f"{args.database}.before-incremental-{timestamp}")
    _backup_database(args.database, backup)
    initial_count = sqlite3.connect(args.database).execute("SELECT count(*) FROM face_encodings").fetchone()[0]
    _atomic_status(args.status, running=True, processed=0, total=len(rows), faces_before=initial_count,
                   backup=str(backup), manifest=str(args.manifest), updated_at=datetime.now(timezone.utc).isoformat())

    def stop(signum, _frame):
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        sys.path.insert(0, str(DEFAULT_PHOTO_REORG))
        from rebuild_face_database import FaceDatabaseRebuilder

        logging.getLogger().setLevel(logging.WARNING)
        rebuilder = FaceDatabaseRebuilder(str(args.config))
        processed = missing = face_detections = 0
        started = time.monotonic()
        for identity, path in rows:
            if not path.is_file():
                missing += 1
                continue
            count = rebuilder._process_image_for_person(str(path), identity, dry_run=False)
            face_detections += count
            processed += 1
            if processed % 50 == 0 or processed == len(rows):
                _atomic_status(args.status, running=True, processed=processed, total=len(rows),
                               missing=missing, face_detections=face_detections, current_identity=identity,
                               elapsed_seconds=round(time.monotonic() - started, 1), faces_before=initial_count,
                               backup=str(backup), manifest=str(args.manifest),
                               updated_at=datetime.now(timezone.utc).isoformat())
        connection = sqlite3.connect(args.database)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            final_count = connection.execute("SELECT count(*) FROM face_encodings").fetchone()[0]
        finally:
            connection.close()
        if result != "ok":
            raise RuntimeError(f"database integrity check failed: {result}")
        _atomic_status(args.status, running=False, stage="complete", processed=processed, total=len(rows),
                       missing=missing, face_detections=face_detections, faces_before=initial_count,
                       faces_after=final_count, database_integrity=result, backup=str(backup),
                       manifest=str(args.manifest), finished_at=datetime.now(timezone.utc).isoformat())
        print(json.dumps({"processed": processed, "missing": missing, "face_detections": face_detections,
                          "faces_before": initial_count, "faces_after": final_count,
                          "backup": str(backup), "status": str(args.status)}, sort_keys=True))
        return 0
    except BaseException as exc:
        _restore_database(args.database, backup)
        _atomic_status(args.status, running=False, stage="failed", error=str(exc), backup=str(backup),
                       manifest=str(args.manifest), updated_at=datetime.now(timezone.utc).isoformat())
        raise


if __name__ == "__main__":
    raise SystemExit(main())
