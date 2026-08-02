import face_cluster_unmatched as matcher


def test_cluster_embeddings_groups_close_vectors_and_keeps_separate_faces():
    clusters = matcher.cluster_embeddings(
        [
            ("a.jpg", [0.0, 0.0]),
            ("b.jpg", [0.1, 0.0]),
            ("c.jpg", [1.0, 1.0]),
        ],
        threshold=0.2,
    )
    assert [cluster["count"] for cluster in clusters] == [2, 1]
    assert clusters[0]["paths"] == ["a.jpg", "b.jpg"]


def test_vector_distance_is_deterministic():
    assert matcher.vector_distance([0, 0], [3, 4]) == 5


def test_file_fingerprint_changes_when_file_changes(tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"first")
    first = matcher.file_fingerprint(path)
    path.write_bytes(b"second")
    assert matcher.file_fingerprint(path) != first


def test_load_rgb_image_converts_palette_transparency_without_mutating_source(tmp_path):
    from PIL import Image

    path = tmp_path / "palette.png"
    image = Image.new("P", (2, 2))
    image.info["transparency"] = 0
    image.save(path)
    loaded = matcher.load_rgb_image(path)
    assert loaded.shape == (2, 2, 3)
    assert Image.open(path).mode == "P"


def test_extract_embeddings_records_error_categories(tmp_path):
    audit = tmp_path / "audit.json"
    missing = tmp_path / "missing.jpg"
    audit.write_text('{"results": [{"path": "' + str(missing) + '"}]}')
    cache = tmp_path / "cache.json"
    _, stats = matcher.extract_embeddings(audit, cache_path=cache, checkpoint_seconds=1)
    assert stats["errors"] == 1
    assert stats["error_categories"]["FileNotFoundError"] == 1
    assert stats["error_samples"][0]["path"] == str(missing)
