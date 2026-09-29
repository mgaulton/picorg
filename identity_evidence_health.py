#!/usr/bin/env python3
"""Check and back up PicOrg's durable identity evidence database.

The check is deliberately read-only except for the optional SQLite online
backup.  It never walks media roots, changes assignments, or rewrites the
primary database.  WAL state is read through SQLite's connection/backup API so
the copy is consistent while the review UI is serving reads.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(".cache/picorg/identity_evidence.sqlite3")
DEFAULT_OUTPUT = Path(".cache/picorg/evidence-health.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for item in str(value).split(".")[:3]:
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]


def version_is_wal_fixed(value: str) -> bool:
    """Return whether the documented SQLite WAL-reset fixes are present."""
    version = _version_tuple(value)
    return (
        version >= (3, 51, 3)
        or (3, 50, 7) <= version < (3, 51, 0)
        or (3, 44, 6) <= version < (3, 45, 0)
    )


def _pragma_check(connection: sqlite3.Connection, pragma: str) -> tuple[bool, list[str]]:
    rows = [str(row[0]) for row in connection.execute(pragma).fetchall()]
    return rows == ["ok"], rows[:20]


def backup_database(source: Path, backup_dir: Path) -> Path:
    """Create a consistent online backup and atomically publish it."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{source.stem}-{stamp}.sqlite3"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    source_connection = sqlite3.connect(source, timeout=30)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    temporary.replace(target)
    return target


def check_database(
    path: Path,
    *,
    backup_dir: Path | None = None,
    full_integrity: bool = False,
) -> dict[str, Any]:
    """Return a bounded health report; no primary data is modified."""
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": _now(),
        "database": str(path),
        "runtime_sqlite": sqlite3.sqlite_version,
        "wal_fixed_version": version_is_wal_fixed(sqlite3.sqlite_version),
        "exists": path.is_file(),
        "backup": None,
        "checks": {},
        "healthy": False,
    }
    if not path.is_file():
        report["error"] = "database_missing"
        return report
    try:
        connection = sqlite3.connect(path, timeout=10)
        try:
            journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            report["journal_mode"] = journal
            report["wal_files"] = {
                "wal": path.with_name(path.name + "-wal").is_file(),
                "shm": path.with_name(path.name + "-shm").is_file(),
            }
            quick_ok, quick_rows = _pragma_check(connection, "PRAGMA quick_check(1)")
            report["checks"]["quick_check"] = {"ok": quick_ok, "result": quick_rows}
            if full_integrity:
                integrity_ok, integrity_rows = _pragma_check(connection, "PRAGMA integrity_check")
                report["checks"]["integrity_check"] = {"ok": integrity_ok, "result": integrity_rows}
            report["checks"]["required_tables"] = {
                "ok": all(
                    connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    for table in ("media", "face_observations", "face_markers", "assignment_queue", "pipeline_runs")
                )
            }
        finally:
            connection.close()
        if backup_dir is not None:
            report["backup"] = str(backup_database(path, backup_dir))
    except (OSError, sqlite3.Error) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    report["healthy"] = bool(
        report["checks"].get("quick_check", {}).get("ok")
        and report["checks"].get("required_tables", {}).get("ok")
        and (not full_integrity or report["checks"].get("integrity_check", {}).get("ok"))
    )
    report["warnings"] = [] if report["wal_fixed_version"] else ["runtime SQLite predates the documented WAL-reset fix"]
    return report


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("PICORG_EVIDENCE_DB", DEFAULT_DB)))
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("PICORG_EVIDENCE_HEALTH", DEFAULT_OUTPUT)))
    parser.add_argument("--backup-dir", type=Path, help="also create a consistent online backup here")
    parser.add_argument("--full", action="store_true", help="run the more expensive full integrity_check")
    parser.add_argument("--strict-version", action="store_true", help="fail when SQLite lacks the documented WAL fix")
    args = parser.parse_args()
    report = check_database(args.db, backup_dir=args.backup_dir, full_integrity=args.full)
    _atomic_write(args.output, report)
    print(json.dumps(report, sort_keys=True), flush=True)
    if not report["healthy"] or (args.strict_version and not report["wal_fixed_version"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
