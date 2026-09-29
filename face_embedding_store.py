#!/usr/bin/env python3
"""Durable, indexed storage for face-extraction cache records.

The legacy JSON cache remains an interchange format.  This SQLite store avoids
loading tens of thousands of Python dictionaries for every incremental match.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np


SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    path TEXT PRIMARY KEY,
    fingerprint TEXT,
    status TEXT,
    embedding BLOB,
    metadata_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_embeddings_fingerprint ON embeddings(fingerprint);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def list_paths(path: Path) -> set[str]:
    with connect(path) as conn:
        return {str(row[0]) for row in conn.execute("SELECT path FROM embeddings")}


def read_records(path: Path, paths: Iterable[str]) -> dict[str, dict[str, object]]:
    wanted = list(dict.fromkeys(str(item) for item in paths))
    if not wanted or not path.is_file():
        return {}
    records: dict[str, dict[str, object]] = {}
    with connect(path) as conn:
        for offset in range(0, len(wanted), 400):
            chunk = wanted[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT path, fingerprint, status, embedding, metadata_json FROM embeddings WHERE path IN ({placeholders})",
                chunk,
            )
            for raw_path, fingerprint, status, blob, metadata_json in rows:
                record: dict[str, object] = {"fingerprint": fingerprint}
                if status:
                    record["status"] = status
                if blob is not None:
                    vector = np.frombuffer(blob, dtype=np.float64)
                    if vector.shape == (128,):
                        record["embedding"] = vector.tolist()
                if metadata_json:
                    try:
                        metadata = json.loads(metadata_json)
                        if isinstance(metadata, dict):
                            record.update(metadata)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                records[str(raw_path)] = record
    return records


def upsert_records(path: Path, records: Iterable[tuple[str, dict[str, object]]]) -> int:
    """Persist newly decoded embeddings/statuses without rewriting the cache.

    Records are keyed by source path and carry the audit fingerprint, so a
    later audit automatically invalidates a stale result when a file changes.
    """
    rows = []
    for raw_path, record in records:
        if not isinstance(record, dict):
            continue
        embedding = record.get("embedding")
        blob = None
        if isinstance(embedding, (list, tuple)) and len(embedding) == 128:
            blob = np.asarray(embedding, dtype=np.float64).tobytes()
        metadata = {
            key: value for key, value in record.items()
            if key not in {"embedding", "fingerprint", "status"}
        }
        rows.append((
            str(raw_path),
            record.get("fingerprint"),
            record.get("status"),
            blob,
            json.dumps(metadata, sort_keys=True) if metadata else None,
        ))
    if not rows:
        return 0
    with connect(path) as conn:
        conn.executemany(
            "INSERT INTO embeddings(path, fingerprint, status, embedding, metadata_json) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status,"
            "embedding=excluded.embedding,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
            rows,
        )
    return len(rows)


def import_json(cache_path: Path, db_path: Path) -> dict[str, int | bool]:
    stat = cache_path.stat()
    source_key = f"{cache_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    with connect(db_path) as conn:
        previous = conn.execute("SELECT value FROM metadata WHERE key='json_source'").fetchone()
        if previous and previous[0] == source_key:
            count = int(conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
            return {"records": count, "imported": False}
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    if not isinstance(records, dict):
        raise ValueError("embedding cache records must be an object")
    rows = []
    for raw_path, record in records.items():
        if not isinstance(record, dict):
            continue
        embedding = record.get("embedding")
        blob = None
        if isinstance(embedding, list) and len(embedding) == 128:
            blob = np.asarray(embedding, dtype=np.float64).tobytes()
        metadata = {key: value for key, value in record.items() if key not in {"embedding", "fingerprint", "status"}}
        rows.append((str(raw_path), record.get("fingerprint"), record.get("status"), blob, json.dumps(metadata, sort_keys=True) if metadata else None))
    with connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO embeddings(path, fingerprint, status, embedding, metadata_json) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status,embedding=excluded.embedding,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
            rows,
        )
        conn.execute("INSERT INTO metadata(key,value) VALUES('json_source',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (source_key,))
        count = int(conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
    return {"records": count, "imported": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--import-json", type=Path)
    args = parser.parse_args()
    if not args.import_json:
        parser.error("--import-json is required")
    if not args.import_json.is_file():
        parser.error(f"cache file not found: {args.import_json}")
    print(json.dumps(import_json(args.import_json, args.db), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
