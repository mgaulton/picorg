#!/usr/bin/env python3
"""Relink PicOrg records and derived review snapshots after identity consolidation."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_ui import _image_decision_key


ROOT = Path("/opt/picorg")
AUDITS = ROOT / ".cache/picorg/audits"
EVIDENCE_DB = ROOT / ".cache/picorg/identity_evidence.sqlite3"
FACE_DB = Path("/opt/photo_reorg/data/high_accuracy_faces.db")


def atomic_json(path: Path, data: Any) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def backup_file(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.before-identity-consolidation")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def remap_json(value: Any, path_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: remap_json(item, path_map) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_json(item, path_map) for item in value]
    if isinstance(value, str):
        return path_map.get(value, value)
    return value


def identity_indexes(items: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    aliases: dict[str, set[str]] = {}
    source_by_path: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("status") == "unresolved_identity" or item["source"] == item["target"]:
            continue
        source_by_path[item["source"]] = item
        if item.get("identity_family") != "linked":
            continue
        terms = {str(item.get("identity_id") or "").casefold()}
        terms.add(Path(item["source"]).parent.name.casefold())
        for values in (item.get("source_aliases") or {}).values():
            if isinstance(values, list):
                terms.update(str(term).casefold() for term in values if term)
            elif isinstance(values, str):
                terms.add(values.casefold())
        for term in terms:
            if term:
                aliases.setdefault(term, set()).add(str(item["identity_id"]))
    return aliases, source_by_path


def canonical_identity(value: Any, aliases: dict[str, set[str]]) -> Any:
    if not isinstance(value, str):
        return value
    targets = aliases.get(value.casefold(), set())
    return next(iter(targets)) if len(targets) == 1 else value


def update_review_json(path: Path, field: str, path_map: dict[str, str],
                       aliases: dict[str, set[str]], preserve_path: bool) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get(field, [])
    changed = 0
    for record in records:
        old = record.get("canonical_path") or record.get("path")
        if old not in path_map:
            continue
        new = path_map[old]
        record["key"] = _image_decision_key(new)
        if preserve_path:
            record["canonical_path"] = new
        else:
            record["path"] = new
        record["identity"] = canonical_identity(record.get("identity"), aliases)
        changed += 1
    backup_file(path)
    payload["updated"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, payload)
    return changed


def update_sqlite_paths(database: Path, path_map: dict[str, str], aliases: dict[str, set[str]],
                        *, face_database: bool = False,
                        coalesced_prefix: str | None = None) -> dict[str, int]:
    backup = database.with_name(f"{database.name}.before-picorg-identity-consolidation")
    if not backup.exists():
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    connection = sqlite3.connect(database)
    counts: dict[str, int] = {}
    try:
        connection.execute("CREATE TEMP TABLE picorg_path_map (source TEXT PRIMARY KEY, target TEXT NOT NULL)")
        connection.executemany("INSERT INTO picorg_path_map VALUES (?,?)", path_map.items())
        connection.execute("CREATE TEMP TABLE picorg_key_map (source_key TEXT PRIMARY KEY, target_key TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO picorg_key_map VALUES (?,?)",
            ((f"image:{hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]}", _image_decision_key(target))
             for source, target in path_map.items()),
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                if table == "face_markers" and column == "marker_key":
                    cursor = connection.execute(
                        "UPDATE face_markers SET marker_key=(SELECT target_key FROM picorg_key_map WHERE source_key=marker_key) WHERE marker_key IN (SELECT source_key FROM picorg_key_map)"
                    )
                    if cursor.rowcount:
                        counts["face_markers.marker_key"] = cursor.rowcount
                elif column in {"path", "image_path", "current_path", "canonical_path", "source_path"}:
                    query = f'UPDATE "{table}" SET "{column}"=(SELECT target FROM picorg_path_map WHERE source="{column}") WHERE "{column}" IN (SELECT source FROM picorg_path_map)'
                    cursor = connection.execute(query)
                    if cursor.rowcount:
                        counts[f"{table}.{column}"] = cursor.rowcount
                elif column.endswith("_json"):
                    rows = connection.execute(f'SELECT rowid,"{column}" FROM "{table}" WHERE "{column}" IS NOT NULL').fetchall()
                    for rowid, raw in rows:
                        try:
                            parsed = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        remapped = remap_json(parsed, path_map)
                        encoded = json.dumps(remapped, ensure_ascii=False, separators=(",", ":"))
                        if encoded != raw:
                            connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (encoded, rowid))
                            counts[f"{table}.{column}"] = counts.get(f"{table}.{column}", 0) + 1
            path_column = next((name for name in ("path", "image_path", "current_path") if name in columns), None)
            if table in {"face_markers", "assignments", "assignment_queue"} and "identity" in columns and path_column:
                for (name,) in connection.execute(f'SELECT DISTINCT identity FROM "{table}"').fetchall():
                    canonical = canonical_identity(name, aliases)
                    if canonical != name:
                        connection.execute(
                            f'UPDATE "{table}" SET identity=? WHERE identity=? AND "{path_column}" IN (SELECT target FROM picorg_path_map)',
                            (canonical, name),
                        )
                        counts[f"{table}.identity"] = counts.get(f"{table}.identity", 0) + connection.execute("SELECT changes()").fetchone()[0]
        if face_database:
            rows = connection.execute("SELECT id,person_name,image_path FROM face_encodings").fetchall()
            cache_prefix = coalesced_prefix or str(ROOT / ".cache/picorg/face-references-coalesced-")
            path_changes = identity_changes = 0
            for rowid, name, image_path in rows:
                target_path = image_path
                if image_path.startswith(cache_prefix) and os.path.islink(image_path):
                    source_path = os.readlink(image_path)
                    # Sorted media was moved to its canonical folder. MetaDaily
                    # and RedditDaily remain at their protected source paths.
                    target_path = path_map.get(source_path, source_path)
                if target_path != image_path:
                    connection.execute("UPDATE face_encodings SET image_path=? WHERE id=?",
                                       (target_path, rowid))
                    path_changes += 1
                identity = canonical_identity(name, aliases)
                if identity != name:
                    connection.execute("UPDATE face_encodings SET person_name=? WHERE id=?",
                                       (identity, rowid))
                    identity_changes += 1
            if path_changes:
                counts["face_encodings.image_path"] = path_changes
            if identity_changes:
                counts["face_encodings.person_name"] = identity_changes
                connection.execute(
                    "DELETE FROM person_metadata WHERE person_name NOT IN "
                    "(SELECT DISTINCT person_name FROM face_encodings)"
                )
                people = connection.execute(
                    "SELECT person_name,COUNT(*),AVG(face_quality) "
                    "FROM face_encodings GROUP BY person_name"
                ).fetchall()
                for person, total, avg_quality in people:
                    best = connection.execute(
                        "SELECT encoding FROM face_encodings WHERE person_name=? ORDER BY id DESC LIMIT 1",
                        (person,),
                    ).fetchone()[0]
                    connection.execute("""
                        INSERT INTO person_metadata
                            (person_name,total_faces,avg_quality,best_encoding,updated_at)
                        VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                        ON CONFLICT(person_name) DO UPDATE SET
                            total_faces=excluded.total_faces,
                            avg_quality=excluded.avg_quality,
                            best_encoding=excluded.best_encoding,
                            updated_at=CURRENT_TIMESTAMP
                    """, (person, total, avg_quality, best))
                counts["person_metadata.rebuilt"] = len(people)
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {database}")
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts


def main() -> None:
    manifest_path = ROOT / ".cache/picorg/identity-consolidation-apply.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = [item for item in manifest["items"] if item.get("status") != "unresolved_identity" and item["source"] != item["target"]]
    path_map = {item["source"]: item["target"] for item in rows}
    aliases, source_rows = identity_indexes(manifest["items"])
    if len(path_map) != len(rows):
        raise RuntimeError("manifest has duplicate source paths")
    summary: dict[str, Any] = {"path_map": len(path_map)}

    markers = ROOT / "identity_face_markers.json"
    decisions = ROOT / "review_image_decisions.json"
    summary["face_marker_records"] = update_review_json(markers, "markers", path_map, aliases, False)
    summary["image_decision_records"] = update_review_json(decisions, "decisions", path_map, aliases, True)

    history = ROOT / "review_move_history.json"
    payload = json.loads(history.read_text(encoding="utf-8"))
    operation_id = "identity-consolidation-20260929"
    if not any(operation.get("id") == operation_id for operation in payload.get("operations", [])):
        backup_file(history)
        payload.setdefault("operations", []).append({
            "id": operation_id,
            "identity": "shared-linked-identities",
            "family": "linked_consolidation",
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "moves": [{"source": item["source"], "destination": item["target"], "identity": item["identity_id"], "family": item.get("identity_family", "review"), "undone": False} for item in rows],
            "undone": False,
            "undo_errors": [],
        })
        payload["updated"] = datetime.now(timezone.utc).isoformat()
        atomic_json(history, payload)
    summary["review_history_moves"] = len(rows)

    summary["evidence_db"] = update_sqlite_paths(EVIDENCE_DB, path_map, aliases)
    summary["face_reference_db"] = update_sqlite_paths(FACE_DB, path_map, aliases, face_database=True)

    audit_names = (
        "20260916T114034Z.json",
        "20260916T114034Z.face-clusters.json",
        "20260916T114034Z.preflight.json",
        "20260916T114034Z.reconciled.json",
        "20260916T114034Z.reconciled.preflight.json",
        "20260916T114034Z.reconciled.clusters.json",
    )
    audit_counts = {}
    for name in audit_names:
        source = AUDITS / name
        target = AUDITS / name.replace("20260916T114034Z", "20260916T114034Z.reorg")
        data = json.loads(source.read_text(encoding="utf-8"))
        rewritten = remap_json(data, path_map)
        atomic_json(target, rewritten)
        audit_counts[target.name] = len(path_map)
    summary["derived_audits"] = audit_counts

    output = ROOT / ".cache/picorg/identity-consolidation-records.json"
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
