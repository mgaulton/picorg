import json

import pytest

from faiss_exact_benchmark import run_benchmark


def test_benchmark_requires_optional_faiss(tmp_path):
    cache = tmp_path / "embeddings.json"
    cache.write_text(json.dumps({"records": {"a": {"embedding": [0.0, 0.0]}, "b": {"embedding": [1.0, 1.0]}}}))
    try:
        import faiss  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="optional retrieval extra"):
            run_benchmark(cache, queries=1, k=1, limit=None)
    else:
        report = run_benchmark(cache, queries=1, k=1, limit=None)
        assert report["top_k_parity"] is True
