import reconcile_review_clusters as reconcile


def audit(results):
    return {"results": results}


def test_reconcile_links_name_and_face_clusters_by_shared_paths():
    name = audit([
        {"path": "a.jpg", "title": "FB IMG", "canonical": None},
        {"path": "b.jpg", "title": "FB IMG", "canonical": None},
    ])
    face = audit([
        {"path": "a.jpg", "title": "face-a", "canonical": None},
        {"path": "c.jpg", "title": "face-a", "canonical": None},
    ])
    clusters = reconcile.reconcile_clusters(name, face)
    methods = {item["method"] for item in clusters}
    assert "name+face" in methods
    merged = next(item for item in clusters if item["method"] == "name+face")
    assert merged["paths"] == ["a.jpg", "b.jpg", "c.jpg"]
