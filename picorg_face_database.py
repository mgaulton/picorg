#!/usr/bin/env python3
"""PicOrg-owned face-reference database builder.

The database schema intentionally remains compatible with the existing
photo_reorg readers while the extraction, validation, and write path are owned
by PicOrg. Source reference trees are read-only; the output database is built
in a temporary sibling and atomically promoted only after validation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from itertools import chain
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import face_cluster_unmatched as extractor
from coalesce_face_references import THUMBNAIL_PATTERN


DEFAULT_DB = Path("/opt/picorg/.cache/picorg/face_database.sqlite3")
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tif", ".tiff"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS face_encodings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_name TEXT NOT NULL,
    encoding BLOB NOT NULL,
    image_path TEXT NOT NULL,
    face_quality REAL NOT NULL,
    model_used TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS person_metadata (
    person_name TEXT PRIMARY KEY,
    total_faces INTEGER DEFAULT 0,
    avg_quality REAL DEFAULT 0.0,
    best_encoding BLOB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS picorg_face_database_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_face_encodings_person ON face_encodings(person_name);
"""


def iter_reference_images(root: Path) -> Iterable[tuple[str, Path]]:
    """Yield canonical folder names and media below a flat coalesced root."""
    try:
        identity_dirs = sorted(item for item in root.iterdir() if item.is_dir() and not item.name.startswith("."))
    except OSError as exc:
        raise RuntimeError(f"reference root is unreadable: {root}: {exc}") from exc
    for identity_dir in identity_dirs:
        try:
            def raise_walk_error(exc: OSError) -> None:
                raise exc

            for current, directories, files in os.walk(
                identity_dir,
                topdown=True,
                onerror=raise_walk_error,
                followlinks=False,
            ):
                directories[:] = [name for name in directories if not name.startswith(".")]
                for name in sorted(files):
                    path = Path(current) / name
                    if name.startswith(".") or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    if THUMBNAIL_PATTERN.search(path.stem):
                        continue
                    yield identity_dir.name, path
        except OSError as exc:
            # A partial gallery silently poisons future matches; fail closed
            # so the caller keeps the last validated database instead.
            raise RuntimeError(f"reference folder is unreadable: {identity_dir}: {exc}") from exc


def _quality(result: dict[str, Any]) -> float:
    """Return a conservative, reproducible quality score for an embedding."""
    # The worker applies the minimum face-size/area gate. Keep the database
    # quality value useful for ranking without pretending it is photo_reorg's
    # multi-model quality metric.
    # Missing quality metadata is deliberately conservative. Older caches
    # predate quality scoring and must not be treated as perfect exemplars.
    return max(0.0, min(1.0, float(result.get("face_quality") or 0.0)))


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def validate_database(path: Path, *, min_faces: int = 0, min_identities: int = 0) -> dict[str, int]:
    """Validate the compatible schema and return bounded counts."""
    if not path.is_file():
        raise RuntimeError(f"face database is missing: {path}")
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"face_encodings", "person_metadata"} - tables
        if missing:
            raise RuntimeError("face database is missing tables: " + ",".join(sorted(missing)))
        faces = int(connection.execute("SELECT COUNT(*) FROM face_encodings").fetchone()[0])
        identities = int(connection.execute("SELECT COUNT(DISTINCT person_name) FROM face_encodings").fetchone()[0])
    if faces < min_faces:
        raise RuntimeError(f"face database has only {faces} faces (minimum {min_faces})")
    if identities < min_identities:
        raise RuntimeError(f"face database has only {identities} identities (minimum {min_identities})")
    return {"faces": faces, "identities": identities}


def _write_database(path: Path, rows: list[tuple[str, bytes, str, float, str]], metadata: dict[str, str]) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        _schema(connection)
        now = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            "INSERT INTO face_encodings (person_name, encoding, image_path, face_quality, model_used, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(person, encoding, image, quality, model, now, now) for person, encoding, image, quality, model in rows],
        )
        grouped: dict[str, list[tuple[float, bytes]]] = {}
        for person, encoding, _, quality, _ in rows:
            grouped.setdefault(person, []).append((quality, encoding))
        for person, entries in grouped.items():
            best_quality, best_encoding = max(entries, key=lambda item: item[0])
            avg_quality = sum(item[0] for item in entries) / len(entries)
            connection.execute(
                "INSERT INTO person_metadata (person_name, total_faces, avg_quality, best_encoding, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (person, len(entries), avg_quality, best_encoding, now, now),
            )
        connection.executemany(
            "INSERT OR REPLACE INTO picorg_face_database_meta (key, value) VALUES (?, ?)",
            [(str(key), str(value)) for key, value in metadata.items()],
        )
        connection.commit()
    return validate_database(path)


class _StreamingJSON:
    """Small bounded-memory reader for the cache's top-level JSON object."""

    def __init__(self, path: Path, chunk_size: int = 1024 * 1024) -> None:
        self.handle = path.open("r", encoding="utf-8")
        self.chunk_size = chunk_size
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def __enter__(self) -> "_StreamingJSON":
        return self

    def __exit__(self, *_: Any) -> None:
        self.handle.close()

    def _fill(self) -> bool:
        if self.eof:
            return False
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0
        chunk = self.handle.read(self.chunk_size)
        if not chunk:
            self.eof = True
            return False
        self.buffer += chunk
        return True

    def _skip_whitespace(self) -> None:
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer) or not self._fill():
                return

    def peek(self) -> str:
        self._skip_whitespace()
        if self.position >= len(self.buffer):
            raise ValueError("unexpected end of JSON cache")
        return self.buffer[self.position]

    def consume(self, expected: str | None = None) -> str:
        self._skip_whitespace()
        if self.position >= len(self.buffer):
            raise ValueError("unexpected end of JSON cache")
        value = self.buffer[self.position]
        if expected is not None and value != expected:
            raise ValueError(f"expected {expected!r}, found {value!r}")
        self.position += 1
        return value

    def value(self) -> Any:
        self._skip_whitespace()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
                self.position = end
                return value
            except json.JSONDecodeError:
                if not self._fill():
                    raise


def _iter_cache_records(path: Path) -> Iterable[tuple[Any, str, dict[str, Any]]]:
    """Yield model id and records from a cache without reading it all at once."""
    with _StreamingJSON(path) as stream:
        stream.consume("{")
        model_id: Any = None
        while True:
            if stream.peek() == "}":
                stream.consume("}")
                return
            key = stream.value()
            stream.consume(":")
            if key == "model_id":
                model_id = stream.value()
            elif key == "records":
                if stream.peek() != "{":
                    stream.value()
                else:
                    stream.consume("{")
                    while True:
                        if stream.peek() == "}":
                            stream.consume("}")
                            break
                        record_key = stream.value()
                        stream.consume(":")
                        record = stream.value()
                        if isinstance(record, dict):
                            yield model_id, str(record_key), record
                        delimiter = stream.peek()
                        if delimiter == ",":
                            stream.consume(",")
                        elif delimiter == "}":
                            continue
                        else:
                            raise ValueError("invalid records object delimiter")
            else:
                stream.value()
            delimiter = stream.peek()
            if delimiter == ",":
                stream.consume(",")
            elif delimiter == "}":
                stream.consume("}")
                return
            else:
                raise ValueError("invalid cache object delimiter")

def _load_embedding_cache(
    path: Path | None,
    *,
    wanted_paths: set[str] | None = None,
    wanted_fingerprints: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load reusable cache records without materializing unrelated entries."""
    if not path or not path.is_file():
        return {}, {}
    by_path: dict[str, dict[str, Any]] = {}
    by_fingerprint: dict[str, dict[str, Any]] = {}
    try:
        model_id = None
        for seen_model_id, record_path, value in _iter_cache_records(path):
            if model_id is None:
                model_id = seen_model_id
            fingerprint = str(value.get("fingerprint") or "")
            if wanted_paths is not None and record_path not in wanted_paths and fingerprint not in (wanted_fingerprints or set()):
                continue
            by_path[record_path] = value
            if fingerprint:
                by_fingerprint[fingerprint] = value
        if model_id not in (None, extractor.EMBEDDING_MODEL_ID):
            return {}, {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}, {}
    return by_path, by_fingerprint


def _load_hash_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("files", {}) if isinstance(payload, dict) else {}
        return {str(key): value for key, value in records.items() if isinstance(value, dict)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _write_embedding_cache(path: Path, records: dict[str, dict[str, Any]], progress: dict[str, Any]) -> None:
    """Atomically write updates while streaming unchanged legacy records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write('{"schema_version":2,"model_id":')
            json.dump(extractor.EMBEDDING_MODEL_ID, handle)
            handle.write(',"detector":"small","extraction_config":{"upsample_times":1,"num_jitters":1,"quality_version":1},"records":{')
            written: set[str] = set()
            first = True

            def write_record(record_path: str, value: dict[str, Any]) -> None:
                nonlocal first
                if not first:
                    handle.write(",")
                json.dump(record_path, handle, ensure_ascii=False)
                handle.write(":")
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                first = False
                written.add(record_path)

            if path.is_file():
                try:
                    for model_id, old_path, old_value in _iter_cache_records(path):
                        if model_id not in (None, extractor.EMBEDDING_MODEL_ID):
                            raise ValueError("cache model does not match current extractor")
                        write_record(old_path, records.get(old_path, old_value))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    # A malformed/partial prior cache is not trusted; the new
                    # cache still contains all records produced by this run.
                    handle.seek(0)
                    handle.truncate()
                    handle.write('{"schema_version":2,"model_id":')
                    json.dump(extractor.EMBEDDING_MODEL_ID, handle)
                    handle.write(',"detector":"small","extraction_config":{"upsample_times":1,"num_jitters":1,"quality_version":1},"records":{')
                    written.clear()
                    first = True
            for record_path, value in records.items():
                if record_path not in written:
                    write_record(record_path, value)
            handle.write('},"progress":')
            json.dump(progress, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("}\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def build_database(
    reference_root: Path,
    output: Path,
    *,
    workers: int | None = None,
    min_face_pixels: int = extractor.DEFAULT_MIN_FACE_PIXELS,
    min_face_area_ratio: float = extractor.DEFAULT_MIN_FACE_AREA_RATIO,
    num_jitters: int = 1,
    backup: Path | None = None,
    dry_run: bool = False,
    cache: Path | None = None,
    checkpoint_every: int = 1000,
    hash_workers: int = 2,
    hash_cache: Path | None = None,
) -> dict[str, Any]:
    """Build a PicOrg-owned compatible database from a read-only gallery."""
    image_tasks: list[tuple[str, str, int, float, bool, int, int]] = []
    path_identity: dict[str, str] = {}
    reference_items = list(iter_reference_images(reference_root))
    hash_records = _load_hash_cache(hash_cache)
    hash_cache_hits = 0

    def fingerprint_item(item: tuple[str, Path]) -> tuple[str, Path, str | None, str | None]:
        identity, path = item
        nonlocal hash_cache_hits
        for candidate in (str(path), str(path.resolve(strict=False))):
            cached = hash_records.get(candidate)
            try:
                stat = path.stat()
            except OSError as exc:
                return identity, path, None, str(exc)
            if (
                isinstance(cached, dict)
                and cached.get("sha256")
                and int(cached.get("size", -1)) == stat.st_size
                and int(cached.get("mtime_ns", -1)) == stat.st_mtime_ns
            ):
                hash_cache_hits += 1
                return identity, path, str(cached["sha256"]), None
        try:
            return identity, path, extractor.file_fingerprint(path), None
        except OSError as exc:
            return identity, path, None, str(exc)

    hash_workers = max(1, min(8, int(hash_workers)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=hash_workers) as hasher:
        for index, (identity, path, fingerprint, error) in enumerate(hasher.map(fingerprint_item, reference_items), 1):
            if error:
                print(f"warning: skipping unreadable reference {path}: {error}", flush=True)
                continue
            assert fingerprint is not None
            path_text = str(path)
            path_identity[path_text] = identity
            image_tasks.append((path_text, fingerprint, min_face_pixels, min_face_area_ratio, False, num_jitters, 1))
            if index % 1000 == 0:
                print(f"face database: fingerprinted {index}/{len(reference_items)} references", file=sys.stderr, flush=True)

    wanted_paths = {task[0] for task in image_tasks}
    wanted_fingerprints = {task[1] for task in image_tasks}
    cached_by_path, cached_by_fingerprint = _load_embedding_cache(
        cache,
        wanted_paths=wanted_paths,
        wanted_fingerprints=wanted_fingerprints,
    )
    cache_records = dict(cached_by_path)
    cached_results: list[dict[str, Any]] = []
    pending_tasks: list[tuple[str, str, int, float, bool, int, int]] = []
    cache_hits = 0
    for task in image_tasks:
        path_text, fingerprint = task[0], task[1]
        cached = cached_by_path.get(path_text) or cached_by_fingerprint.get(fingerprint)
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            normalized = dict(cached)
            # v1 caches stored successful embeddings without a status field.
            # Treat only a complete 128-value vector as an embedded result;
            # malformed/empty legacy records remain fail-closed.
            if not normalized.get("status") and isinstance(normalized.get("embedding"), list) and len(normalized["embedding"]) == 128:
                normalized["status"] = "embedded"
            cached_results.append({"path": path_text, **normalized})
            cache_records[path_text] = normalized
            cache_hits += 1
        else:
            pending_tasks.append(task)

    worker_count = extractor.resolve_face_workers(workers)
    if worker_count == 1:
        results = map(extractor._extract_dlib_face, pending_tasks)
        pool = None
    else:
        context = multiprocessing.get_context("spawn")
        pool = concurrent.futures.ProcessPoolExecutor(max_workers=worker_count, mp_context=context)
        results = pool.map(extractor._extract_dlib_face, pending_tasks, chunksize=1)

    rows: list[tuple[str, bytes, str, float, str]] = []
    statuses: dict[str, int] = {}
    error_samples: list[dict[str, str]] = []
    processed = 0
    last_checkpoint = time.monotonic()
    try:
        for result in chain(cached_results, results):
            processed += 1
            if processed == 1 or processed % max(1, checkpoint_every) == 0:
                print(
                    f"face database: {processed}/{len(image_tasks)} processed, "
                    f"cached={cache_hits}, embedded={len(rows)}",
                    file=sys.stderr,
                    flush=True,
                )
            status = str(result.get("status") or "error")
            statuses[status] = statuses.get(status, 0) + 1
            if status == "error" and len(error_samples) < 20:
                error_samples.append(
                    {
                        "path": str(result.get("path") or ""),
                        "type": str(result.get("error_type") or "RuntimeError"),
                        "message": str(result.get("error_message") or "")[:240],
                    }
                )
            if status != "embedded":
                cache_records[str(result.get("path") or "")] = {
                    key: value for key, value in result.items() if key != "path"
                }
                continue
            path = str(result["path"])
            embedding = result.get("embedding") or []
            if len(embedding) != 128 or path not in path_identity:
                statuses["invalid_embedding"] = statuses.get("invalid_embedding", 0) + 1
                continue
            import numpy as np

            rows.append((path_identity[path], np.asarray(embedding, dtype=np.float64).tobytes(), path, _quality(result), extractor.EMBEDDING_MODEL_ID))
            cache_records[path] = {key: value for key, value in result.items() if key != "path"}
            if cache and (processed % max(1, checkpoint_every) == 0 or time.monotonic() - last_checkpoint >= 300):
                _write_embedding_cache(cache, cache_records, {"selected": len(image_tasks), "processed": processed, "cached": cache_hits})
                last_checkpoint = time.monotonic()
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

    report: dict[str, Any] = {
        "reference_root": str(reference_root),
        "output": str(output),
        "selected": len(image_tasks),
        "embedded": len(rows),
        "statuses": statuses,
        "error_samples": error_samples,
        "workers": worker_count,
        "hash_workers": hash_workers,
        "hash_cache": str(hash_cache) if hash_cache else None,
        "hash_cache_hits": hash_cache_hits,
        "cache": str(cache) if cache else None,
        "cached": cache_hits,
        "model_id": extractor.EMBEDDING_MODEL_ID,
    }
    if cache:
        _write_embedding_cache(cache, cache_records, {"selected": len(image_tasks), "processed": processed, "cached": cache_hits, "embedded": len(rows)})
    if dry_run:
        return report
    if image_tasks and not rows:
        raise RuntimeError(
            "no usable face embeddings were extracted; "
            + json.dumps({"selected": len(image_tasks), "statuses": statuses, "errors": error_samples[:3]}, sort_keys=True)
        )

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent)))
    temp_db = temp_dir / output.name
    try:
        counts = _write_database(temp_db, rows, {"backend": "picorg", "model_id": extractor.EMBEDDING_MODEL_ID, "reference_root": str(reference_root)})
        if backup and output.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, backup)
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_db, output)
        report.update(counts)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_DB)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--min-face-pixels", type=int, default=extractor.DEFAULT_MIN_FACE_PIXELS)
    parser.add_argument("--min-face-area-ratio", type=float, default=extractor.DEFAULT_MIN_FACE_AREA_RATIO)
    parser.add_argument("--num-jitters", type=int, default=1)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache", type=Path, help="optional fingerprinted extraction cache for resume/reuse")
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--hash-workers", type=int, default=2)
    parser.add_argument("--hash-cache", type=Path, help="optional mtime/size-validated SHA cache")
    args = parser.parse_args()
    report = build_database(
        args.reference_root,
        args.output,
        workers=args.workers,
        min_face_pixels=args.min_face_pixels,
        min_face_area_ratio=args.min_face_area_ratio,
        num_jitters=args.num_jitters,
        backup=args.backup,
        dry_run=args.dry_run,
        cache=args.cache,
        checkpoint_every=args.checkpoint_every,
        hash_workers=args.hash_workers,
        hash_cache=args.hash_cache,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
