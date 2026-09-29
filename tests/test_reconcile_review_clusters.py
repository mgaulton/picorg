import reconcile_review_clusters as reconcile


def audit(results):
    return {"results": results}


def test_reconcile_prioritizes_face_cluster_membership():
    name = audit([
        {"path": "a.jpg", "title": "FB IMG", "canonical": None},
        {"path": "b.jpg", "title": "FB IMG", "canonical": None},
    ])
    face = audit([
        {"path": "a.jpg", "title": "fbunknown001", "cluster_label": "fbunknown001", "canonical": None},
        {"path": "c.jpg", "title": "fbunknown001", "cluster_label": "fbunknown001", "canonical": None},
    ])
    clusters = reconcile.reconcile_clusters(name, face)
    methods = {item["method"] for item in clusters}
    assert "name+face" in methods
    merged = next(item for item in clusters if item["method"] == "name+face")
    assert merged["paths"] == ["a.jpg", "c.jpg"]
    assert merged["title"].startswith("fbunknown001")
    assert "name: FB IMG" in merged["title"]
    assert all(item["method"] != "name-only" for item in clusters)
    assert all(item["face_clusters"] for item in clusters)


def test_face_cluster_ids_never_merge_on_shared_label():
    face = audit([
        {"path": "a.jpg", "title": "fbunknown001", "cluster_label": "fbunknown001", "face_cluster_id": "face-a", "canonical": None},
        {"path": "b.jpg", "title": "fbunknown001", "cluster_label": "fbunknown001", "face_cluster_id": "face-b", "canonical": None},
    ])
    clusters = reconcile.reconcile_clusters(audit([]), face)
    assert {tuple(item["paths"]) for item in clusters} == {("a.jpg",), ("b.jpg",)}
    assert all(item["face_clusters"] and len(item["face_clusters"]) == 1 for item in clusters)
