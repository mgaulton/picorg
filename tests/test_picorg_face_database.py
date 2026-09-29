import json
import sqlite3

import picorg_face_database as backend
import pytest


def test_iter_reference_images_filters_hidden_thumbnails_and_extensions(tmp_path):
    identity = tmp_path / "creator_a"
    identity.mkdir()
    (identity / "good.jpg").write_bytes(b"one")
    (identity / "preview_small.jpg").write_bytes(b"two")
    (identity / "notes.txt").write_text("not media", encoding="utf-8")
    (identity / ".hidden.jpg").write_bytes(b"hidden")

    rows = list(backend.iter_reference_images(tmp_path))

    assert rows == [("creator_a", identity / "good.jpg")]


def test_iter_reference_images_fails_closed_on_walk_error(tmp_path, monkeypatch):
    identity = tmp_path / "creator_a"
    identity.mkdir()

    def broken_walk(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(backend.os, "walk", broken_walk)
    with pytest.raises(RuntimeError, match="reference folder is unreadable"):
        list(backend.iter_reference_images(tmp_path))


def test_build_database_writes_compatible_schema(tmp_path, monkeypatch):
    identity = tmp_path / "creator_a"
    identity.mkdir()
    image = identity / "good.jpg"
    image.write_bytes(b"image-bytes")
    output = tmp_path / "face.sqlite3"

    monkeypatch.setattr(backend.extractor, "resolve_face_workers", lambda value=None: 1)
    monkeypatch.setattr(
        backend.extractor,
        "_extract_dlib_face",
        lambda task: {
            "path": task[0],
            "fingerprint": task[1],
            "status": "embedded",
            "embedding": [0.1] * 128,
        },
    )

    report = backend.build_database(tmp_path, output, workers=1)

    assert report["selected"] == 1
    assert report["embedded"] == 1
    assert report["faces"] == 1
    assert report["identities"] == 1
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT person_name, image_path FROM face_encodings").fetchone() == ("creator_a", str(image))
        assert connection.execute("SELECT total_faces FROM person_metadata").fetchone() == (1,)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"face_encodings", "person_metadata", "picorg_face_database_meta"} <= tables


def test_build_database_reuses_fingerprint_cache(tmp_path, monkeypatch):
    identity = tmp_path / "creator_a"
    identity.mkdir()
    image = identity / "good.jpg"
    image.write_bytes(b"image-bytes")
    cache = tmp_path / "embeddings.json"
    fingerprint = backend.extractor.file_fingerprint(image)
    cache.write_text(json.dumps({
        "model_id": backend.extractor.EMBEDDING_MODEL_ID,
        "records": {"old/path.jpg": {"fingerprint": fingerprint, "status": "embedded", "embedding": [0.2] * 128}},
    }), encoding="utf-8")
    monkeypatch.setattr(backend.extractor, "resolve_face_workers", lambda value=None: 1)
    monkeypatch.setattr(backend.extractor, "_extract_dlib_face", lambda task: (_ for _ in ()).throw(AssertionError("cache miss")))

    report = backend.build_database(tmp_path, tmp_path / "face.sqlite3", workers=1, cache=cache)

    assert report["cached"] == 1
    assert report["embedded"] == 1


def test_build_database_accepts_legacy_cache_embedding_without_status(tmp_path, monkeypatch):
    identity = tmp_path / "creator_a"
    identity.mkdir()
    image = identity / "good.jpg"
    image.write_bytes(b"image-bytes")
    cache = tmp_path / "embeddings.json"
    fingerprint = backend.extractor.file_fingerprint(image)
    cache.write_text(json.dumps({
        "model_id": backend.extractor.EMBEDDING_MODEL_ID,
        "records": {"old/path.jpg": {"fingerprint": fingerprint, "embedding": [0.2] * 128}},
    }), encoding="utf-8")
    monkeypatch.setattr(backend.extractor, "resolve_face_workers", lambda value=None: 1)
    monkeypatch.setattr(backend.extractor, "_extract_dlib_face", lambda task: (_ for _ in ()).throw(AssertionError("cache miss")))

    report = backend.build_database(tmp_path, tmp_path / "face.sqlite3", workers=1, cache=cache)

    assert report["cached"] == 1
    assert report["embedded"] == 1


def test_streaming_cache_filter_and_write_preserves_unrelated_records(tmp_path):
    cache = tmp_path / "embeddings.json"
    old = {"fingerprint": "old-fp", "status": "embedded", "embedding": [0.1] * 128}
    current = {"fingerprint": "current-fp", "status": "embedded", "embedding": [0.2] * 128}
    backend._write_embedding_cache(cache, {"old.jpg": old, "current.jpg": current}, {"selected": 2})

    paths, fingerprints = backend._load_embedding_cache(cache, wanted_paths={"current.jpg"}, wanted_fingerprints={"old-fp"})
    assert set(paths) == {"old.jpg", "current.jpg"}
    assert fingerprints["old-fp"] == old

    replacement = {"fingerprint": "current-fp-2", "status": "no_face"}
    backend._write_embedding_cache(cache, {"current.jpg": replacement}, {"selected": 1})
    loaded, _ = backend._load_embedding_cache(cache)
    assert loaded["old.jpg"] == old
    assert loaded["current.jpg"] == replacement


def test_build_database_reuses_mtime_size_hash_cache(tmp_path, monkeypatch):
    identity = tmp_path / "creator_a"
    identity.mkdir()
    image = identity / "good.jpg"
    image.write_bytes(b"image-bytes")
    stat = image.stat()
    hash_cache = tmp_path / "hashes.json"
    hash_cache.write_text(json.dumps({"files": {str(image): {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "sha256": "cached-sha",
    }}}), encoding="utf-8")
    monkeypatch.setattr(backend.extractor, "resolve_face_workers", lambda value=None: 1)
    monkeypatch.setattr(backend.extractor, "file_fingerprint", lambda path: (_ for _ in ()).throw(AssertionError("hash cache miss")))
    monkeypatch.setattr(
        backend.extractor,
        "_extract_dlib_face",
        lambda task: {"path": task[0], "fingerprint": task[1], "status": "embedded", "embedding": [0.3] * 128},
    )

    report = backend.build_database(tmp_path, tmp_path / "face.sqlite3", workers=1, hash_cache=hash_cache)

    assert report["hash_cache_hits"] == 1
