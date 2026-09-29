import sqlite3

import numpy as np
import pytest

from faiss_identity_benchmark import run_benchmark


def test_identity_benchmark_requires_optional_faiss(tmp_path):
    db = tmp_path / "faces.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE face_encodings (person_name TEXT, encoding BLOB)")
        conn.executemany(
            "INSERT INTO face_encodings VALUES (?, ?)",
            [
                ("alice", np.zeros(128, dtype=np.float64).tobytes()),
                ("bob", np.ones(128, dtype=np.float64).tobytes()),
            ],
        )
    try:
        import faiss  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="optional retrieval extra"):
            run_benchmark(db, queries=1, k=1)
