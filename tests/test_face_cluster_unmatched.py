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
