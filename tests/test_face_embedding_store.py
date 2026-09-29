import json

import numpy as np

from face_embedding_store import import_json, list_paths, read_records, upsert_records


def test_import_is_idempotent_and_indexed(tmp_path):
    source = tmp_path / "cache.json"
    source.write_text(json.dumps({"records": {
        "/tmp/a.jpg": {"fingerprint": "abc", "embedding": [0.1] * 128},
        "/tmp/b.jpg": {"fingerprint": "def", "status": "no_face"},
    }}), encoding="utf-8")
    db = tmp_path / "embeddings.sqlite3"
    first = import_json(source, db)
    second = import_json(source, db)
    assert first["imported"] is True
    assert second["imported"] is False
    assert list_paths(db) == {"/tmp/a.jpg", "/tmp/b.jpg"}
    records = read_records(db, ["/tmp/a.jpg", "/tmp/b.jpg"])
    assert len(records["/tmp/a.jpg"]["embedding"]) == 128
    assert np.isclose(records["/tmp/a.jpg"]["embedding"][0], 0.1)
    assert records["/tmp/b.jpg"]["status"] == "no_face"


def test_upsert_records_updates_existing_path(tmp_path):
    db = tmp_path / "embeddings.sqlite3"
    written = upsert_records(db, [("/tmp/new.jpg", {
        "fingerprint": "sha1",
        "status": "matched",
        "embedding": [0.2] * 128,
        "quality": {"score": 0.9},
    })])
    assert written == 1
    record = read_records(db, ["/tmp/new.jpg"])["/tmp/new.jpg"]
    assert record["fingerprint"] == "sha1"
    assert record["status"] == "matched"
    assert record["quality"] == {"score": 0.9}
