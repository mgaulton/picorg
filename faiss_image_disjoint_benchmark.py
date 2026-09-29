#!/usr/bin/env python3
"""Measure FAISS identity-candidate parity on image-disjoint reviewed queries."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from faiss_identity_benchmark import current_rank, faiss_rank, load_database
from face_match_benchmark import load_embeddings, load_pairs


def run_benchmark(
    database: Path,
    pairs_path: Path,
    embeddings_path: Path,
    queries: int,
    top_k: int,
    candidate_k: int = 0,
) -> dict[str, Any]:
    names, vectors, by_identity = load_database(database)
    with sqlite3.connect(database) as conn:
        database_paths = {str(row[0]) for row in conn.execute("SELECT image_path FROM face_encodings")}
    embeddings = load_embeddings(embeddings_path)
    pair_records = load_pairs(pairs_path)
    query_paths = sorted(
        {
            str(pair[key])
            for pair in pair_records
            for key in ("path_a", "path_b")
            if str(pair.get(key)) in embeddings
        }
    )
    overlapping = sorted(set(query_paths) & database_paths)
    query_paths = [path for path in query_paths if path not in database_paths]
    if not query_paths:
        raise ValueError("no image-disjoint query embeddings are available")
    query_count = min(max(1, queries), len(query_paths))
    spread_indices = np.linspace(0, len(query_paths) - 1, query_count, dtype=np.int64)
    query_paths = [query_paths[int(index)] for index in spread_indices]
    query_vectors = np.asarray([embeddings[path] for path in query_paths], dtype=np.float32)
    top_k = min(max(1, top_k), len(by_identity))

    started = time.perf_counter()
    current_results = [current_rank(query, vectors, by_identity, top_k) for query in query_vectors]
    current_seconds = time.perf_counter() - started

    try:
        import faiss  # type: ignore[import-not-found]
    except ImportError as exc:
        message = "install the optional retrieval extra: uv sync --extra retrieval"
        raise RuntimeError(message) from exc
    index = faiss.IndexFlatL2(vectors.shape[1])
    index.add(vectors)
    search_k = len(vectors) if candidate_k <= 0 else min(max(candidate_k, top_k), len(vectors))
    started = time.perf_counter()
    distances, indices = index.search(query_vectors, search_k)
    faiss_results = faiss_rank(index, names, distances, indices, top_k)
    faiss_seconds = time.perf_counter() - started
    identical = sum(left == right for left, right in zip(current_results, faiss_results))
    return {
        "database": str(database),
        "pairs": str(pairs_path),
        "embeddings": str(embeddings_path),
        "database_vectors": len(vectors),
        "database_identities": len(by_identity),
        "query_count": len(query_paths),
        "overlapping_query_paths_excluded": len(overlapping),
        "top_k": top_k,
        "retrieval_k": search_k,
        "current_rank_seconds": round(current_seconds, 6),
        "faiss_search_seconds": round(faiss_seconds, 6),
        "speedup_vs_current": round(current_seconds / faiss_seconds, 3) if faiss_seconds else None,
        "identical_identity_top_k_rows": identical,
        "identity_top_k_parity": identical == len(query_paths),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--embeddings", type=Path, required=True)
    parser.add_argument("--queries", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--candidate-k", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.candidate_k < 0:
        parser.error("--candidate-k must be non-negative")
    report = run_benchmark(
        args.db,
        args.pairs,
        args.embeddings,
        args.queries or 1_000_000,
        args.top_k,
        args.candidate_k,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
