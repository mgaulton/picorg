import sqlite3
import json

import numpy as np
import pytest

from faiss_image_disjoint_benchmark import run_benchmark


def test_image_disjoint_benchmark_requires_faiss(tmp_path):
    db = tmp_path / "faces.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE face_encodings (person_name TEXT, encoding BLOB, image_path TEXT)")
        conn.executemany(
            "INSERT INTO face_encodings VALUES (?, ?, ?)",
            [
                ("alice", np.zeros(128, dtype=np.float64).tobytes(), "ref-a.jpg"),
                ("bob", np.ones(128, dtype=np.float64).tobytes(), "ref-b.jpg"),
            ],
        )
    pairs = tmp_path / "pairs.json"
    pairs.write_text('[{"path_a":"query-a.jpg","path_b":"query-b.jpg","label":"genuine"}]')
    embeddings = tmp_path / "embeddings.json"
    embeddings.write_text(json.dumps({
        "records": {
            "query-a.jpg": {"embedding": [0] * 128},
            "query-b.jpg": {"embedding": [1] * 128},
        }
    }))
    try:
        import faiss  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="optional retrieval extra"):
            run_benchmark(db, pairs, embeddings, queries=2, top_k=1)
