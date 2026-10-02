#!/usr/bin/env python3
"""Build review-only face-similarity clusters from unmatched audit results.

This tool never assigns identities, moves files, or edits the registry. It
extracts dlib/face_recognition embeddings, groups similar faces, and writes an
audit that can be opened by ``review_ui.py``. Multi-face and low-quality images
are deferred unless explicitly enabled.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import heapq
import json
import math
import multiprocessing
import os
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

DEFAULT_THRESHOLD = 0.48
DEFAULT_MIN_FACE_PIXELS = 80
DEFAULT_MIN_FACE_AREA_RATIO = 0.01
EMBEDDING_MODEL_ID = "dlib-face-recognition-small-v1"
FACE_QUALITY_VERSION = 1
DEFAULT_FACE_WORKERS = 1
MAX_FACE_WORKERS = 8


def _progress_interval() -> float:
    """Return the bounded extraction heartbeat interval from the environment."""
    try:
        return max(1.0, float(os.environ.get("PICORG_PROGRESS_SECONDS", "15")))
    except ValueError:
        return 15.0


def file_fingerprint(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def face_quality(location: tuple[int, int, int, int], shape: tuple[int, ...]) -> dict[str, float]:
    """Return geometry and a conservative 0..1 quality score for a face."""
    top, right, bottom, left = location
    height = max(0, bottom - top)
    width = max(0, right - left)
    image_area = max(1, int(shape[0]) * int(shape[1]))
    area_ratio = (width * height) / image_area
    size_score = min(1.0, min(width, height) / 256.0)
    area_score = min(1.0, math.sqrt(max(0.0, area_ratio) / 0.10))
    score = 0.6 * size_score + 0.4 * area_score
    return {
        "width": float(width),
        "height": float(height),
        "min_dimension": float(min(width, height)),
        "area_ratio": round(area_ratio, 6),
        "score": round(score, 6),
    }


def load_unmatched_paths(audit_path: Path, preflight_path: Path | None = None) -> List[Dict[str, Any]]:
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    items = [item for item in payload.get("results", []) if isinstance(item, dict) and not item.get("canonical") and item.get("path")]
    if not preflight_path or not preflight_path.is_file():
        return items
    try:
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        statuses = {str(row.get("path")): row.get("status") for row in preflight.get("records", []) if isinstance(row, dict)}
        return [item for item in items if statuses.get(str(item["path"]), "candidate") == "candidate"]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return items


def load_cached_embeddings(audit_path: Path, cache_path: Path, preflight_path: Path | None = None) -> Tuple[List[Tuple[str, List[float]]], Dict[str, Any]]:
    """Load candidate embeddings without stat/decoder work for cache-only reruns."""
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    cached = payload.get("records", {})
    if not isinstance(cached, dict):
        raise ValueError("embedding cache records must be an object")
    items = load_unmatched_paths(audit_path, preflight_path)
    stats: Dict[str, Any] = {"selected": len(items), "embedded": 0, "cached": 0, "missing": 0, "no_face": 0, "multi_face_deferred": 0, "low_quality": 0, "errors": 0, "error_categories": {}, "error_samples": [], "workers": 0}
    records: List[Tuple[str, List[float]]] = []
    for item in items:
        path = str(item["path"])
        entry = cached.get(path)
        if not isinstance(entry, dict):
            stats["missing"] += 1
            continue
        embedding = entry.get("embedding")
        if isinstance(embedding, list):
            records.append((path, [float(value) for value in embedding]))
            stats["embedded"] += 1
            stats["cached"] += 1
            continue
        status = str(entry.get("status") or "error")
        if status in {"no_face", "multi_face_deferred", "low_quality"}:
            stats[status] += 1
            stats["cached"] += 1
        else:
            stats["errors"] += 1
            stats["error_categories"][status] = stats["error_categories"].get(status, 0) + 1
    return records, stats


def load_preflight_summary(path: Path) -> Dict[str, int]:
    """Load only bounded coverage counts from an optional preflight report."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        counts = payload.get("counts", {})
        return {str(key): int(value) for key, value in counts.items() if isinstance(value, int)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def vector_distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity for unnormalised face embeddings."""
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return -1.0
    return dot / (left_norm * right_norm)


def load_rgb_image(path: Path):
    """Load an image as RGB without mutating the source file.

    Converting paletted PNGs in memory avoids Pillow's transparency warning and
    prevents a workflow cleanup pass from changing file fingerprints/cache keys.
    """
    from PIL import Image
    import numpy as np

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as source:
            source.verify()
        with Image.open(path) as source:
            return np.asarray(source.convert("RGB"))


def resolve_face_workers(value: int | None = None) -> int:
    """Resolve a conservative worker count for dlib extraction."""
    if value is None:
        raw = os.environ.get("PICORG_FACE_WORKERS", str(DEFAULT_FACE_WORKERS))
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("PICORG_FACE_WORKERS must be an integer") from exc
    if not 1 <= value <= MAX_FACE_WORKERS:
        raise ValueError(f"face workers must be between 1 and {MAX_FACE_WORKERS}")
    return value


def _extract_dlib_face(task: Tuple[str, str, int, float, bool, int, int]) -> Dict[str, Any]:
    """Extract one face in an isolated worker; the parent owns all cache writes."""
    path_text, fingerprint, min_face_pixels, min_face_area_ratio, allow_multi_face, num_jitters, upsample_times = task
    path = Path(path_text)
    try:
        # Prevent each process from creating its own BLAS/OpenMP thread pool.
        for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            # Workers are deliberately single-threaded; inheriting a large
            # parent value would otherwise multiply CPU/RAM use by worker count.
            os.environ[variable] = "1"
        import face_recognition  # type: ignore

        image = load_rgb_image(path)
        locations = face_recognition.face_locations(
            image,
            number_of_times_to_upsample=upsample_times,
            model="small",
        )
        if not locations:
            return {"path": path_text, "fingerprint": fingerprint, "status": "no_face"}
        if len(locations) != 1 and not allow_multi_face:
            return {"path": path_text, "fingerprint": fingerprint, "status": "multi_face_deferred"}
        location = max(locations, key=lambda box: (box[2] - box[0]) * (box[1] - box[3]))
        height, width = image.shape[:2]
        face_height = location[2] - location[0]
        face_width = location[1] - location[3]
        if min(face_height, face_width) < min_face_pixels or (face_height * face_width) / max(1, height * width) < min_face_area_ratio:
            return {"path": path_text, "fingerprint": fingerprint, "status": "low_quality"}
        quality = face_quality(location, image.shape)
        encodings = face_recognition.face_encodings(
            image,
            known_face_locations=[location],
            num_jitters=max(1, num_jitters),
            model="small",
        )
        if not encodings:
            return {"path": path_text, "fingerprint": fingerprint, "status": "no_embedding"}
        return {
            "path": path_text,
            "fingerprint": fingerprint,
            "status": "embedded",
            "embedding": [float(value) for value in encodings[0]],
            "face_quality": quality["score"],
            "face_box": [int(value) for value in location],
            "face_count": len(locations),
            "quality_version": FACE_QUALITY_VERSION,
        }
    except Exception as exc:  # worker errors are reported and classified by the parent
        terminal_decode_error = type(exc).__name__ in {"UnidentifiedImageError", "DecompressionBombError"}
        return {
            "path": path_text,
            "fingerprint": fingerprint,
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:240],
            "cache_error": not isinstance(exc, OSError) or terminal_decode_error,
        }


def cluster_embeddings(
    records: Iterable[Tuple[str, Sequence[float]]],
    threshold: float = DEFAULT_THRESHOLD,
    max_representatives: int = 5,
    similarity_threshold: float | None = None,
    strict_all_members: bool = False,
    strategy: str = "greedy",
) -> List[Dict[str, Any]]:
    """Cluster embeddings using bounded representative comparisons.

    Each cluster keeps several representatives, limiting memory and comparison
    cost while retaining pose/lighting variation. A candidate must be within
    threshold of every representative (complete-link matching), which prevents
    single-link chains from merging distinct people. ``strict_all_members``
    additionally compares a candidate with every existing member, eliminating
    representative-capacity bridge errors at the cost of more comparisons.
    This remains a candidate grouping pass, not an identity decision or
    calibrated verifier.
    """
    if max_representatives < 1:
        raise ValueError("max_representatives must be at least 1")
    if strategy not in {"greedy", "complete_link"}:
        raise ValueError("strategy must be greedy or complete_link")
    if similarity_threshold is not None and not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0 and 1")
    if strategy == "complete_link":
        if similarity_threshold is None:
            raise ValueError("complete_link requires a cosine similarity threshold")
        return _cluster_complete_link(records, similarity_threshold)
    clusters: List[Dict[str, Any]] = []
    member_vectors: List[List[List[float]]] = []
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - requirements-face supplies numpy
        np = None
    # Keep representative rows per cluster. Unused rows duplicate the
    # first representative, so the vectorized max is exactly equivalent to the
    # complete-link comparison below while avoiding millions of Python loops.
    representative_matrix = None
    representative_counts: List[int] = []
    representative_capacity = 0
    for path, embedding in records:
        best_index = -1
        best_distance = float("inf")
        best_similarity = -1.0
        if np is None:
            ranked = []
            for index, cluster in enumerate(clusters):
                comparisons = cluster["representatives"]
                if similarity_threshold is not None:
                    ranked.append((min(cosine_similarity(embedding, representative) for representative in comparisons), index))
                else:
                    ranked.append((max(vector_distance(embedding, representative) for representative in comparisons), index))
            if similarity_threshold is not None:
                for representative_score, index in sorted(ranked, reverse=True):
                    if representative_score < similarity_threshold:
                        break
                    comparisons = member_vectors[index] if strict_all_members else clusters[index]["representatives"]
                    similarity = min(cosine_similarity(embedding, representative) for representative in comparisons)
                    if similarity >= similarity_threshold:
                        best_index, best_similarity = index, similarity
                        break
            elif ranked:
                for _, index in sorted(ranked):
                    comparisons = member_vectors[index] if strict_all_members else clusters[index]["representatives"]
                    distance = max(vector_distance(embedding, representative) for representative in comparisons)
                    if distance <= threshold:
                        best_index, best_distance = index, distance
                        break
        else:
            vector = np.asarray(embedding, dtype=np.float32)
            if vector.ndim != 1:
                raise ValueError("face embedding must be a one-dimensional vector")
            if representative_matrix is None:
                dimension = int(vector.shape[0])
                representative_capacity = 16
                representative_matrix = np.empty((representative_capacity, max_representatives, dimension), dtype=np.float32)
            elif vector.shape[0] != representative_matrix.shape[2]:
                raise ValueError("face embeddings must have consistent dimensions")
            if similarity_threshold is not None:
                norm = float(np.linalg.norm(vector))
                if norm <= 0.0:
                    continue
                vector = vector / norm
                if clusters:
                    if strict_all_members:
                        representative_scores = (representative_matrix[: len(clusters)] @ vector).min(axis=1)
                        for candidate in np.argsort(representative_scores)[::-1]:
                            candidate = int(candidate)
                            if float(representative_scores[candidate]) < similarity_threshold:
                                break
                            members = member_vectors[candidate]
                            exact_similarity = min(
                                float(np.dot(vector, np.asarray(member, dtype=np.float32)))
                                for member in members
                            )
                            if exact_similarity >= similarity_threshold:
                                best_index = candidate
                                best_similarity = exact_similarity
                                break
                    else:
                        similarities = (representative_matrix[: len(clusters)] @ vector).min(axis=1)
                        best_index = int(np.argmax(similarities))
                        best_similarity = float(similarities[best_index])
                else:
                    best_index = -1
                    best_similarity = -1.0
            else:
                if clusters:
                    if strict_all_members:
                        representative_distances = np.linalg.norm(representative_matrix[: len(clusters)] - vector, axis=2).max(axis=1)
                        for candidate in np.argsort(representative_distances):
                            candidate = int(candidate)
                            if float(representative_distances[candidate]) > threshold:
                                break
                            members = member_vectors[candidate]
                            exact_distance = max(
                                float(np.linalg.norm(np.asarray(member, dtype=np.float32) - vector))
                                for member in members
                            )
                            if exact_distance <= threshold:
                                best_index = candidate
                                best_distance = exact_distance
                                break
                    else:
                        distances = np.linalg.norm(representative_matrix[: len(clusters)] - vector, axis=2).max(axis=1)
                        best_index = int(np.argmin(distances))
                        best_distance = float(distances[best_index])
                else:
                    best_index = -1
                    best_distance = float("inf")
        accepted = (
            best_index >= 0
            and ((similarity_threshold is not None and best_similarity >= similarity_threshold)
                 or (similarity_threshold is None and best_distance <= threshold))
        )
        if accepted:
            cluster = clusters[best_index]
            cluster["paths"].append(path)
            if similarity_threshold is not None:
                cluster["min_similarity"] = min(float(cluster.get("min_similarity", 1.0)), best_similarity)
                cluster.setdefault("member_link_scores", {})[path] = best_similarity
            if len(cluster["representatives"]) < max_representatives:
                cluster["representatives"].append(list(vector if np is not None else embedding))
                if np is not None:
                    representative_matrix[best_index, representative_counts[best_index]] = vector
                    representative_counts[best_index] += 1
            if strict_all_members:
                member_vectors[best_index].append(list(vector if np is not None else embedding))
        else:
            if np is not None and len(clusters) >= representative_capacity:
                new_capacity = max(16, representative_capacity * 2)
                expanded = np.empty((new_capacity, max_representatives, representative_matrix.shape[2]), dtype=np.float32)
                if clusters:
                    expanded[: len(clusters)] = representative_matrix[: len(clusters)]
                representative_matrix = expanded
                representative_capacity = new_capacity
            clusters.append({"paths": [path], "representatives": [list(vector if np is not None else embedding)], **({"min_similarity": 1.0, "member_link_scores": {}} if similarity_threshold is not None else {})})
            member_vectors.append([list(vector if np is not None else embedding)] if strict_all_members else [])
            if np is not None:
                representative_matrix[len(clusters) - 1] = vector
                representative_counts.append(1)
    output: List[Dict[str, Any]] = []
    for index, cluster in enumerate(sorted(clusters, key=lambda item: (-len(item["paths"]), item["paths"][0])), start=1):
        paths = sorted(cluster["paths"])
        key = hashlib.sha256("|".join(paths).encode("utf-8")).hexdigest()[:16]
        output.append(
            {
                "cluster_id": f"face-{key}",
                "cluster_label": f"fbunknown{index:03d}",
                "paths": paths,
                "count": len(paths),
                "sample_paths": paths[:12],
                **({"min_similarity": round(float(cluster["min_similarity"]), 6)} if similarity_threshold is not None else {}),
                **({"member_link_scores": {path: round(float(score), 6) for path, score in cluster.get("member_link_scores", {}).items()}} if similarity_threshold is not None else {}),
            }
        )
    return output


def _cluster_complete_link(
    records: Iterable[Tuple[str, Sequence[float]]], similarity_threshold: float
) -> List[Dict[str, Any]]:
    """Deterministic exact complete-link HAC, bounded to review-sized batches."""
    import numpy as np

    rows = sorted(((str(path), np.asarray(vector, dtype=np.float32)) for path, vector in records), key=lambda row: row[0])
    if len(rows) > 1200:
        raise ValueError("complete_link is limited to 1200 embeddings; use greedy for larger production runs")
    if not rows:
        return []
    dimension = rows[0][1].shape
    if len(dimension) != 1 or any(vector.shape != dimension for _, vector in rows):
        raise ValueError("face embeddings must be one-dimensional and have consistent dimensions")
    norms = np.asarray([np.linalg.norm(vector) for _, vector in rows], dtype=np.float32)
    valid = [index for index, norm in enumerate(norms) if norm > 0]
    if not valid:
        return []
    vectors = np.stack([rows[index][1] / norms[index] for index in valid])
    paths = [rows[index][0] for index in valid]
    floor_distance = 1.0 - similarity_threshold
    distances: dict[tuple[int, int], float] = {}
    heap: list[tuple[float, int, int]] = []
    for left in range(len(paths)):
        similarities = vectors[left + 1:] @ vectors[left]
        for offset in np.flatnonzero((1.0 - similarities) <= floor_distance):
            right = left + 1 + int(offset)
            distance = float(1.0 - similarities[int(offset)])
            key = (left, right)
            distances[key] = distance
            heapq.heappush(heap, (distance, left, right))
    active: dict[int, list[int]] = {index: [index] for index in range(len(paths))}
    next_id = len(paths)
    while heap:
        distance, left, right = heapq.heappop(heap)
        if left not in active or right not in active or distances.get((left, right)) != distance:
            continue
        merged = active.pop(left) + active.pop(right)
        others = list(active)
        merged_distances = []
        for other in others:
            left_distance = distances.get((min(left, other), max(left, other)))
            right_distance = distances.get((min(right, other), max(right, other)))
            if left_distance is None or right_distance is None:
                continue
            candidate = max(left_distance, right_distance)
            if candidate <= floor_distance:
                merged_distances.append((candidate, other))
        active[next_id] = merged
        for candidate, other in merged_distances:
            key = (min(next_id, other), max(next_id, other))
            distances[key] = candidate
            heapq.heappush(heap, (candidate, *key))
        next_id += 1
    groups = []
    for members in active.values():
        member_paths = sorted(paths[index] for index in members)
        key = hashlib.sha256("|".join(member_paths).encode("utf-8")).hexdigest()[:16]
        groups.append({"cluster_id": f"face-{key}", "paths": member_paths, "count": len(member_paths), "sample_paths": member_paths[:12]})
    groups.sort(key=lambda item: (-item["count"], item["paths"][0]))
    for index, group in enumerate(groups, start=1):
        group["cluster_label"] = f"fbunknown{index:03d}"
    return groups


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
    preflight_path: Path | None = None,
    upsample_times: int = 1,
    workers: int | None = None,
) -> Tuple[List[Tuple[str, List[float]]], Dict[str, Any]]:
    """Extract one usable face embedding per unmatched image.

    The optional dependency is imported lazily so the rest of picorg remains
    usable without face-matching packages installed.
    """
    records: List[Tuple[str, List[float]]] = []
    worker_count = resolve_face_workers(workers)
    stats: Dict[str, Any] = {"selected": 0, "embedded": 0, "cached": 0, "missing": 0, "no_face": 0, "multi_face_deferred": 0, "low_quality": 0, "errors": 0, "error_categories": {}, "error_samples": [], "workers": worker_count}
    items = load_unmatched_paths(audit_path, preflight_path)
    total = min(len(items), max_images) if max_images else len(items)
    cached_records: Dict[str, Any] = {}
    extraction_config = {
        "upsample_times": upsample_times,
        "num_jitters": num_jitters,
        "quality_version": FACE_QUALITY_VERSION,
    }
    if cache_path and cache_path.is_file():
        try:
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cache_config = cached_payload.get("extraction_config")
            config_compatible = cache_config == extraction_config or (
                cache_config is None and upsample_times == 1 and num_jitters == 1
            )
            if cached_payload.get("model_id") in (None, EMBEDDING_MODEL_ID) and config_compatible:
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
    progress_seconds = _progress_interval()
    last_progress = started
    print(f"face extraction: starting {total} unmatched images, cached={len(cached_records)}", flush=True)

    def record_error(path: Path, exc: Exception, category: str | None = None, message: str | None = None) -> None:
        category = category or type(exc).__name__
        stats["errors"] += 1
        stats["error_categories"][category] = stats["error_categories"].get(category, 0) + 1
        if len(stats["error_samples"]) < 20:
            stats["error_samples"].append({"path": str(path), "type": category, "message": (message or str(exc))[:240]})

    def checkpoint() -> None:
        nonlocal last_checkpoint
        if not cache_path:
            return
        audit_stat = audit_path.stat()
        _atomic_write(cache_path, {"schema_version": 2, "model_id": EMBEDDING_MODEL_ID, "detector": "small", "extraction_config": extraction_config, "audit": {"mtime_ns": audit_stat.st_mtime_ns, "size": audit_stat.st_size}, "records": cached_records, "progress": stats})
        last_checkpoint = time.monotonic()
        print(f"face extraction: checkpoint saved to {cache_path}", flush=True)

    def report_progress() -> None:
        nonlocal last_progress
        now = time.monotonic()
        if stats["selected"] % 100 != 0 and now - last_progress < progress_seconds:
            return
        elapsed = max(0.001, now - started)
        rate = stats["selected"] / elapsed
        print(f"face extraction: {stats['selected']}/{total} selected, embedded={stats['embedded']}, cached={stats['cached']}, no_face={stats['no_face']}, workers={worker_count}, rate={rate:.1f}/s", flush=True)
        last_progress = now

    def consume_cached(path: Path, cached: Dict[str, Any]) -> None:
        if isinstance(cached.get("embedding"), list):
            records.append((str(path), [float(value) for value in cached["embedding"]]))
            stats["cached"] += 1
            stats["embedded"] += 1
            return
        status = cached.get("status")
        if status in {"no_face", "multi_face_deferred", "low_quality"}:
            stats["cached"] += 1
            stats[status] += 1
            return
        if status == "error" and cached.get("error_type"):
            stats["cached"] += 1
            stats["errors"] += 1
            category = str(cached["error_type"])
            stats["error_categories"][category] = stats["error_categories"].get(category, 0) + 1

    def consume_result(result: Dict[str, Any]) -> None:
        path = Path(str(result["path"]))
        status = result.get("status")
        if status == "embedded":
            vector = result.get("embedding") or []
            records.append((str(path), [float(value) for value in vector]))
            cached_records[str(path)] = {"fingerprint": result["fingerprint"], "embedding": vector}
            stats["embedded"] += 1
        elif status in {"no_face", "multi_face_deferred", "low_quality"}:
            stats[status] += 1
            cached_records[str(path)] = {"fingerprint": result["fingerprint"], "status": status}
        elif status == "error":
            category = str(result.get("error_type") or "RuntimeError")
            record_error(path, RuntimeError(str(result.get("error_message") or category)), category, str(result.get("error_message") or category))
            if result.get("cache_error"):
                cached_records[str(path)] = {"fingerprint": result["fingerprint"], "status": "error", "error_type": category}
        stats["selected"] += 1
        report_progress()
        if cache_path and (stats["selected"] % max(1, checkpoint_every) == 0 or time.monotonic() - last_checkpoint >= max(1.0, checkpoint_seconds)):
            checkpoint()

    pending: List[Tuple[str, str, int, float, bool, int, int]] = []
    for item in items[:total]:
        path = Path(str(item["path"]))
        try:
            fingerprint = file_fingerprint(path)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                stats["missing"] += 1
                stats["selected"] += 1
                report_progress()
                continue
            record_error(path, exc)
            stats["selected"] += 1
            report_progress()
            continue
        cached = cached_records.get(str(path))
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            consume_cached(path, cached)
            stats["selected"] += 1
            report_progress()
            continue
        pending.append((str(path), fingerprint, min_face_pixels, min_face_area_ratio, allow_multi_face, num_jitters, upsample_times))

    if pending:
        try:
            import face_recognition  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("face clustering requires face_recognition and numpy; install requirements-face.txt") from exc
        if worker_count == 1:
            result_iterator = map(_extract_dlib_face, pending)
            for result in result_iterator:
                consume_result(result)
        else:
            context = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count, mp_context=context) as executor:
                for result in executor.map(_extract_dlib_face, pending, chunksize=1):
                    consume_result(result)
    if cache_path:
        checkpoint()
    report_progress()
    return records, stats


def extract_embeddings_insightface(
    audit_path: Path,
    max_images: int = 0,
    cache_path: Path | None = None,
    checkpoint_every: int = 500,
    preflight_path: Path | None = None,
) -> Tuple[List[Tuple[str, List[float]]], Dict[str, Any]]:
    """Extract embeddings with the optional InsightFace backend."""
    from insightface_backend import InsightFaceBackend, MODEL_ID

    items = load_unmatched_paths(audit_path, preflight_path)
    total = min(len(items), max_images) if max_images else len(items)
    records: List[Tuple[str, List[float]]] = []
    stats: Dict[str, Any] = {"selected": 0, "embedded": 0, "cached": 0, "missing": 0, "no_face": 0, "multi_face_deferred": 0, "low_quality": 0, "errors": 0, "error_categories": {}, "error_samples": []}
    cached_records: Dict[str, Any] = {}

    # InsightFace runs can take hours on a large reference gallery. Reuse a
    # compatible checkpoint when present so interruption or a transient disk
    # failure resumes without discarding completed embeddings.
    if cache_path and cache_path.is_file():
        try:
            existing = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("records"), dict):
                cached_records = dict(existing["records"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cached_records = {}
    if cache_path and cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("model_id") == MODEL_ID:
                cached_records = payload.get("records", {})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    backend = InsightFaceBackend()
    started = time.monotonic()
    progress_seconds = _progress_interval()
    last_progress = started
    print(f"face extraction: starting {total} unmatched images, cached={len(cached_records)}, backend=insightface", flush=True)
    for item in items[:total]:
        stats["selected"] += 1
        path = Path(str(item["path"]))
        try:
            fingerprint = file_fingerprint(path)
            cached = cached_records.get(str(path), {})
            if cached.get("fingerprint") == fingerprint:
                if isinstance(cached.get("embedding"), list):
                    records.append((str(path), cached["embedding"])); stats["embedded"] += 1; stats["cached"] += 1; continue
                if cached.get("status") in {"no_face", "multi_face_deferred"}:
                    stats[cached["status"]] += 1; stats["cached"] += 1; continue
            vector, metadata = backend.embed(path)
            status = metadata.get("status")
            if vector:
                records.append((str(path), vector)); stats["embedded"] += 1
                cached_records[str(path)] = {"fingerprint": fingerprint, "embedding": vector}
            else:
                stats[status] = stats.get(status, 0) + 1
                cached_records[str(path)] = {"fingerprint": fingerprint, "status": status}
        except FileNotFoundError:
            stats["missing"] += 1
        except Exception as exc:
            stats["errors"] += 1
            category = type(exc).__name__
            stats["error_categories"][category] = stats["error_categories"].get(category, 0) + 1
            if len(stats["error_samples"]) < 20:
                stats["error_samples"].append({"path": str(path), "type": category, "message": str(exc)[:240]})
        now = time.monotonic()
        if stats["selected"] % 100 == 0 or stats["selected"] == total or now - last_progress >= progress_seconds:
            elapsed = max(0.001, now - started)
            rate = stats["selected"] / elapsed
            print(
                f"face extraction: {stats['selected']}/{total} selected, embedded={stats['embedded']}, "
                f"cached={stats['cached']}, no_face={stats['no_face']}, workers=1, rate={rate:.1f}/s",
                flush=True,
            )
            last_progress = now
        if cache_path and stats["selected"] % max(1, checkpoint_every) == 0:
            _atomic_write(cache_path, {"schema_version": 2, "model_id": MODEL_ID, "detector": "scrfd", "records": cached_records, "progress": stats})
    if cache_path:
        _atomic_write(cache_path, {"schema_version": 2, "model_id": MODEL_ID, "detector": "scrfd", "records": cached_records, "progress": stats})
    return records, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, help="optional media preflight JSON; defaults to audit sibling")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="optional cosine-similarity floor (0..1) for same-cluster membership; use with --threshold only for legacy comparisons",
    )
    parser.add_argument(
        "--max-representatives",
        type=int,
        default=5,
        help="maximum representative faces retained per cluster (higher is stricter)",
    )
    parser.add_argument(
        "--strict-all-members",
        action="store_true",
        help="compare each candidate with every existing cluster member; slower but prevents representative bridge merges",
    )
    parser.add_argument(
        "--strategy",
        choices=("greedy", "complete_link"),
        default="greedy",
        help="complete_link is deterministic and exact but limited to 1200 cached embeddings",
    )
    parser.add_argument("--backend", choices=("dlib", "insightface"), default="dlib")
    parser.add_argument("--max-images", type=int, default=0, help="0 means all unmatched images")
    parser.add_argument("--min-face-pixels", type=int, default=DEFAULT_MIN_FACE_PIXELS)
    parser.add_argument("--min-face-area-ratio", type=float, default=DEFAULT_MIN_FACE_AREA_RATIO)
    parser.add_argument("--allow-multi-face", action="store_true")
    parser.add_argument("--num-jitters", type=int, default=1)
    parser.add_argument("--cache", type=Path, help="embedding cache for safe resume")
    parser.add_argument("--cache-only", action="store_true", help="skip extraction and cluster embeddings already present in --cache")
    parser.add_argument("--checkpoint-every", type=int, default=500)
    parser.add_argument("--checkpoint-seconds", type=float, default=300.0)
    parser.add_argument("--upsample-times", type=int, default=1, help="dlib detector upsampling; 0 is faster, 1 may find smaller faces")
    parser.add_argument("--workers", type=int, default=None, help=f"bounded dlib extraction workers (default: $PICORG_FACE_WORKERS or {DEFAULT_FACE_WORKERS}, max {MAX_FACE_WORKERS})")
    args = parser.parse_args()
    try:
        workers = resolve_face_workers(args.workers)
    except ValueError as exc:
        parser.error(str(exc))
    if args.backend == "insightface" and workers != 1:
        print("face extraction: InsightFace backend remains single-process; ignoring --workers", flush=True)
        workers = 1
    preflight_path = args.preflight or args.audit.with_name(f"{args.audit.stem}.preflight.json")
    preflight_counts = load_preflight_summary(preflight_path)
    cache_path = args.cache or args.output.with_suffix(".embeddings.json")
    if args.cache_only:
        if not cache_path.is_file():
            parser.error("--cache-only requires an existing --cache file")
        records, stats = load_cached_embeddings(args.audit, cache_path, preflight_path)
        model_id = "insightface-buffalo_l-scrfd-arcface" if args.backend == "insightface" else EMBEDDING_MODEL_ID
    elif args.backend == "insightface":
        records, stats = extract_embeddings_insightface(args.audit, args.max_images, cache_path, args.checkpoint_every, preflight_path)
        stats["workers"] = 1
        model_id = "insightface-buffalo_l-scrfd-arcface"
    else:
        records, stats = extract_embeddings(args.audit, args.max_images, args.min_face_pixels, args.min_face_area_ratio, args.allow_multi_face, args.num_jitters, cache_path, args.checkpoint_every, args.checkpoint_seconds, preflight_path, args.upsample_times, workers)
        model_id = EMBEDDING_MODEL_ID
    try:
        clusters = cluster_embeddings(records, args.threshold, args.max_representatives, args.min_similarity, args.strict_all_members, args.strategy)
    except ValueError as exc:
        parser.error(str(exc))
    results = [
        {"path": path, "title": cluster["cluster_label"], "canonical": None, "source_root": str(Path(path).parent), "face_cluster_id": cluster["cluster_id"], "cluster_label": cluster["cluster_label"], **({"face_link_similarity": cluster["member_link_scores"][path]} if path in cluster.get("member_link_scores", {}) else {})}
        for cluster in clusters
        for path in cluster["paths"]
    ]
    report = {
        **stats,
        "clusters": len(clusters),
        "max_representatives": args.max_representatives,
        "clustering_strategy": args.strategy,
    }
    if preflight_counts:
        report["preflight"] = {"path": str(preflight_path), "counts": preflight_counts}
        report["preflight_candidates"] = preflight_counts.get("candidate", 0)
    payload = {
        "schema_version": 2,
        "source": "face_embedding_cluster",
        "model_id": model_id,
        "detector": "scrfd" if args.backend == "insightface" else "small",
        "upsample_times": args.upsample_times if args.backend == "dlib" else None,
        "num_jitters": args.num_jitters if args.backend == "dlib" else None,
        "threshold": args.threshold,
        "similarity_metric": "cosine" if args.min_similarity is not None else None,
        "min_similarity": args.min_similarity,
        "strict_all_members": args.strict_all_members,
        "clustering_strategy": args.strategy,
        "report": report,
        "results": results,
    }
    _atomic_write(args.output, payload)
    print(json.dumps({**report, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
