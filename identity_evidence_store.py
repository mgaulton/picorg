#!/usr/bin/env python3
"""Durable PicOrg identity, alias, marker, and assignment evidence store.

The store contains operational metadata only.  Source media and the shared
MetaDaily registry remain external and read-only; face embeddings continue to
live in the selected face-database backend.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import picorg_sorter as sorter

DEFAULT_DB = Path(".cache/picorg/identity_evidence.sqlite3")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS identities (
    canonical TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
    alias_key TEXT NOT NULL,
    identity TEXT NOT NULL REFERENCES identities(canonical) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(alias_key, identity, source)
);
CREATE INDEX IF NOT EXISTS idx_aliases_identity ON aliases(identity);
CREATE TABLE IF NOT EXISTS face_markers (
    marker_key TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    quality REAL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_face_markers_identity ON face_markers(identity);
CREATE TABLE IF NOT EXISTS assignments (
    path TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    sha256 TEXT PRIMARY KEY,
    current_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    size INTEGER,
    mtime_ns INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_media_path ON media(current_path);
CREATE TABLE IF NOT EXISTS face_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL REFERENCES media(sha256) ON DELETE CASCADE,
    face_index INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    quality REAL,
    status TEXT NOT NULL,
    embedding BLOB,
    geometry_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(sha256, face_index, model_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_face_observations_media ON face_observations(sha256);
CREATE TABLE IF NOT EXISTS matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES face_observations(observation_id) ON DELETE CASCADE,
    identity TEXT NOT NULL,
    score REAL,
    margin REAL,
    threshold REAL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_matches_observation ON matches(observation_id);
CREATE INDEX IF NOT EXISTS idx_matches_identity ON matches(identity);
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    identity TEXT,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    threshold REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL REFERENCES media(sha256) ON DELETE CASCADE,
    face_index INTEGER NOT NULL,
    similarity REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(cluster_id, sha256, face_index)
);
CREATE TABLE IF NOT EXISTS assignment_queue (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    expected_sha256 TEXT,
    identity TEXT NOT NULL,
    face_index INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    source TEXT NOT NULL,
    confidence REAL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    error TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_assignment_queue_status ON assignment_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_assignment_queue_path ON assignment_queue(path);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    total INTEGER,
    processed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS promotions (
    promotion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity TEXT NOT NULL,
    display_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_promotions_status ON promotions(status, updated_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.executescript(SCHEMA)
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _values(row: dict[str, Any]) -> Iterable[str]:
    for key in ("id", "primary_folder"):
        if row.get(key):
            yield str(row[key])
    for key in ("display_names", "search_terms"):
        for value in row.get(key) or []:
            if value:
                yield str(value)
    reddit = row.get("reddit")
    if isinstance(reddit, dict):
        for key in ("users", "subreddits"):
            for value in reddit.get(key) or []:
                if value:
                    yield str(value)


def _normalise_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return candidate


def upsert_media(
    db: Path,
    *,
    sha256: str,
    current_path: str,
    size: int | None = None,
    mtime_ns: int | None = None,
    status: str = "active",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Register one content-addressed media item without moving the source."""
    digest = _normalise_sha256(sha256)
    assert digest is not None
    now = utc_now()
    with open_store(db) as connection:
        connection.execute(
            """INSERT INTO media(sha256, current_path, status, size, mtime_ns, first_seen_at, last_seen_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sha256) DO UPDATE SET current_path=excluded.current_path,
               status=excluded.status, size=excluded.size, mtime_ns=excluded.mtime_ns,
               last_seen_at=excluded.last_seen_at, metadata_json=excluded.metadata_json""",
            (digest, str(current_path), status, size, mtime_ns, now, now, _json(metadata or {})),
        )


def record_face_observation(
    db: Path,
    *,
    sha256: str,
    current_path: str,
    face_index: int,
    model_id: str,
    model_version: str,
    quality: float | None,
    status: str,
    embedding: bytes | None = None,
    geometry: dict[str, Any] | None = None,
    size: int | None = None,
    mtime_ns: int | None = None,
) -> int:
    """Persist a model-versioned face observation keyed by media hash."""
    digest = _normalise_sha256(sha256)
    assert digest is not None
    now = utc_now()
    with open_store(db) as connection:
        connection.execute(
            """INSERT INTO media(sha256, current_path, size, mtime_ns, first_seen_at, last_seen_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, '{}')
               ON CONFLICT(sha256) DO UPDATE SET current_path=excluded.current_path,
               size=excluded.size, mtime_ns=excluded.mtime_ns, last_seen_at=excluded.last_seen_at""",
            (digest, str(current_path), size, mtime_ns, now, now),
        )
        connection.execute(
            """INSERT INTO face_observations(sha256, face_index, model_id, model_version, quality, status,
               embedding, geometry_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sha256, face_index, model_id, model_version) DO UPDATE SET quality=excluded.quality,
               status=excluded.status, embedding=excluded.embedding, geometry_json=excluded.geometry_json,
               updated_at=excluded.updated_at""",
            (digest, int(face_index), str(model_id), str(model_version), quality, str(status), embedding,
             _json(geometry or {}), now, now),
        )
        row = connection.execute(
            """SELECT observation_id FROM face_observations
               WHERE sha256=? AND face_index=? AND model_id=? AND model_version=?""",
            (digest, int(face_index), str(model_id), str(model_version)),
        ).fetchone()
        assert row is not None
        return int(row[0])


def record_canonical_baseline(
    db: Path,
    records: Iterable[dict[str, Any]],
    *,
    run_id: str,
    source: str = "canonical_baseline",
) -> int:
    """Persist a batch of read-only canonical face evidence in one transaction.

    Baseline records are content-addressed and idempotent.  Re-running a
    baseline updates the current path/quality but never deletes manual markers
    or assignments.  The helper intentionally stores only embeddings and
    provenance; source media and the MD/RD registries remain untouched.
    """
    now = utc_now()
    written = 0
    with open_store(db) as connection:
        for record in records:
            digest = _normalise_sha256(str(record.get("sha256") or "")) if record.get("sha256") else None
            identity = sorter.normalize_key(str(record.get("identity") or ""))
            path = str(record.get("path") or "")
            if not digest or not identity or not path:
                continue
            model_id = str(record.get("model_id") or "dlib-face-recognition-small-v1")
            model_version = str(record.get("model_version") or "picorg-baseline.v1")
            face_index = int(record.get("face_index") or 0)
            quality = record.get("quality")
            try:
                quality = float(quality) if quality is not None else None
            except (TypeError, ValueError):
                quality = None
            embedding = record.get("embedding")
            if isinstance(embedding, memoryview):
                embedding = embedding.tobytes()
            elif isinstance(embedding, bytearray):
                embedding = bytes(embedding)
            elif not isinstance(embedding, bytes):
                embedding = None
            provenance = dict(record.get("provenance") or {})
            provenance.setdefault("source", source)
            provenance.setdefault("run_id", run_id)
            connection.execute(
                """INSERT INTO media(sha256, current_path, status, size, mtime_ns,
                   first_seen_at, last_seen_at, metadata_json)
                   VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
                   ON CONFLICT(sha256) DO UPDATE SET current_path=excluded.current_path,
                   status='active', size=excluded.size, mtime_ns=excluded.mtime_ns,
                   last_seen_at=excluded.last_seen_at, metadata_json=excluded.metadata_json""",
                (digest, path, record.get("size"), record.get("mtime_ns"), now, now,
                 _json(provenance)),
            )
            connection.execute(
                """INSERT INTO face_observations(sha256, face_index, model_id, model_version,
                   quality, status, embedding, geometry_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'embedded', ?, ?, ?, ?)
                   ON CONFLICT(sha256, face_index, model_id, model_version) DO UPDATE SET
                   quality=excluded.quality, status='embedded', embedding=excluded.embedding,
                   geometry_json=excluded.geometry_json, updated_at=excluded.updated_at""",
                (digest, face_index, model_id, model_version, quality, embedding,
                 _json(record.get("geometry") or {}), now, now),
            )
            marker_key = str(record.get("marker_key") or f"baseline:{identity}:{digest}:{face_index}:{model_id}:{model_version}")
            connection.execute(
                """INSERT INTO face_markers(marker_key, identity, path, sha256, status, source,
                   quality, provenance_json, updated_at) VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)
                   ON CONFLICT(marker_key) DO UPDATE SET identity=excluded.identity,
                   path=excluded.path, sha256=excluded.sha256, status='confirmed',
                   source=excluded.source, quality=excluded.quality,
                   provenance_json=excluded.provenance_json, updated_at=excluded.updated_at""",
                (marker_key, identity, path, digest, source, quality, _json(provenance), now),
            )
            written += 1
    return written


def record_match(
    db: Path,
    *,
    observation_id: int,
    identity: str,
    score: float | None,
    margin: float | None,
    threshold: float | None,
    model_id: str,
    model_version: str,
    decision: str,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Record every candidate decision; later decisions never erase history."""
    canonical = sorter.normalize_key(identity)
    if not canonical:
        raise ValueError("identity is required")
    now = utc_now()
    with open_store(db) as connection:
        cursor = connection.execute(
            """INSERT INTO matches(observation_id, identity, score, margin, threshold, model_id, model_version,
               decision, run_id, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(observation_id), canonical, score, margin, threshold, str(model_id), str(model_version),
             str(decision), run_id, now, _json(metadata or {})),
        )
        return int(cursor.lastrowid)


def record_match_results(
    db: Path,
    results: Iterable[dict[str, Any]],
    *,
    run_id: str,
    model_id: str = "dlib",
    model_version: str = "face_group_unmatched.v1",
) -> int:
    """Persist one observation and the top identity candidate per match result.

    The batch writer keeps daily refreshes to one SQLite transaction while
    retaining the raw distance and candidate list in metadata for later
    threshold recalibration. Results without a usable source hash are skipped.
    """
    now = utc_now()
    written = 0
    with open_store(db) as connection:
        for result in results:
            digest = _normalise_sha256(str(result.get("sha256") or "")) if result.get("sha256") else None
            path = str(result.get("path") or "")
            if not digest or not path:
                continue
            face_index = int(result.get("matched_face_index") or 0)
            qualities = result.get("quality")
            quality = None
            if isinstance(qualities, list) and qualities:
                try:
                    quality = float(qualities[min(face_index, len(qualities) - 1)])
                except (TypeError, ValueError):
                    quality = None
            connection.execute(
                """INSERT INTO media(sha256, current_path, status, first_seen_at, last_seen_at, metadata_json)
                   VALUES (?, ?, 'active', ?, ?, '{}')
                   ON CONFLICT(sha256) DO UPDATE SET current_path=excluded.current_path,
                   status=excluded.status, last_seen_at=excluded.last_seen_at""",
                (digest, path, now, now),
            )
            observation_quality = quality
            geometry = {}
            if isinstance(qualities, list) and qualities and 0 <= face_index < len(qualities) and isinstance(qualities[face_index], dict):
                geometry = qualities[face_index]
            effective_model_id = str(result.get("model_id") or model_id)
            effective_model_version = str(result.get("model_version") or model_version)
            effective_threshold = result.get("threshold")
            connection.execute(
                """INSERT INTO face_observations(sha256, face_index, model_id, model_version, quality, status,
                   embedding, geometry_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                   ON CONFLICT(sha256, face_index, model_id, model_version) DO UPDATE SET quality=excluded.quality,
                   status=excluded.status, geometry_json=excluded.geometry_json, updated_at=excluded.updated_at""",
                (digest, face_index, effective_model_id, effective_model_version, observation_quality, str(result.get("status") or "unknown"), _json(geometry), now, now),
            )
            observation = connection.execute(
                "SELECT observation_id FROM face_observations WHERE sha256=? AND face_index=? AND model_id=? AND model_version=?",
                (digest, face_index, effective_model_id, effective_model_version),
            ).fetchone()
            if observation is None:
                continue
            candidates = result.get("candidates")
            top = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else None
            if top and top.get("person"):
                try:
                    distance = float(top.get("distance"))
                except (TypeError, ValueError):
                    distance = None
                margin = None
                if isinstance(candidates, list) and len(candidates) > 1:
                    try:
                        margin = float(candidates[1].get("distance")) - float(top.get("distance"))
                    except (TypeError, ValueError, AttributeError):
                        margin = None
                connection.execute(
                    """INSERT INTO matches(observation_id, identity, score, margin, threshold, model_id, model_version,
                       decision, run_id, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (int(observation[0]), sorter.normalize_key(str(top["person"])),
                     (1.0 - distance) if distance is not None else None, margin, effective_threshold, effective_model_id, effective_model_version,
                     str(result.get("status") or "unknown"), run_id, now,
                     _json({"distance": distance, "candidates": candidates[:5] if isinstance(candidates, list) else []})),
                )
            written += 1
    return written


def upsert_cluster(
    db: Path,
    *,
    cluster_id: str,
    display_name: str,
    status: str,
    model_id: str,
    model_version: str,
    threshold: float | None = None,
    identity: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Create/update a stable face-only cluster while retaining its machine ID."""
    now = utc_now()
    canonical = sorter.normalize_key(identity) if identity else None
    with open_store(db) as connection:
        connection.execute(
            """INSERT INTO clusters(cluster_id, display_name, status, identity, model_id, model_version,
               threshold, created_at, updated_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cluster_id) DO UPDATE SET display_name=excluded.display_name,
               status=excluded.status, identity=excluded.identity, model_id=excluded.model_id,
               model_version=excluded.model_version, threshold=excluded.threshold,
               updated_at=excluded.updated_at, metadata_json=excluded.metadata_json""",
            (str(cluster_id), str(display_name), str(status), canonical, str(model_id), str(model_version),
             threshold, now, now, _json(metadata or {})),
        )


def queue_assignment(
    db: Path,
    *,
    path: str,
    identity: str,
    expected_sha256: str | None = None,
    face_index: int | None = None,
    confidence: float | None = None,
    source: str = "review_ui",
    created_by: str = "operator",
    provenance: dict[str, Any] | None = None,
) -> int:
    """Queue a review assignment; it is not a move and is safe during matching."""
    canonical = sorter.normalize_key(identity)
    if not canonical or not str(path).strip():
        raise ValueError("path and identity are required")
    expected = _normalise_sha256(expected_sha256) if expected_sha256 else None
    now = utc_now()
    with open_store(db) as connection:
        existing = connection.execute(
            """SELECT assignment_id FROM assignment_queue
               WHERE path=? AND identity=? AND status='pending'
               ORDER BY assignment_id DESC LIMIT 1""",
            (str(path), canonical),
        ).fetchone()
        if existing:
            return int(existing[0])
        cursor = connection.execute(
            """INSERT INTO assignment_queue(path, expected_sha256, identity, face_index, status, source,
               confidence, created_by, created_at, provenance_json) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (str(path), expected, canonical, face_index, str(source), confidence, str(created_by), now,
             _json(provenance or {})),
        )
        return int(cursor.lastrowid)


def list_assignment_queue(db: Path, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Return queued assignments for the UI without exposing raw embedding data."""
    wanted = [str(value) for value in (statuses or []) if str(value)]
    with open_store(db) as connection:
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            rows = connection.execute(
                f"SELECT assignment_id, path, expected_sha256, identity, face_index, status, source, confidence, created_by, created_at, applied_at, error, provenance_json FROM assignment_queue WHERE status IN ({placeholders}) ORDER BY assignment_id",
                wanted,
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT assignment_id, path, expected_sha256, identity, face_index, status, source, confidence, created_by, created_at, applied_at, error, provenance_json FROM assignment_queue ORDER BY assignment_id"
            ).fetchall()
    keys = ("assignment_id", "path", "expected_sha256", "identity", "face_index", "status", "source", "confidence", "created_by", "created_at", "applied_at", "error", "provenance_json")
    result = []
    for row in rows:
        item = dict(zip(keys, row))
        try:
            item["provenance"] = json.loads(item.pop("provenance_json") or "{}")
        except json.JSONDecodeError:
            item["provenance"] = {}
            item.pop("provenance_json", None)
        result.append(item)
    return result


def update_assignment_status(
    db: Path,
    assignment_id: int,
    status: str,
    error: str | None = None,
    *,
    applied_move: dict[str, Any] | None = None,
) -> None:
    """Advance a queued assignment without allowing silent deletion."""
    allowed = {"pending", "applying", "applied", "rejected", "conflict", "error", "undone"}
    if status not in allowed:
        raise ValueError(f"unsupported assignment status: {status}")
    now = utc_now()
    with open_store(db) as connection:
        row = connection.execute(
            "SELECT applied_at, provenance_json FROM assignment_queue WHERE assignment_id=?",
            (int(assignment_id),),
        ).fetchone()
        if row is None:
            return
        applied_at = now if status == "applied" else (row[0] if status == "undone" else None)
        try:
            provenance = json.loads(row[1] or "{}")
        except json.JSONDecodeError:
            provenance = {}
        if not isinstance(provenance, dict):
            provenance = {}
        if applied_move is not None:
            provenance["applied_move"] = applied_move
        if status == "undone":
            provenance["undone_at"] = now
        connection.execute(
            "UPDATE assignment_queue SET status=?, applied_at=?, error=?, provenance_json=? WHERE assignment_id=?",
            (status, applied_at, error, _json(provenance), int(assignment_id)),
        )


def record_pipeline_run(
    db: Path,
    *,
    run_id: str,
    job: str,
    status: str,
    stage: str,
    total: int | None = None,
    processed: int = 0,
    error: str | None = None,
    finished: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist resumable daily-cycle progress and terminal outcome."""
    now = utc_now()
    with open_store(db) as connection:
        existing = connection.execute("SELECT started_at FROM pipeline_runs WHERE run_id=?", (str(run_id),)).fetchone()
        started = str(existing[0]) if existing else now
        connection.execute(
            """INSERT INTO pipeline_runs(run_id, job, status, stage, total, processed, started_at, updated_at,
               finished_at, error, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, stage=excluded.stage,
               total=excluded.total, processed=excluded.processed, updated_at=excluded.updated_at,
               finished_at=excluded.finished_at, error=excluded.error, metadata_json=excluded.metadata_json""",
            (str(run_id), str(job), str(status), str(stage), total, int(processed), started, now,
             now if finished else None, error, _json(metadata or {})),
        )


def request_promotion(
    db: Path,
    *,
    identity: str,
    display_name: str,
    aliases: Iterable[str] = (),
    evidence_count: int,
    source: str = "picorg",
    minimum_exemplars: int = 5,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Queue local identity promotion; direct registry writes remain adapter-gated."""
    canonical = sorter.normalize_key(identity)
    if not canonical:
        raise ValueError("identity is required")
    if evidence_count < 0 or minimum_exemplars < 1:
        raise ValueError("evidence counts must be non-negative and minimum must be positive")
    status = "eligible" if evidence_count >= minimum_exemplars else "pending"
    now = utc_now()
    with open_store(db) as connection:
        cursor = connection.execute(
            """INSERT INTO promotions(identity, display_name, aliases_json, status, evidence_count, source,
               created_at, updated_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (canonical, str(display_name), _json(sorted({str(alias) for alias in aliases if str(alias)})), status,
             int(evidence_count), str(source), now, now, _json(metadata or {})),
        )
        return int(cursor.lastrowid)


def _upsert_identity(
    connection: sqlite3.Connection,
    canonical: str,
    *,
    display_name: str,
    status: str,
    trust_level: str,
    source: str,
    aliases: Iterable[str],
    now: str,
) -> None:
    existing = connection.execute(
        "SELECT sources_json FROM identities WHERE canonical = ?", (canonical,)
    ).fetchone()
    sources = set(json.loads(existing[0])) if existing else set()
    sources.add(source)
    connection.execute(
        """INSERT INTO identities(canonical, display_name, status, trust_level, sources_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(canonical) DO UPDATE SET display_name=excluded.display_name,
           status=excluded.status, trust_level=excluded.trust_level,
           sources_json=excluded.sources_json, updated_at=excluded.updated_at""",
        (canonical, display_name, status, trust_level, _json(sorted(sources)), now),
    )
    for alias in set(aliases) | {canonical}:
        alias_key = sorter.normalize_key(alias)
        if alias_key:
            connection.execute(
                "INSERT OR REPLACE INTO aliases(alias_key, identity, alias, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                (alias_key, canonical, alias, source, now),
            )


def import_md_registry(connection: sqlite3.Connection, path: Path, now: str) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    for row in payload.get("identities", []):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        canonical = sorter.normalize_key(str(row["id"]))
        if not canonical:
            continue
        status = str(row.get("status") or "pending")
        trust = "registry_confirmed" if status == "confirmed" else "registry_unverified"
        _upsert_identity(
            connection,
            canonical,
            display_name=str(row.get("primary_folder") or row["id"]),
            status=status,
            trust_level=trust,
            source="metadaily_registry",
            aliases=_values(row),
            now=now,
        )
        count += 1
    connection.execute(
        "INSERT OR REPLACE INTO source_state(source, path, imported_at) VALUES (?, ?, ?)",
        ("metadaily_registry", str(path), now),
    )
    return count


def import_markers(connection: sqlite3.Connection, path: Path, now: str) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    for row in payload.get("markers", []):
        if not isinstance(row, dict) or not row.get("identity"):
            continue
        identity = sorter.normalize_key(str(row["identity"]))
        if not identity:
            continue
        key = str(row.get("key") or f"path:{row.get('path')}")
        status = str(row.get("status") or "pending")
        if status == "confirmed":
            _upsert_identity(
                connection,
                identity,
                display_name=str(row["identity"]),
                status="confirmed",
                trust_level="face_confirmed",
                source="picorg_face_marker",
                aliases=[str(row["identity"])],
                now=now,
            )
        connection.execute(
            """INSERT OR REPLACE INTO face_markers(marker_key, identity, path, sha256, status, source, quality, provenance_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, identity, str(row.get("path") or ""), row.get("sha256"), status,
             str(row.get("source") or "picorg"), row.get("quality"), _json(row), now),
        )
        count += 1
    connection.execute(
        "INSERT OR REPLACE INTO source_state(source, path, imported_at) VALUES (?, ?, ?)",
        ("picorg_face_markers", str(path), now),
    )
    return count


def import_assignments(connection: sqlite3.Connection, path: Path, now: str) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    # This JSON ledger is the interchange source for PicOrg review decisions;
    # replace its imported projection so renamed/rejected decisions do not
    # leave stale assignments behind in SQLite.
    connection.execute("DELETE FROM assignments WHERE source = 'picorg_review'")
    for row in payload.get("decisions", []):
        if not isinstance(row, dict) or not row.get("identity") or not row.get("path"):
            continue
        # The durable assignment table is identity evidence, not a copy of
        # every tentative UI state.  Keep only human-confirmed decisions and
        # point moved media at its current organized path when available.
        if str(row.get("status") or "confirmed") != "confirmed":
            continue
        assignment_path = str(row.get("canonical_path") or row["path"])
        connection.execute(
            """INSERT OR REPLACE INTO assignments(path, identity, status, source, confidence, provenance_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (assignment_path, sorter.normalize_key(str(row["identity"])), "assigned",
             "picorg_review", row.get("confidence"), _json(row), now),
        )
        count += 1
    connection.execute(
        "INSERT OR REPLACE INTO source_state(source, path, imported_at) VALUES (?, ?, ?)",
        ("picorg_review_assignments", str(path), now),
    )
    return count


def sync_store(
    db: Path,
    *,
    md_registry: Path | None = None,
    markers: Path | None = None,
    assignments: Path | None = None,
) -> dict[str, int | str]:
    now = utc_now()
    connection = open_store(db)
    counts = {"identities": 0, "markers": 0, "assignments": 0}
    try:
        with connection:
            if md_registry and md_registry.is_file():
                counts["identities"] = import_md_registry(connection, md_registry, now)
            if markers and markers.is_file():
                counts["markers"] = import_markers(connection, markers, now)
            if assignments and assignments.is_file():
                counts["assignments"] = import_assignments(connection, assignments, now)
    finally:
        connection.close()
    return {**counts, "db": str(db)}


def confirmed_keys(db: Path) -> tuple[set[str], set[str]]:
    connection = open_store(db)
    try:
        rows = connection.execute(
            "SELECT canonical FROM identities WHERE status = 'confirmed'"
        ).fetchall()
        canonical = {str(row[0]) for row in rows}
        aliases = {
            str(row[0]) for row in connection.execute(
                "SELECT alias_key FROM aliases WHERE identity IN (SELECT canonical FROM identities WHERE status = 'confirmed')"
            ).fetchall()
        }
        return aliases, canonical
    finally:
        connection.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--md-registry", type=Path)
    parser.add_argument("--markers", type=Path)
    parser.add_argument("--assignments", type=Path)
    args = parser.parse_args()
    print(json.dumps(sync_store(args.db, md_registry=args.md_registry, markers=args.markers, assignments=args.assignments), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
