#!/usr/bin/env python3
"""Report-only face grouping for generic/unmatched picorg clusters.

This intentionally does not move files or update the picorg registry. It uses
the PicOrg-compatible face database to produce candidates that can later be
reviewed and converted into verified aliases or apply decisions. When no
PicOrg-owned database exists, the legacy photo_reorg database is used for
backward compatibility.
"""

from __future__ import annotations

import argparse
import atexit
from datetime import datetime, timezone
import json
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import face_recognition
import numpy as np
from face_embedding_store import list_paths as list_store_paths
from face_embedding_store import read_records as read_store_records
from face_embedding_store import upsert_records as upsert_store_records


LEGACY_DB = Path("/opt/photo_reorg/data/high_accuracy_faces.db")
PICORG_DB = Path("/opt/picorg/.cache/picorg/face_database.sqlite3")
DEFAULT_GALLERY_MANIFEST = PICORG_DB.with_name("reference-gallery.json")


def _is_readable_file(raw_path: str) -> bool:
    """Treat degraded/FUSE stat failures as unavailable media."""
    try:
        return Path(raw_path).is_file()
    except OSError:
        return False
DEFAULT_TEMP_ROOT = Path(tempfile.gettempdir())
DEFAULT_AUDIT = DEFAULT_TEMP_ROOT / "picorg_periodic_apply.json"
DEFAULT_OUTPUT = DEFAULT_TEMP_ROOT / "picorg_face_grouping.json"
DEFAULT_TIMING_LOG = Path("/opt/picorg/.cache/picorg/face-match-timings.jsonl")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_REFERENCE_ARRAY_CACHE: dict[int, tuple[np.ndarray, list[str], np.ndarray, dict[str, tuple[int, int]]]] = {}


def default_database() -> Path:
    configured = os.environ.get("PICORG_FACE_DB")
    if configured:
        return Path(configured)
    return PICORG_DB if PICORG_DB.is_file() else LEGACY_DB


def load_references(db_path: Path, gallery_manifest: Path | None = None) -> dict[str, list[tuple[np.ndarray, float]]]:
    allowed: set[str] | None = None
    if gallery_manifest and gallery_manifest.is_file():
        try:
            payload = json.loads(gallery_manifest.read_text(encoding="utf-8"))
            manifest_db = str(payload.get("database") or "") if isinstance(payload, dict) else ""
            if manifest_db and Path(manifest_db).resolve(strict=False) != db_path.resolve(strict=False):
                raise ValueError("gallery manifest belongs to a different database")
            allowed = {
                str(item[0])
                for entries in (payload.get("gallery", {}) if isinstance(payload, dict) else {}).values()
                if isinstance(entries, list)
                for item in entries
                if isinstance(item, list) and item
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            allowed = None
    grouped: dict[str, list[tuple[np.ndarray, float]]] = defaultdict(list)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT person_name, image_path, encoding, face_quality FROM face_encodings")
        for person_name, image_path, blob, quality in rows:
            # The manifest is an optional, quality-diverse exemplar subset;
            # absent/invalid manifests deliberately fall back to the full DB.
            if allowed is not None and str(image_path) not in allowed:
                continue
            vector = np.frombuffer(blob, dtype=np.float64)
            if vector.shape == (128,):
                grouped[str(person_name)].append((vector, float(quality or 0.0)))
    return dict(grouped)


def load_embedding_cache(cache_path: Path | None) -> dict[str, dict[str, object]]:
    """Load prior dlib embeddings for fast, read-only identity matching."""
    if not cache_path or not cache_path.is_file():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    return records if isinstance(records, dict) else {}


def rank_candidates(
    encoding: np.ndarray,
    references: dict[str, list[tuple[np.ndarray, float]]],
    quality_weighted: bool = False,
) -> list[dict[str, object]]:
    cache_key = id(references)
    cached = _REFERENCE_ARRAY_CACHE.get(cache_key)
    if cached is None:
        vectors: list[np.ndarray] = []
        persons: list[str] = []
        qualities: list[float] = []
        spans: dict[str, tuple[int, int]] = {}
        for person, entries in references.items():
            start = len(vectors)
            vectors.extend(vector for vector, _ in entries)
            persons.extend([person] * len(entries))
            qualities.extend(float(quality) for _, quality in entries)
            spans[person] = (start, len(vectors))
        cached = (np.asarray(vectors, dtype=np.float64), persons, np.asarray(qualities, dtype=np.float64), spans)
        _REFERENCE_ARRAY_CACHE[cache_key] = cached
    matrix, persons, qualities, spans = cached
    distances = np.linalg.norm(matrix - np.asarray(encoding, dtype=np.float64), axis=1)
    adjusted = distances / (0.75 + 0.25 * np.clip(qualities, 0.0, 1.0)) if quality_weighted else distances
    ranked = []
    for person, (start, end) in spans.items():
        local_index = start + int(np.argmin(adjusted[start:end]))
        ranked.append({"person": person, "distance": round(float(distances[local_index]), 5), "quality_adjusted_distance": round(float(adjusted[local_index]), 5), "reference_quality": round(float(qualities[local_index]), 5)})
    key = "quality_adjusted_distance" if quality_weighted else "distance"
    return sorted(ranked, key=lambda item: float(item[key]))


def confident_face_matches(
    face_candidates: list[list[dict[str, object]]],
    threshold: float,
    margin: float,
) -> list[tuple[int, list[dict[str, object]]]]:
    """Return the faces whose top identity clears both match gates.

    A file may be assigned to a person only when this returns exactly one
    face. Keeping this decision separate from encoding makes the multi-face
    policy explicit and easy to test without loading image models.
    """
    matches: list[tuple[int, list[dict[str, object]]]] = []
    for face_index, candidates in enumerate(face_candidates):
        if not candidates:
            continue
        best = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None
        if (
            float(best["distance"]) <= threshold
            and (second is None or float(second["distance"]) - float(best["distance"]) >= margin)
        ):
            matches.append((face_index, candidates))
    return matches


def multi_face_assignment_policy(face_count: int, confident_face_count: int) -> str:
    """Describe whether a multi-face result is eligible for identity apply."""
    if int(face_count) <= 1:
        return "single_face"
    if int(confident_face_count) == 1:
        return "one_known_other_unresolved"
    return "review_multiple_known_or_unresolved"


def face_quality(location: tuple[int, int, int, int], shape: tuple[int, ...]) -> dict[str, float]:
    top, right, bottom, left = location
    height = max(0, bottom - top)
    width = max(0, right - left)
    image_area = max(1, int(shape[0]) * int(shape[1]))
    return {
        "width": float(width),
        "height": float(height),
        "min_dimension": float(min(width, height)),
        "area_ratio": round((width * height) / image_area, 6),
    }


def format_progress(processed: int, total: int, width: int = 30) -> str:
    """Return a compact progress bar suitable for terminals and the TUI log."""
    total = max(0, int(total))
    processed = max(0, min(int(processed), total)) if total else 0
    width = max(10, int(width))
    fraction = (processed / total) if total else 1.0
    filled = min(width, int(fraction * width))
    bar = "#" * filled + "-" * (width - filled)
    return f"progress [{bar}] {fraction:6.2%} ({processed}/{total})"


def _timing_signature(args: argparse.Namespace) -> dict[str, object]:
    """Return settings that materially affect face-matching throughput."""
    return {
        "db": str(args.db),
        "gallery_manifest": str(args.gallery_manifest) if args.gallery_manifest else None,
        "threshold": float(args.threshold),
        "margin": float(args.margin),
        "num_jitters": int(args.num_jitters),
        "min_face_pixels": int(args.min_face_pixels),
        "min_face_area_ratio": float(args.min_face_area_ratio),
        "quality_weighted": bool(args.quality_weighted),
        "adaptive_jitters": bool(args.adaptive_jitters),
    }


def _load_timing_history(path: Path, signature: dict[str, object]) -> list[dict[str, object]]:
    """Load recent completed runs; malformed lines are ignored safely."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-200:]
    except (FileNotFoundError, OSError):
        return []
    history: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("event") == "complete" and record.get("signature") == signature:
            try:
                if float(record.get("rate", 0.0)) > 0 and int(record.get("processed", 0)) > 0:
                    history.append(record)
            except (TypeError, ValueError):
                continue
    return history[-5:]


def _append_timing_record(path: Path, record: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        # Telemetry must never make a report-only matching run fail.
        print(f"timing warning: could not write {path}: {exc}", flush=True)


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"


def _progress_interval() -> float:
    """Return the bounded heartbeat interval used by long matching loops."""
    try:
        return max(1.0, float(os.environ.get("PICORG_PROGRESS_SECONDS", "15")))
    except ValueError:
        return 15.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--db", type=Path, default=default_database())
    parser.add_argument(
        "--gallery-manifest",
        type=Path,
        default=Path(os.environ.get("PICORG_GALLERY_MANIFEST", str(DEFAULT_GALLERY_MANIFEST))),
        help="quality-diverse exemplar manifest (default: use the rebuild manifest when present)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timing-log", type=Path, default=Path(os.environ.get("PICORG_FACE_TIMING_LOG", str(DEFAULT_TIMING_LOG))), help="JSONL history used for throughput and duration estimates")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--limit-per-cluster", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=0.48)
    parser.add_argument("--margin", type=float, default=0.04)
    parser.add_argument("--quality-weighted", action="store_true", help="use stored reference quality as a conservative ranking tie-breaker")
    parser.add_argument("--min-face-pixels", type=int, default=80)
    parser.add_argument("--min-face-area-ratio", type=float, default=0.005)
    parser.add_argument("--num-jitters", type=int, default=1)
    parser.add_argument("--adaptive-jitters", action="store_true", help="retry only borderline faces with higher jitter")
    parser.add_argument("--embedding-cache", type=Path, help="optional prior dlib embedding cache for unchanged files")
    parser.add_argument("--embedding-store", type=Path, help="optional indexed SQLite embedding store")
    parser.add_argument("--trust-embedding-cache", action="store_true", help="reuse cache entries without an audit fingerprint match")
    parser.add_argument(
        "--allow-multi-face",
        action="store_true",
        help="legacy compatibility flag; all faces are evaluated, and assignment requires exactly one confident face",
    )
    args = parser.parse_args()
    run_started_at = time.monotonic()
    run_started_wall = datetime.now(timezone.utc)

    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    embedding_cache = {} if args.embedding_store else load_embedding_cache(args.embedding_cache)
    store_paths = list_store_paths(args.embedding_store) if args.embedding_store else set()
    audit_fingerprints = audit.get("source_fingerprints", {}) if isinstance(audit, dict) else {}
    cache_hits = 0
    store_updates: list[tuple[str, dict[str, object]]] = []

    def queue_store(path: str, status: str, embedding: object = None) -> None:
        if not args.embedding_store:
            return
        record: dict[str, object] = {"status": status}
        fingerprint = audit_fingerprints.get(path) if isinstance(audit_fingerprints, dict) else None
        if fingerprint:
            record["fingerprint"] = fingerprint
        if isinstance(embedding, np.ndarray) and embedding.shape == (128,):
            record["embedding"] = embedding.tolist()
        store_updates.append((path, record))

    unmatched = []
    for item in audit.get("results", []):
        if item.get("rule") != "unmatched" or Path(item["path"]).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        # A trusted embedding cache already represents successfully decoded
        # files; avoid tens of thousands of network stat() calls.  Uncached
        # paths are still validated by the decoder and remain reviewable on
        # error.
        cached = embedding_cache.get(str(item["path"])) if args.trust_embedding_cache else None
        cached_in_store = args.trust_embedding_cache and str(item["path"]) in store_paths
        if args.trust_embedding_cache and (cached_in_store or isinstance(cached, dict)) and (
            cached_in_store or (
            isinstance(cached.get("embedding"), list) or cached.get("status") in {"no_face", "multi_face_deferred"}
            )
        ):
            unmatched.append(item)
            continue
        if not args.trust_embedding_cache and not _is_readable_file(str(item["path"])):
            continue
        unmatched.append(item)

    # Sample each generic title cluster first so one repeated gallery cannot
    # consume the entire run. Set --limit-per-cluster 0 for all files.
    clusters: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in unmatched:
        clusters[str(item.get("title") or Path(item["path"]).stem)].append(item)
    selected = []
    for items in clusters.values():
        selected.extend(items if args.limit_per_cluster <= 0 else items[:args.limit_per_cluster])
    selected.sort(key=lambda item: str(item["path"]))
    total_selected = len(selected)
    if args.offset < 0 or args.offset > total_selected:
        parser.error(f"--offset must be between 0 and {total_selected}")
    selected = selected[args.offset:]
    selected = selected if args.max_files <= 0 else selected[:args.max_files]

    if args.embedding_store:
        embedding_cache = read_store_records(args.embedding_store, (item["path"] for item in selected))

    references = load_references(args.db, args.gallery_manifest)
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    cluster_matches: dict[str, list[str]] = defaultdict(list)
    results = []
    timing_signature = _timing_signature(args)
    timing_history = _load_timing_history(args.timing_log, timing_signature)
    prior_estimate = None
    if timing_history:
        rates = sorted(float(record["rate"]) for record in timing_history)
        median_rate = rates[len(rates) // 2]
        prior_estimate = len(selected) / median_rate if median_rate > 0 else None
        print(
            f"prior estimate: {_format_duration(prior_estimate)} for {len(selected)} files "
            f"(median {median_rate:.2f}/s from {len(timing_history)} completed run(s))",
            flush=True,
        )
    # Include audit loading, selection, and reference loading in the reported
    # duration so estimates reflect the complete command rather than only the
    # face-encoding loop.
    started_at = run_started_at
    started_wall = run_started_wall
    progress_seconds = _progress_interval()
    last_progress = started_at
    print(
        f"face match: starting {len(selected)} files, references={sum(len(items) for items in references.values())}, "
        f"heartbeat={progress_seconds:g}s",
        flush=True,
    )
    timing_state = {"finalized": False}

    def record_interrupted_run() -> None:
        if timing_state["finalized"]:
            return
        elapsed = max(0.001, time.monotonic() - started_at)
        _append_timing_record(args.timing_log, {
            "event": "aborted",
            "started_at": started_wall.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 3),
            "processed": len(results),
            "selected": len(selected),
            "signature": timing_signature,
        })

    atexit.register(record_interrupted_run)
    _append_timing_record(args.timing_log, {
        "event": "start",
        "started_at": started_wall.isoformat(),
        "selected": len(selected),
        "signature": timing_signature,
    })
    for index, item in enumerate(selected, 1):
        now = time.monotonic()
        if index % 100 == 0 or index == len(selected) or now - last_progress >= progress_seconds:
            elapsed = max(0.001, now - started_at)
            rate = index / elapsed
            remaining = max(0, len(selected) - index)
            eta = (remaining / rate) if rate > 0 else 0.0
            print(
                f"face match: {format_progress(index, len(selected))} rate={rate:.2f}/s eta={eta:.0f}s",
                flush=True,
            )
            last_progress = now
        path = Path(item["path"])
        cached = embedding_cache.get(str(path))
        cached_embedding = cached.get("embedding") if isinstance(cached, dict) else None
        cache_fingerprint = cached.get("fingerprint") if isinstance(cached, dict) else None
        audit_fingerprint = audit_fingerprints.get(str(path)) if isinstance(audit_fingerprints, dict) else None
        cached_status = cached.get("status") if isinstance(cached, dict) else None
        if args.trust_embedding_cache and cached_status in {"no_face", "multi_face_deferred"}:
            cache_hits += 1
            results.append({"path": str(path), "status": cached_status, "cached": True})
            continue
        cache_valid = (
            isinstance(cached_embedding, list)
            and len(cached_embedding) == 128
            and (args.trust_embedding_cache or (audit_fingerprint and cache_fingerprint == audit_fingerprint))
        )
        if cache_valid:
            # Cached records represent a single usable face.  Keep geometry
            # conservative; matching remains gated by threshold and margin.
            cache_hits += 1
            encodings = [np.asarray(cached_embedding, dtype=np.float64)]
            locations = [(0, 0, 100, 100)]
            image = np.zeros((100, 100, 3), dtype=np.uint8)
        else:
            try:
                image = face_recognition.load_image_file(str(path))
                locations = face_recognition.face_locations(image, model="small")
            except Exception as exc:  # corrupt/unsupported files remain reviewable
                results.append({"path": str(path), "status": "error", "error": str(exc)})
                queue_store(str(path), "error")
                continue
        if not locations:
            results.append({"path": str(path), "status": "no_face"})
            queue_store(str(path), "no_face")
            continue
        # Multi-face images are indexed face-by-face.  A file may be assigned
        # when exactly one face clears the identity gates and every other face
        # is unresolved; the presence of an unrelated/unknown companion face
        # must not suppress a valid identity match.  If two or more faces
        # clear the gates, the file remains review-only because the identity
        # assignment is ambiguous.
        qualities = [face_quality(location, image.shape) for location in locations]
        usable = [
            index for index, quality in enumerate(qualities)
            if quality["min_dimension"] >= args.min_face_pixels
            and quality["area_ratio"] >= args.min_face_area_ratio
        ]
        if not usable:
            results.append({
                "path": str(path),
                "status": "low_quality",
                "face_count": len(locations),
                "quality": qualities,
            })
            queue_store(str(path), "low_quality")
            continue
        if not cache_valid:
            encodings = face_recognition.face_encodings(
                image,
                known_face_locations=[locations[index] for index in usable],
                num_jitters=max(1, args.num_jitters),
                model="small",
            )
        if not encodings:
            results.append({"path": str(path), "status": "encoding_failed"})
            queue_store(str(path), "encoding_failed")
            continue
        if len(encodings) != len(usable):
            # Never assign a multi-face image when one detected face could not
            # be encoded; an unknown face must remain available for review.
            results.append({
                "path": str(path),
                "status": "encoding_failed",
                "face_count": len(locations),
                "encoded_face_count": len(encodings),
                "quality": qualities,
            })
            queue_store(str(path), "encoding_failed")
            continue
        face_candidates = [rank_candidates(encoding, references, args.quality_weighted) for encoding in encodings]
        if args.adaptive_jitters and not cache_valid:
            for candidate_index, initial in enumerate(face_candidates):
                borderline = bool(
                    initial
                    and abs(float(initial[0]["distance"]) - args.threshold) <= max(0.06, args.margin * 2)
                )
                if not borderline:
                    continue
                refined = face_recognition.face_encodings(
                    image,
                    known_face_locations=[locations[usable[candidate_index]]],
                    num_jitters=max(3, args.num_jitters),
                    model="small",
                )
                if refined:
                    face_candidates[candidate_index] = rank_candidates(refined[0], references, args.quality_weighted)
        confident_matches = confident_face_matches(face_candidates, args.threshold, args.margin)
        confident = len(confident_matches) == 1
        selected_face_index = confident_matches[0][0] if confident else None
        candidates = face_candidates[selected_face_index] if selected_face_index is not None else (
            face_candidates[0] if face_candidates else []
        )
        best = candidates[0] if candidates else None
        candidates_by_location = {
            location_index: face_candidates[candidate_index]
            for candidate_index, location_index in enumerate(usable)
        }
        confident_by_location = {
            usable[candidate_index]
            for candidate_index, _ in confident_matches
        }
        per_face = []
        for location_index, quality in enumerate(qualities):
            if location_index in candidates_by_location:
                face_face_candidates = candidates_by_location[location_index]
                per_face.append({
                    "face_index": location_index,
                    "quality": quality,
                    "candidates": face_face_candidates[:5],
                    "confident": location_index in confident_by_location,
                    "evaluated": True,
                })
            else:
                per_face.append({
                    "face_index": location_index,
                    "quality": quality,
                    "candidates": [],
                    "confident": False,
                    "evaluated": False,
                })
        result = {
            "path": str(path),
            "status": "matched" if confident else "ambiguous",
            "model_id": "dlib",
            "model_version": "face_group_unmatched.v1",
            "threshold": args.threshold,
            "margin_threshold": args.margin,
            "face_count": len(locations),
            "multi_face": len(locations) > 1,
            "quality": qualities,
            "candidates": candidates[:5],
            "face_candidates": per_face,
            "confident_face_count": len(confident_matches),
            "matched_identity": str(best["person"]) if confident and best else None,
            "matched_face_index": usable[selected_face_index] if confident and selected_face_index is not None else None,
            "multi_face_policy": multi_face_assignment_policy(len(locations), len(confident_matches)),
        }
        results.append(result)
        if not cache_valid and len(encodings) == 1:
            queue_store(str(path), str(result["status"]), encodings[0])
        if confident:
            groups[str(best["person"])].append(result)
            cluster_matches[str(item.get("title") or path.stem)].append(str(best["person"]))
    payload = {
        "audit": str(args.audit),
        "database": str(args.db),
        "threshold": args.threshold,
        "margin": args.margin,
        "references": len(references),
        "unmatched_available": len(unmatched),
        "processed": len(selected),
        "offset": args.offset,
        "next_offset": args.offset + len(selected),
        "total_selected": total_selected,
        "matched": sum(1 for item in results if item["status"] == "matched"),
        "ambiguous": sum(1 for item in results if item["status"] == "ambiguous"),
        "no_face": sum(1 for item in results if item["status"] == "no_face"),
        "groups": {person: items for person, items in sorted(groups.items())},
        "cluster_consensus": {
            cluster: {
                "images": len(people),
                "identities": {person: people.count(person) for person in sorted(set(people))},
                "consensus": len(set(people)) == 1 and len(people) >= 3,
            }
            for cluster, people in sorted(cluster_matches.items())
        },
        "results": results,
        "cache_hits": cache_hits,
        "embedding_store_updates": len(store_updates),
        "source_fingerprints": {
            str(item.get("path")): audit_fingerprints[str(item.get("path"))]
            for item in results
            if isinstance(audit_fingerprints, dict) and str(item.get("path")) in audit_fingerprints
        },
    }
    elapsed_seconds = max(0.001, time.monotonic() - started_at)
    completed_rate = len(results) / elapsed_seconds
    finished_wall = datetime.now(timezone.utc)
    payload["timing"] = {
        "started_at": started_wall.isoformat(),
        "finished_at": finished_wall.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "rate_per_second": round(completed_rate, 4),
        "prior_estimate_seconds": round(prior_estimate, 3) if prior_estimate is not None else None,
        "timing_log": str(args.timing_log),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if store_updates:
        written = upsert_store_records(args.embedding_store, store_updates)
        print(f"embedding store: updated={written}", flush=True)
    if args.checkpoint:
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        args.checkpoint.write_text(
            json.dumps({
                "audit": str(args.audit),
                "next_offset": payload["next_offset"],
                "total_selected": total_selected,
                "output": str(args.output),
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    _append_timing_record(args.timing_log, {
        "event": "complete",
        "started_at": started_wall.isoformat(),
        "finished_at": finished_wall.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "processed": len(results),
        "selected": len(selected),
        "rate": round(completed_rate, 4),
        "signature": timing_signature,
        "output": str(args.output),
    })
    timing_state["finalized"] = True
    print(
        f"timing: elapsed={_format_duration(elapsed_seconds)} "
        f"rate={completed_rate:.2f}/s log={args.timing_log}",
        flush=True,
    )
    print(json.dumps({key: payload[key] for key in ("references", "unmatched_available", "processed", "matched", "ambiguous", "no_face")}, sort_keys=True))
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
