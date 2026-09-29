#!/usr/bin/env python3
"""Compare exact FAISS top-k retrieval with PicOrg's NumPy brute-force path.

This is a read-only benchmark.  It measures ranking parity and elapsed time
on a local embedding cache; it never changes the face database or decisions.
FAISS is optional so the normal PicOrg environment remains unchanged.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from face_match_benchmark import load_embeddings


def _vectors(path: Path, limit: int | None) -> tuple[list[str], np.ndarray]:
    embeddings = load_embeddings(path)
    items = list(embeddings.items())
    if limit:
        items = items[:limit]
    if not items:
        raise ValueError("embedding cache contains no vectors")
    matrix = np.asarray([vector for _, vector in items], dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("embedding vectors must have consistent dimensions")
    return [key for key, _ in items], matrix


def _numpy_topk(matrix: np.ndarray, queries: np.ndarray, k: int) -> np.ndarray:
    # Expand the squared L2 identity instead of materializing a
    # queries-by-vectors-by-dimensions tensor.  This keeps the benchmark
    # bounded as the reference gallery grows.
    distances = (
        np.sum(queries * queries, axis=1, keepdims=True)
        + np.sum(matrix * matrix, axis=1, keepdims=True).T
        - 2.0 * (queries @ matrix.T)
    )
    np.maximum(distances, 0.0, out=distances)
    return np.argsort(distances, axis=1, kind="stable")[:, :k]


def run_benchmark(cache: Path, queries: int, k: int, limit: int | None) -> dict[str, Any]:
    load_started = time.perf_counter()
    labels, matrix = _vectors(cache, limit)
    load_seconds = time.perf_counter() - load_started
    query_count = min(max(1, queries), len(labels))
    query_vectors = matrix[:query_count]
    k = min(max(1, k), len(labels))

    started = time.perf_counter()
    numpy_indices = _numpy_topk(matrix, query_vectors, k)
    numpy_seconds = time.perf_counter() - started

    try:
        import faiss  # type: ignore[import-not-found]
    except ImportError as exc:
        message = "install the optional retrieval extra: uv sync --extra retrieval"
        raise RuntimeError(message) from exc

    index_started = time.perf_counter()
    index = faiss.IndexFlatL2(matrix.shape[1])
    index.add(matrix)
    index_build_seconds = time.perf_counter() - index_started
    started = time.perf_counter()
    _, faiss_indices = index.search(query_vectors, k)
    faiss_seconds = time.perf_counter() - started
    identical_rows = sum(np.array_equal(left, right) for left, right in zip(numpy_indices, faiss_indices))
    return {
        "cache": str(cache),
        "vectors": len(labels),
        "dimensions": int(matrix.shape[1]),
        "queries": query_count,
        "top_k": k,
        "load_seconds": round(load_seconds, 6),
        "index_build_seconds": round(index_build_seconds, 6),
        "numpy_seconds": round(numpy_seconds, 6),
        "faiss_seconds": round(faiss_seconds, 6),
        "speedup": round(numpy_seconds / faiss_seconds, 3) if faiss_seconds else None,
        "identical_top_k_rows": identical_rows,
        "top_k_parity": identical_rows == query_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embeddings", type=Path, required=True)
    parser.add_argument("--queries", type=int, default=256)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, help="bounded cache sample for a quick benchmark")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmark(args.embeddings, args.queries, args.top_k, args.limit)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
