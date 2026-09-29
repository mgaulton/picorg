#!/usr/bin/env python3
"""Build a durable, content-addressed face baseline from canonical media.

The MetaDaily/RedditDaily trees and the organized tree are read-only inputs.
Only PicOrg's local evidence database, embedding cache, marker ledger, and a
manifest are written.  The job is incremental: a matching path fingerprint
reuses the existing embedding cache and unchanged evidence rows are harmless
to replay.
"""

from __future__ import annotations

import argparse
import array
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import picorg_sorter as sorter
from identity_evidence_store import open_store, record_canonical_baseline, record_pipeline_run

ROOT = Path(__file__).resolve().parent
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
DEFAULT_ORGANIZED = Path("/mnt/elements16/@mixedpics_sorted")
DEFAULT_METADAILY = Path("/mnt/elements16a/Pron/metadaily/downloads")
DEFAULT_REDDITDAILY = Path("/mnt/elements16a/Pron/redditdaily/downloads")
DEFAULT_ASSORTED = Path("/mnt/assorted")
DEFAULT_FACE_CACHE = ROOT / ".cache/picorg/face_embeddings.sqlite3"
DEFAULT_EVIDENCE_DB = ROOT / ".cache/picorg/identity_evidence.sqlite3"
DEFAULT_OUTPUT = ROOT / ".cache/picorg/canonical-face-baseline.json"
DEFAULT_HASH_CACHE = ROOT / ".cache/picorg/canonical-face-baseline-hashes.json"
DEFAULT_MARKERS = ROOT / "identity_face_markers.json"
MODEL_ID = "dlib-face-recognition-small-v1"
MODEL_VERSION = "face_cluster_unmatched.v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_hash_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    return records if isinstance(records, dict) else {}


def fingerprint(path: Path, cache: dict[str, dict[str, Any]]) -> tuple[str | None, os.stat_result | None, bool]:
    stat = safe_stat(path)
    if stat is None:
        return None, None, False
    key = str(path)
    cached = cache.get(key)
    if (isinstance(cached, dict) and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns and isinstance(cached.get("sha256"), str)):
        return str(cached["sha256"]), stat, True
    try:
        digest = sha256_file(path)
    except OSError:
        return None, stat, False
    cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}
    return digest, stat, False


def load_trusted_identities(evidence_db: Path) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Return alias->canonical, trusted canonicals, and display names."""
    # Initialize project block/ambiguous-token overlays before filtering the
    # evidence DB; this keeps baseline folder names consistent with matching.
    sorter.load_identity_catalog()
    aliases: dict[str, str] = {}
    trusted: set[str] = set()
    display: dict[str, str] = {}
    with open_store(evidence_db) as connection:
        rows = connection.execute(
            "SELECT canonical, display_name, trust_level, status FROM identities"
        ).fetchall()
        for canonical, display_name, trust_level, status in rows:
            canonical = sorter.normalize_key(str(canonical))
            if not canonical or str(status) not in {"confirmed", "active"}:
                continue
            if str(trust_level) not in {"registry_confirmed", "face_confirmed", "manual_confirmed"}:
                continue
            # Registry membership alone does not make a topic/container name
            # suitable as a person face authority.  Ambiguous and generic
            # canonicals stay review context only.
            if sorter.is_generic_identity_token(canonical) or canonical in sorter.PROJECT_AMBIGUOUS_TOKENS:
                continue
            trusted.add(canonical)
            display[canonical] = str(display_name or canonical)
            aliases.setdefault(canonical, canonical)
        for alias_key, identity, _alias, _source, _updated in connection.execute(
            "SELECT alias_key, identity, alias, source, updated_at FROM aliases"
        ):
            identity = sorter.normalize_key(str(identity))
            alias_key = sorter.normalize_key(str(alias_key))
            if identity in trusted and alias_key and not sorter.is_generic_identity_token(alias_key) and alias_key not in sorter.PROJECT_AMBIGUOUS_TOKENS:
                existing = aliases.get(alias_key)
                if existing is None or existing == identity:
                    aliases[alias_key] = identity
                else:
                    aliases.pop(alias_key, None)
    return aliases, trusted, display


def safe_children(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as entries:
            return list(entries)
    except OSError:
        return []


def candidate_directories(root: Path, aliases: dict[str, str]) -> Iterable[tuple[str, Path, str]]:
    """Yield canonical identity directories without scanning unrelated trees."""
    if not safe_is_dir(root):
        return
    direct = safe_children(root)
    root_name = root.name.lower()
    for entry in direct:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if not is_dir or entry.name.startswith("."):
            continue
        key = sorter.normalize_key(entry.name)
        canonical = aliases.get(key)
        if canonical:
            yield canonical, Path(entry.path), root_name
            continue
        # The organized tree is grouped by source family first.
        if root == DEFAULT_ORGANIZED or root_name == DEFAULT_ORGANIZED.name.lower():
            for child in safe_children(Path(entry.path)):
                try:
                    if not child.is_dir(follow_symlinks=False) or child.name.startswith("."):
                        continue
                except OSError:
                    continue
                canonical = aliases.get(sorter.normalize_key(child.name))
                if canonical:
                    yield canonical, Path(child.path), entry.name


def iter_media(root: Path, aliases: dict[str, str]) -> Iterable[tuple[str, Path, str]]:
    for canonical, directory, source_family in candidate_directories(root, aliases):
        stack = [directory]
        while stack:
            current = stack.pop()
            for entry in safe_children(current):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in {"thumb", "thumbs", "thumbnail", "thumbnails"}:
                            stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                path = Path(entry.path)
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                if "thumbnail" in path.stem.lower() or path.stem.lower().startswith("thumb"):
                    continue
                yield canonical, path, source_family


def load_embedding_cache(path: Path) -> dict[str, tuple[str | None, bytes, dict[str, Any]]]:
    result: dict[str, tuple[str | None, bytes, dict[str, Any]]] = {}
    if not path.is_file():
        return result
    try:
        connection = sqlite3.connect(path, timeout=5)
        rows = connection.execute("SELECT path, fingerprint, embedding, metadata_json FROM embeddings WHERE embedding IS NOT NULL")
        for source_path, digest, embedding, metadata_json in rows:
            if not isinstance(embedding, (bytes, bytearray)) or len(embedding) not in {512, 1024, 2048}:
                continue
            try:
                metadata = json.loads(metadata_json or "{}")
            except json.JSONDecodeError:
                metadata = {}
            result[str(source_path)] = (str(digest) if digest else None, bytes(embedding), metadata if isinstance(metadata, dict) else {})
        connection.close()
    except (OSError, sqlite3.Error):
        return {}
    return result


def cache_embeddings(path: Path, results: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("CREATE TABLE IF NOT EXISTS embeddings (path TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, embedding BLOB, metadata_json TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        with connection:
            for result in results:
                if not result.get("path") or not result.get("fingerprint"):
                    continue
                vector = result.get("embedding")
                blob = None
                if isinstance(vector, list):
                    values = array.array("d", (float(value) for value in vector))
                    blob = values.tobytes()
                metadata = {key: result.get(key) for key in ("face_quality", "face_box", "face_count", "quality_version") if result.get(key) is not None}
                connection.execute(
                    "INSERT INTO embeddings(path,fingerprint,status,embedding,metadata_json,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status,embedding=excluded.embedding,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
                    (str(result["path"]), str(result["fingerprint"]), str(result.get("status") or "error"), blob, json.dumps(metadata, sort_keys=True)),
                )
        connection.close()
    except sqlite3.Error as exc:
        print(f"warning: embedding cache update failed: {exc}", flush=True)


def decode_embedding(blob: bytes) -> bytes | None:
    if len(blob) != 1024:
        return None
    values = array.array("d")
    values.frombytes(blob)
    return values.tobytes() if len(values) == 128 else None


def extract_missing(paths: list[tuple[Path, str]], hashes: dict[str, str], workers: int) -> list[dict[str, Any]]:
    if not paths:
        return []
    from face_cluster_unmatched import _extract_dlib_face

    tasks = [(str(path), hashes[str(path)], 80, 0.01, False, 1, 1) for path, _identity in paths]
    results: list[dict[str, Any]] = []
    try:
        progress_seconds = max(1.0, float(os.environ.get("PICORG_BASELINE_PROGRESS_SECONDS", "15")))
    except ValueError:
        progress_seconds = 15.0
    started = time.monotonic()
    last_progress = started

    def report_progress(force: bool = False) -> None:
        nonlocal last_progress
        completed = len(results)
        now = time.monotonic()
        if not force and completed % 100 != 0 and now - last_progress < progress_seconds:
            return
        elapsed = max(0.001, now - started)
        rate = completed / elapsed
        remaining = max(0, len(tasks) - completed)
        eta = remaining / rate if rate > 0 else 0.0
        print(
            f"baseline extraction: {completed}/{len(tasks)} rate={rate:.1f}/s "
            f"eta={eta:.0f}s workers={workers}",
            flush=True,
        )
        last_progress = now

    if workers <= 1:
        iterator = map(_extract_dlib_face, tasks)
        for result in iterator:
            results.append(result)
            report_progress()
    else:
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
            for result in executor.map(_extract_dlib_face, tasks, chunksize=1):
                results.append(result)
                report_progress()
    report_progress(force=True)
    return results


def merge_marker_ledger(path: Path, records: list[dict[str, Any]]) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    existing = payload.get("markers") if isinstance(payload, dict) else None
    markers = existing if isinstance(existing, list) else []
    by_key = {str(item.get("key")): item for item in markers if isinstance(item, dict) and item.get("key")}
    for record in records:
        key = str(record["marker_key"])
        by_key[key] = {
            "key": key,
            "identity": record["identity"],
            "family": "canonical",
            "path": record["path"],
            "sha256": record["sha256"],
            "status": "confirmed",
            "source": "canonical_baseline",
            "saved_at": record.get("updated_at") or now(),
        }
    atomic_write(path, {"schema_version": 2, "updated": now(), "markers": list(by_key.values())})
    return len(by_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organized-root", type=Path, default=DEFAULT_ORGANIZED)
    parser.add_argument("--metadaily-root", type=Path, default=DEFAULT_METADAILY)
    parser.add_argument("--redditdaily-root", type=Path, default=DEFAULT_REDDITDAILY)
    parser.add_argument("--assorted-root", type=Path, default=DEFAULT_ASSORTED,
                        help="read-only person-named assorted-media root")
    parser.add_argument("--evidence-db", type=Path, default=DEFAULT_EVIDENCE_DB)
    parser.add_argument("--embedding-cache", type=Path, default=DEFAULT_FACE_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hash-cache", type=Path, default=DEFAULT_HASH_CACHE)
    parser.add_argument("--markers", type=Path, default=DEFAULT_MARKERS)
    parser.add_argument("--extract-missing", action="store_true", help="extract uncached canonical media with the existing dlib backend")
    parser.add_argument("--workers", type=int, default=max(1, min(2, int(os.environ.get("PICORG_FACE_WORKERS", "1")))) )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    run_id = f"canonical-baseline:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"
    aliases, trusted, display = load_trusted_identities(args.evidence_db)
    if not trusted:
        print("error: no confirmed canonical identities in evidence database", flush=True)
        return 2
    roots = [args.organized_root, args.metadaily_root, args.redditdaily_root, args.assorted_root]
    if not args.dry_run:
        record_pipeline_run(args.evidence_db, run_id=run_id, job="canonical_baseline", status="running", stage="scanning", total=None)
    hash_cache = load_hash_cache(args.hash_cache)
    candidates: dict[str, tuple[str, Path, str, os.stat_result]] = {}
    counters = defaultdict(int)
    try:
        progress_interval = max(1.0, float(os.environ.get("PICORG_BASELINE_PROGRESS_SECONDS", "15")))
    except ValueError:
        progress_interval = 15.0
    progress_started = time.monotonic()
    last_progress = progress_started
    for root in roots:
        root_started = time.monotonic()
        print(f"baseline scan: starting root={root}", flush=True)
        if not safe_is_dir(root):
            counters["unavailable_roots"] += 1
            print(f"warning: canonical baseline root unavailable: {root}", flush=True)
            continue
        for identity, path, source_family in iter_media(root, aliases):
            key = str(path)
            digest, stat, _cached = fingerprint(path, hash_cache)
            if not digest or stat is None:
                counters["unreadable"] += 1
                continue
            prior = candidates.get(key)
            if prior and prior[0] != identity:
                counters["path_conflicts"] += 1
                continue
            candidates[key] = (identity, path, source_family, stat)
            counters["files"] += 1
            now_monotonic = time.monotonic()
            if now_monotonic - last_progress >= progress_interval:
                elapsed = max(0.1, now_monotonic - progress_started)
                print(
                    f"baseline scan: heartbeat root={root.name} files={counters['files']} "
                    f"unreadable={counters['unreadable']} elapsed={elapsed:.0f}s "
                    f"rate={counters['files'] / elapsed:.1f}/s",
                    flush=True,
                )
                last_progress = now_monotonic
        print(f"baseline scan: completed root={root} files={counters['files']} unreadable={counters['unreadable']} elapsed={time.monotonic() - root_started:.1f}s", flush=True)
    atomic_write(args.hash_cache, {"schema_version": 1, "updated": now(), "records": hash_cache})
    cached_embeddings = load_embedding_cache(args.embedding_cache)
    missing: list[tuple[Path, str]] = []
    hashes: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for key, (identity, path, source_family, stat) in candidates.items():
        digest = hash_cache[key]["sha256"]
        hashes[key] = digest
        cached = cached_embeddings.get(key)
        if cached and cached[0] == digest and (blob := decode_embedding(cached[1])):
            records.append({"identity": identity, "path": key, "sha256": digest, "face_index": 0,
                            "model_id": MODEL_ID, "model_version": MODEL_VERSION, "quality": cached[2].get("face_quality"),
                            "embedding": blob, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                            "provenance": {"source_root": str(path.parents[0]), "source_family": source_family, "cache": "face_embeddings.sqlite3"}})
        elif args.extract_missing:
            missing.append((path, identity))
        else:
            counters["embedding_missing"] += 1
    print(f"baseline candidates={len(candidates)} cached={len(records)} missing={len(missing)}", flush=True)
    extraction_results = extract_missing(missing, hashes, args.workers) if args.extract_missing else []
    cache_updates = []
    for result in extraction_results:
        cache_updates.append(result)
        if result.get("status") != "embedded":
            counters[str(result.get("status") or "error")] += 1
            continue
        key = str(result["path"])
        identity, path, source_family, stat = candidates[key]
        vector = array.array("d", (float(value) for value in result.get("embedding") or [])).tobytes()
        records.append({"identity": identity, "path": key, "sha256": hashes[key], "face_index": 0,
                        "model_id": MODEL_ID, "model_version": MODEL_VERSION, "quality": result.get("face_quality"),
                        "geometry": {"box": result.get("face_box"), "face_count": result.get("face_count")},
                        "embedding": vector, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "provenance": {"source_root": str(path.parents[0]), "source_family": source_family, "cache": "extracted"}})
    if cache_updates and not args.dry_run:
        cache_embeddings(args.embedding_cache, cache_updates)
    # The same content under two identities is unsafe baseline evidence.
    by_digest: dict[str, set[str]] = defaultdict(set)
    for record in records:
        by_digest[record["sha256"]].add(record["identity"])
    conflicts = {digest for digest, identities in by_digest.items() if len(identities) > 1}
    if conflicts:
        records = [record for record in records if record["sha256"] not in conflicts]
        counters["conflict_hashes"] = len(conflicts)
    for record in records:
        record["marker_key"] = f"baseline:{record['identity']}:{record['sha256']}:{record['face_index']}:{MODEL_VERSION}"
        record["updated_at"] = now()
    if not args.dry_run:
        written = 0
        for offset in range(0, len(records), 500):
            written += record_canonical_baseline(args.evidence_db, records[offset:offset + 500], run_id=run_id)
        merge_marker_ledger(args.markers, records)
        record_pipeline_run(args.evidence_db, run_id=run_id, job="canonical_baseline", status="complete", stage="complete", total=len(candidates), processed=written, finished=True, metadata=dict(counters))
    manifest_records = [{key: record[key] for key in ("identity", "path", "sha256", "face_index", "model_id", "model_version", "quality", "provenance", "marker_key")} for record in records]
    atomic_write(args.output, {"schema_version": 1, "run_id": run_id, "generated_at": now(), "dry_run": bool(args.dry_run), "model_id": MODEL_ID, "model_version": MODEL_VERSION, "trusted_identities": len(trusted), "candidate_files": len(candidates), "baseline_records": len(records), "counters": dict(counters), "roots": [str(root) for root in roots], "records": manifest_records})
    print(json.dumps({"run_id": run_id, "trusted_identities": len(trusted), "candidate_files": len(candidates), "baseline_records": len(records), "counters": dict(counters), "output": str(args.output)}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
