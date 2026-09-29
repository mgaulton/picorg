#!/usr/bin/env python3
"""Optional FAISS candidate retrieval with fail-safe manifest validation.

The adapter only narrows a reference gallery for a later verifier. Callers
must retain PicOrg's distance, quality, margin, and safety gates and fall back
to the existing brute-force path whenever FAISS or its manifest is invalid.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class ReferenceRow:
    identity: str
    vector: np.ndarray
    quality: float = 0.0
    path: str = ""


def gallery_sha256(rows: Iterable[ReferenceRow]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.identity.encode("utf-8"))
        digest.update(b"\0")
        digest.update(row.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(np.asarray([row.quality], dtype=np.float32).tobytes())
        digest.update(np.asarray(row.vector, dtype=np.float32).tobytes())
    return digest.hexdigest()


def manifest_for_rows(
    rows: Sequence[ReferenceRow],
    *,
    model_id: str,
    retrieval_k: int,
    database_sha256: str,
) -> dict[str, object]:
    dimensions = int(rows[0].vector.shape[0]) if rows else 0
    return {
        "schema_version": 1,
        "index_type": "IndexFlatL2",
        "database_sha256": database_sha256,
        "gallery_sha256": gallery_sha256(rows),
        "model_id": model_id,
        "dimensions": dimensions,
        "metric": "L2",
        "normalized": False,
        "identity_count": len({row.identity for row in rows}),
        "vector_count": len(rows),
        "retrieval_k": int(retrieval_k),
    }


def validate_manifest(actual: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    keys = (
        "schema_version",
        "index_type",
        "database_sha256",
        "gallery_sha256",
        "model_id",
        "dimensions",
        "metric",
        "normalized",
        "identity_count",
        "vector_count",
    )
    if not all(actual.get(key) == expected.get(key) for key in keys):
        return False
    return (
        isinstance(actual.get("model_id"), str)
        and bool(actual.get("model_id"))
        and isinstance(actual.get("database_sha256"), str)
        and len(str(actual.get("database_sha256"))) == 64
        and isinstance(actual.get("gallery_sha256"), str)
        and len(str(actual.get("gallery_sha256"))) == 64
        and int(actual.get("dimensions", 0)) > 0
        and int(actual.get("identity_count", 0)) > 0
        and int(actual.get("vector_count", 0)) > 0
        and int(actual.get("retrieval_k", 0)) > 0
    )


class FaissCandidateRetriever:
    """Build an exact index and return candidate reference rows by position."""

    def __init__(self, rows: Sequence[ReferenceRow], manifest: Mapping[str, object]):
        if not rows:
            raise ValueError("cannot index an empty reference gallery")
        matrix = np.asarray([row.vector for row in rows], dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[1] == 0:
            raise ValueError("reference vectors must form a two-dimensional matrix")
        if not np.isfinite(matrix).all():
            raise ValueError("reference vectors must contain only finite values")
        expected = manifest_for_rows(
            rows,
            model_id=str(manifest.get("model_id", "")),
            retrieval_k=int(manifest.get("retrieval_k", 0)),
            database_sha256=str(manifest.get("database_sha256", "")),
        )
        if not validate_manifest(manifest, expected):
            raise ValueError("FAISS manifest does not match the reference gallery")
        try:
            import faiss  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("FAISS is unavailable; use the brute-force fallback") from exc
        self.rows = tuple(rows)
        self.manifest = dict(manifest)
        self.index = faiss.IndexFlatL2(matrix.shape[1])
        self.index.add(matrix)

    def candidate_rows(self, query: Sequence[float], candidate_k: int | None = None) -> list[ReferenceRow]:
        vector = np.asarray(query, dtype=np.float32).reshape(1, -1)
        if vector.shape[1] != int(self.manifest["dimensions"]):
            raise ValueError("query dimensions do not match the index manifest")
        if not np.isfinite(vector).all():
            raise ValueError("query vector must contain only finite values")
        requested = int(candidate_k or self.manifest["retrieval_k"])
        requested = min(max(1, requested), len(self.rows))
        _, indices = self.index.search(vector, requested)
        return [self.rows[int(index)] for index in indices[0] if int(index) >= 0]


def retrieve_with_fallback(
    retriever: FaissCandidateRetriever | None,
    query: Sequence[float],
    fallback: Callable[[], list[ReferenceRow]],
    candidate_k: int | None = None,
) -> list[ReferenceRow]:
    """Return FAISS candidates or the complete brute-force reference set."""
    if retriever is None:
        return fallback()
    try:
        return retriever.candidate_rows(query, candidate_k)
    except (RuntimeError, ValueError, TypeError):
        return fallback()


def dump_manifest(path: str, manifest: Mapping[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=f".{destination.name}.", delete=False
    )
    try:
        with temporary:
            json.dump(dict(manifest), temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, destination)
    finally:
        try:
            os.unlink(temporary.name)
        except FileNotFoundError:
            pass
