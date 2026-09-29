import face_cluster_purity_report as report


def test_metrics_use_face_cluster_ids_and_report_mixed_clusters(tmp_path):
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        '{"decisions": [{"scope": "image", "status": "confirmed", "identity": "alice", "path": "/old/a.jpg"},'
        '{"scope": "image", "status": "confirmed", "identity": "bob", "path": "/old/c.jpg"}]}'
    )
    audit = {
        "model_id": "dlib",
        "threshold": 0.52,
        "strict_all_members": True,
        "results": [
            {"path": "/old/a.jpg", "face_cluster_id": "face-1"},
            {"path": "/old/b.jpg", "face_cluster_id": "face-1"},
            {"path": "/old/c.jpg", "face_cluster_id": "face-1"},
            {"path": "/old/d.jpg", "title": "filename-only"},
        ],
    }
    metrics = report.compute_metrics(audit, [decisions])
    assert metrics["clusters"] == 1
    assert metrics["clusters_mixed"] == 1
    assert metrics["weighted_purity"] == 0.5
    assert metrics["comparable_to_default_strict_mode"] is True


def test_unique_basename_relink_is_conservative(tmp_path):
    root = tmp_path / "sorted"
    (root / "alice").mkdir(parents=True)
    (root / "alice" / "a.jpg").write_bytes(b"x")
    decisions = tmp_path / "decisions.json"
    decisions.write_text('{"decisions": [{"scope": "image", "status": "confirmed", "identity": "alice", "path": "/old/a.jpg"}]}')
    audit = {"results": [{"path": "/old/a.jpg", "face_cluster_id": "face-1"}], "strict_all_members": False}
    metrics = report.compute_metrics(audit, [decisions], [root])
    assert metrics["labeled_paths"] == 1
    assert metrics["resolver"]["relinked"] == 2
    assert metrics["comparable_to_default_strict_mode"] is False
