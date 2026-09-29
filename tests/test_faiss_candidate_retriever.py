import numpy as np
import pytest

from faiss_candidate_retriever import (
    FaissCandidateRetriever,
    ReferenceRow,
    manifest_for_rows,
    retrieve_with_fallback,
    validate_manifest,
)


def _rows():
    return [
        ReferenceRow("alice", np.zeros(4, dtype=np.float32), 0.8),
        ReferenceRow("bob", np.ones(4, dtype=np.float32), 0.9),
    ]


def test_manifest_validation_and_fallback_without_faiss():
    rows = _rows()
    manifest = manifest_for_rows(rows, model_id="test", retrieval_k=2, database_sha256="d" * 64)
    assert validate_manifest(manifest, manifest)
    assert retrieve_with_fallback(None, [0, 0, 0, 0], lambda: rows) is rows
    try:
        import faiss  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="FAISS is unavailable"):
            FaissCandidateRetriever(rows, manifest)


def test_manifest_rejects_changed_gallery():
    rows = _rows()
    manifest = manifest_for_rows(rows, model_id="test", retrieval_k=2, database_sha256="d" * 64)
    changed = list(rows)
    changed[0] = ReferenceRow("different", changed[0].vector)
    changed_manifest = manifest_for_rows(changed, model_id="test", retrieval_k=2, database_sha256="d" * 64)
    assert not validate_manifest(manifest, changed_manifest)


def test_manifest_requires_real_database_fingerprint():
    rows = _rows()
    manifest = manifest_for_rows(rows, model_id="test", retrieval_k=2, database_sha256="")
    assert not validate_manifest(manifest, manifest)


def test_gallery_hash_includes_quality_and_path():
    rows = _rows()
    baseline = manifest_for_rows(rows, model_id="test", retrieval_k=2, database_sha256="d" * 64)
    changed = [ReferenceRow("alice", rows[0].vector, quality=0.1, path="changed.jpg"), rows[1]]
    changed_manifest = manifest_for_rows(changed, model_id="test", retrieval_k=2, database_sha256="d" * 64)
    assert baseline["gallery_sha256"] != changed_manifest["gallery_sha256"]
