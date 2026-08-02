#!/usr/bin/env python3
"""Build review-only face-similarity clusters from unmatched audit results.

This tool never assigns identities, moves files, or edits the registry. It
extracts dlib/face_recognition embeddings, groups similar faces, and writes an
audit that can be opened by ``review_ui.py``. Multi-face and low-quality images
are deferred unless explicitly enabled.
"""

from __future__ import annotations

import argparse
import hashlib
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

DEFAULT_THRESHOLD = 0.48
DEFAULT_MIN_FACE_PIXELS = 80
DEFAULT_MIN_FACE_AREA_RATIO = 0.01
EMBEDDING_MODEL_ID = "dlib-face-recognition-small-v1"


def file_fingerprint(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_unmatched_paths(audit_path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    return [item for item in payload.get("results", []) if isinstance(item, dict) and not item.get("canonical") and item.get("path")]


def vector_distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def load_rgb_image(path: Path):
    """Load an image as RGB without mutating the source file.

    Converting paletted PNGs in memory avoids Pillow's transparency warning and
    prevents a workflow cleanup pass from changing file fingerprints/cache keys.
    """
    from PIL import Image
    import numpy as np

    with Image.open(path) as source:
        return np.asarray(source.convert("RGB"))


def cluster_embeddings(records: Iterable[Tuple[str, Sequence[float]]], threshold: float = DEFAULT_THRESHOLD) -> List[Dict[str, Any]]:
    """Cluster embeddings using bounded representative comparisons.

    Each cluster keeps up to three representatives, limiting memory and
    comparison cost while retaining pose/lighting variation. This is a
    candidate grouping pass, not an identity decision or a calibrated verifier.
    """
    clusters: List[Dict[str, Any]] = []
    for path, embedding in records:
        best_index = -1
        best_distance = float("inf")
        for index, cluster in enumerate(clusters):
            distance = min(vector_distance(embedding, representative) for representative in cluster["representatives"])
            if distance < best_distance:
                best_index, best_distance = index, distance
        if best_index >= 0 and best_distance <= threshold:
            cluster = clusters[best_index]
            cluster["paths"].append(path)
            if len(cluster["representatives"]) < 3:
                cluster["representatives"].append(list(embedding))
        else:
            clusters.append({"paths": [path], "representatives": [list(embedding)]})
    output: List[Dict[str, Any]] = []
    for cluster in sorted(clusters, key=lambda item: (-len(item["paths"]), item["paths"][0])):
        paths = sorted(cluster["paths"])
        key = hashlib.sha256("|".join(paths).encode("utf-8")).hexdigest()[:16]
        output.append({"cluster_id": f"face-{key}", "paths": paths, "count": len(paths), "sample_paths": paths[:12]})
    return output


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def extract_embeddings(
    audit_path: Path,
    max_images: int = 0,
    min_face_pixels: int = DEFAULT_MIN_FACE_PIXELS,
    min_face_area_ratio: float = DEFAULT_MIN_FACE_AREA_RATIO,
    allow_multi_face: bool = False,
    num_jitters: int = 1,
    cache_path: Path | None = None,
    checkpoint_every: int = 500,
    checkpoint_seconds: float = 300.0,
) -> Tuple[List[Tuple[str, List[float]]], Dict[str, Any]]:
    """Extract one usable face embedding per unmatched image.

    The optional dependency is imported lazily so the rest of picorg remains
    usable without face-matching packages installed.
    """
    try:
        import face_recognition  # type: ignore
    except ImportError as exc:
        raise RuntimeError("face clustering requires face_recognition and numpy; install requirements-face.txt") from exc

    records: List[Tuple[str, List[float]]] = []
    stats: Dict[str, Any] = {"selected": 0, "embedded": 0, "cached": 0, "no_face": 0, "multi_face_deferred": 0, "low_quality": 0, "errors": 0, "error_categories": {}, "error_samples": []}
    items = load_unmatched_paths(audit_path)
    total = min(len(items), max_images) if max_images else len(items)
    cached_records: Dict[str, Any] = {}
    if cache_path and cache_path.is_file():
        try:
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached_payload.get("model_id") in (None, EMBEDDING_MODEL_ID):
                cached_records = cached_payload.get("records", {})
                if cached_payload.get("model_id") is None:
                    print("face extraction: accepting legacy cache and upgrading metadata", flush=True)
            else:
                cached_records = {}
                print("face extraction: ignored cache with incompatible model_id", flush=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cached_records = {}
    started = time.monotonic()
    last_checkpoint = started
    print(f"face extraction: starting {total} unmatched images, cached={len(cached_records)}", flush=True)

    def record_error(path: Path, exc: Exception) -> None:
        category = type(exc).__name__
        stats["errors"] += 1
        stats["error_categories"][category] = stats["error_categories"].get(category, 0) + 1
        if len(stats["error_samples"]) < 20:
            stats["error_samples"].append({"path": str(path), "type": category, "message": str(exc)[:240]})

    def checkpoint() -> None:
        nonlocal last_checkpoint
        if not cache_path:
            return
        audit_stat = audit_path.stat()
        _atomic_write(cache_path, {"schema_version": 2, "model_id": EMBEDDING_MODEL_ID, "detector": "small", "audit": {"mtime_ns": audit_stat.st_mtime_ns, "size": audit_stat.st_size}, "records": cached_records, "progress": stats})
        last_checkpoint = time.monotonic()
        print(f"face extraction: checkpoint saved to {cache_path}", flush=True)
    for item in items:
        if max_images and stats["selected"] >= max_images:
            break
        stats["selected"] += 1
        path = Path(str(item["path"]))
        try:
            fingerprint = file_fingerprint(path)
            cached = cached_records.get(str(path))
            if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
                if isinstance(cached.get("embedding"), list):
                    records.append((str(path), [float(value) for value in cached["embedding"]]))
                    stats["cached"] += 1
                    stats["embedded"] += 1
                    continue
                status = cached.get("status")
                if status in {"no_face", "multi_face_deferred", "low_quality"}:
                    stats["cached"] += 1
                    stats[status] += 1
                    continue
                if status == "error" and cached.get("error_type"):
                    stats["cached"] += 1
                    stats["errors"] += 1
                    category = str(cached["error_type"])
                    stats["error_categories"][category] = stats["error_categories"].get(category, 0) + 1
                    continue
        except OSError as exc:
            record_error(path, exc)
            continue
        if stats["selected"] % 100 == 0:
            elapsed = max(0.001, time.monotonic() - started)
            rate = stats["selected"] / elapsed
            print(f"face extraction: {stats['selected']}/{total} selected, embedded={stats['embedded']}, no_face={stats['no_face']}, rate={rate:.1f}/s", flush=True)
        try:
            image = load_rgb_image(path)
            locations = face_recognition.face_locations(image, model="small")
            if not locations:
                stats["no_face"] += 1
                cached_records[str(path)] = {"fingerprint": fingerprint, "status": "no_face"}
                continue
            if len(locations) != 1 and not allow_multi_face:
                stats["multi_face_deferred"] += 1
                cached_records[str(path)] = {"fingerprint": fingerprint, "status": "multi_face_deferred"}
                continue
            location = max(locations, key=lambda box: (box[2] - box[0]) * (box[1] - box[3]))
            height, width = image.shape[:2]
            face_height, face_width = location[2] - location[0], location[1] - location[3]
            if min(face_height, face_width) < min_face_pixels or (face_height * face_width) / max(1, height * width) < min_face_area_ratio:
                stats["low_quality"] += 1
                cached_records[str(path)] = {"fingerprint": fingerprint, "status": "low_quality"}
                continue
            encodings = face_recognition.face_encodings(image, known_face_locations=[location], num_jitters=max(1, num_jitters), model="small")
            if encodings:
                vector = [float(value) for value in encodings[0]]
                records.append((str(path), vector))
                cached_records[str(path)] = {"fingerprint": fingerprint, "embedding": vector}
                stats["embedded"] += 1
        except Exception as exc:
            record_error(path, exc)
            if not isinstance(exc, OSError):
                cached_records[str(path)] = {"fingerprint": fingerprint, "status": "error", "error_type": type(exc).__name__}
        if cache_path and (stats["selected"] % max(1, checkpoint_every) == 0 or time.monotonic() - last_checkpoint >= max(1.0, checkpoint_seconds)):
            checkpoint()
    if cache_path:
        checkpoint()
    return records, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--max-images", type=int, default=0, help="0 means all unmatched images")
    parser.add_argument("--min-face-pixels", type=int, default=DEFAULT_MIN_FACE_PIXELS)
    parser.add_argument("--min-face-area-ratio", type=float, default=DEFAULT_MIN_FACE_AREA_RATIO)
    parser.add_argument("--allow-multi-face", action="store_true")
    parser.add_argument("--num-jitters", type=int, default=1)
    parser.add_argument("--cache", type=Path, help="embedding cache for safe resume")
    parser.add_argument("--checkpoint-every", type=int, default=500)
    parser.add_argument("--checkpoint-seconds", type=float, default=300.0)
    args = parser.parse_args()
    cache_path = args.cache or args.output.with_suffix(".embeddings.json")
    records, stats = extract_embeddings(args.audit, args.max_images, args.min_face_pixels, args.min_face_area_ratio, args.allow_multi_face, args.num_jitters, cache_path, args.checkpoint_every, args.checkpoint_seconds)
    clusters = cluster_embeddings(records, args.threshold)
    results = [
        {"path": path, "title": cluster["cluster_id"], "canonical": None, "source_root": str(Path(path).parent), "face_cluster_id": cluster["cluster_id"]}
        for cluster in clusters
        for path in cluster["paths"]
    ]
    payload = {"schema_version": 2, "source": "face_embedding_cluster", "model_id": EMBEDDING_MODEL_ID, "detector": "small", "threshold": args.threshold, "report": {**stats, "clusters": len(clusters)}, "results": results}
    _atomic_write(args.output, payload)
    print(json.dumps({**stats, "clusters": len(clusters), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
