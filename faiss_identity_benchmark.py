#!/usr/bin/env python3
"""Benchmark exact FAISS identity ranking against PicOrg's current path.

The comparison is read-only and uses the face database directly.  FAISS
searches every reference with ``IndexFlatL2`` before reducing results to the
best distance per identity, preserving exact top-k identity semantics.  It
does not change production matching or acceptance thresholds.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np


def load_database(path: Path) -> tuple[list[str], np.ndarray, dict[str, list[int]]]:
    names: list[str] = []
    vectors: list[np.ndarray] = []
    by_identity: dict[str, list[int]] = defaultdict(list)
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT person_name, encoding FROM face_encodings")
        for person_name, blob in rows:
            vector = np.frombuffer(blob, dtype=np.float64)
            if vector.shape != (128,):
                continue
            index = len(vectors)
            name = str(person_name)
            names.append(name)
            vectors.append(vector.astype(np.float32))
            by_identity[name].append(index)
    if not vectors:
        raise ValueError("database contains no usable 128-value encodings")
    return names, np.asarray(vectors, dtype=np.float32), dict(by_identity)


def current_rank(
    query: np.ndarray, vectors: np.ndarray, by_identity: dict[str, list[int]], k: int
) -> list[str]:
    best: dict[str, float] = {}
    for identity, indices in by_identity.items():
        distance = min(float(np.linalg.norm(vectors[index] - query)) for index in indices)
        best[identity] = distance
    return [identity for identity, _ in sorted(best.items(), key=lambda item: (item[1], item[0]))[:k]]


def faiss_rank(
    index: Any, names: list[str], distances: np.ndarray, indices: np.ndarray, k: int
) -> list[list[str]]:
    del index  # retained in the signature to make the exact-index step explicit
    results: list[list[str]] = []
    for row_distances, row_indices in zip(distances, indices):
        best: dict[str, float] = {}
        for squared_distance, vector_index in zip(row_distances, row_indices):
            identity = names[int(vector_index)]
            distance = float(max(0.0, squared_distance) ** 0.5)
            if identity not in best or distance < best[identity]:
                best[identity] = distance
        results.append([identity for identity, _ in sorted(best.items(), key=lambda item: (item[1], item[0]))[:k]])
    return results


def run_benchmark(database: Path, queries: int, k: int, candidate_k: int = 0) -> dict[str, Any]:
    load_started = time.perf_counter()
    names, vectors, by_identity = load_database(database)
    load_seconds = time.perf_counter() - load_started
    query_count = min(max(1, queries), len(vectors))
    k = min(max(1, k), len(by_identity))
    # Spread queries across the gallery rather than sampling one identity's
    # contiguous insertion block; this makes bounded-k parity less order-biased.
    query_indices = np.linspace(0, len(vectors) - 1, query_count, dtype=np.int64)
    query_vectors = vectors[query_indices]

    started = time.perf_counter()
    current_results = [current_rank(query, vectors, by_identity, k) for query in query_vectors]
    current_seconds = time.perf_counter() - started

    try:
        import faiss  # type: ignore[import-not-found]
    except ImportError as exc:
        message = "install the optional retrieval extra: uv sync --extra retrieval"
        raise RuntimeError(message) from exc

    started = time.perf_counter()
    index = faiss.IndexFlatL2(vectors.shape[1])
    index.add(vectors)
    index_build_seconds = time.perf_counter() - started
    started = time.perf_counter()
    # Search the complete gallery so reducing vectors to identities is exact;
    # a production top-k shortcut requires a separate parity evaluation.
    search_k = len(vectors) if candidate_k <= 0 else min(max(candidate_k, k), len(vectors))
    distances, indices = index.search(query_vectors, search_k)
    faiss_results = faiss_rank(index, names, distances, indices, k)
    faiss_seconds = time.perf_counter() - started
    identical = sum(left == right for left, right in zip(current_results, faiss_results))
    return {
        "database": str(database),
        "identities": len(by_identity),
        "vectors": len(vectors),
        "dimensions": int(vectors.shape[1]),
        "queries": query_count,
        "top_k": k,
        "retrieval_k": search_k,
        "load_seconds": round(load_seconds, 6),
        "current_rank_seconds": round(current_seconds, 6),
        "current_rate_per_second": round(query_count / current_seconds, 3) if current_seconds else None,
        "faiss_index_build_seconds": round(index_build_seconds, 6),
        "faiss_exact_search_seconds": round(faiss_seconds, 6),
        "speedup_vs_current": round(current_seconds / faiss_seconds, 3) if faiss_seconds else None,
        "identical_identity_top_k_rows": identical,
        "identity_top_k_parity": identical == query_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--queries", type=int, default=256)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--candidate-k",
        type=int,
        default=0,
        help="number of vector candidates before identity reduction (0 searches the complete gallery)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.candidate_k < 0:
        parser.error("--candidate-k must be non-negative")
    report = run_benchmark(args.db, args.queries, args.top_k, args.candidate_k)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
