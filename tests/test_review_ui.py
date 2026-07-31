import json
from pathlib import Path

import review_ui


def audit_payload(tmp_path: Path) -> Path:
    path = tmp_path / "audit.json"
    path.write_text(
        json.dumps(
            {
                "report": {"scanned": 2},
                "results": [
                    {"path": str(tmp_path / "A (1).jpg"), "title": "A", "canonical": None, "source_root": str(tmp_path)},
                    {"path": str(tmp_path / "A (2).jpg"), "title": "A", "canonical": None, "source_root": str(tmp_path)},
                    {"path": str(tmp_path / "known.jpg"), "title": "Known", "canonical": "known", "source_root": str(tmp_path)},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_build_clusters_groups_unmatched_gallery(tmp_path):
    clusters = review_ui.build_clusters(json.loads(audit_payload(tmp_path).read_text()))
    assert len(clusters) == 1
    assert clusters[0]["count"] == 2
    assert clusters[0]["title"] == "A"


def test_decision_is_saved_and_media_is_allowlisted(tmp_path):
    audit = audit_payload(tmp_path)
    (tmp_path / "A (1).jpg").write_bytes(b"not-an-image")
    decisions = tmp_path / "decisions.json"
    client = review_ui.create_app(audit, decisions).test_client()
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/favicon.ico").status_code == 204
    assert client.get("/missing-page").status_code == 404
    summary = client.get("/api/summary").get_json()
    assert "gallery_sets" not in summary["report"]
    cluster = client.get("/api/clusters").get_json()["clusters"][0]
    response = client.post(
        "/api/decisions",
        json={"cluster_id": cluster["cluster_id"], "identity": "creator_a", "family": "manual", "notes": "reviewed"},
    )
    assert response.status_code == 201
    assert json.loads(decisions.read_text())["decisions"][0]["identity"] == "creator_a"
    assert json.loads(decisions.read_text())["decisions"][0]["status"] == "pending"
    assert client.get("/media", query_string={"path": str(tmp_path / "A (1).jpg")}).status_code == 200
    assert client.get("/media", query_string={"path": "/etc/passwd"}).status_code == 403

    bulk = client.post(
        "/api/decisions/bulk",
        json={"cluster_ids": [cluster["cluster_id"]], "identity": "creator_a", "family": "manual", "status": "confirmed"},
    )
    assert bulk.status_code == 201
    assert json.loads(decisions.read_text())["decisions"][0]["status"] == "confirmed"
    oversized = client.post("/api/decisions", data="x" * 70000, content_type="application/json")
    assert oversized.status_code == 413


def test_export_promotes_confirmed_only(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"entries": []}), encoding="utf-8")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "decisions": [
                    {"cluster_id": "confirmed", "identity": "creator_a", "family": "manual", "status": "confirmed", "aliases": ["Creator A"], "notes": "two sources"},
                    {"cluster_id": "pending", "identity": "creator_b", "family": "manual", "status": "pending", "aliases": ["Creator B"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert review_ui.export_confirmed_decisions(decisions, registry) == 1
    entries = json.loads(registry.read_text())["entries"]
    assert [item["canonical"] for item in entries] == ["creator_a"]


def test_review_family_is_not_exportable(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"entries": []}), encoding="utf-8")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"decisions": [{"cluster_id": "review", "identity": "creator", "family": "review", "status": "confirmed"}]}), encoding="utf-8")

    assert review_ui.export_confirmed_decisions(decisions, registry) == 0
    assert json.loads(registry.read_text())["entries"] == []


def test_paginated_cached_queue_and_member_edits(tmp_path):
    audit = audit_payload(tmp_path)
    decisions = tmp_path / "decisions.json"
    overrides = tmp_path / "overrides.json"
    app = review_ui.create_app(audit, decisions, overrides)
    client = app.test_client()

    first_page = client.get("/api/clusters?page=1&page_size=1").get_json()
    assert first_page["page_size"] == 1
    assert first_page["has_next"] is False
    cluster_id = first_page["clusters"][0]["cluster_id"]
    path = review_ui.build_clusters(json.loads(audit.read_text()))[0]["paths"][0]
    assert audit.with_suffix(".clusters.json").exists()

    removed = client.post(f"/api/clusters/{cluster_id}/members", json={"path": path, "action": "remove"})
    assert removed.status_code == 201
    assert path not in client.get(f"/api/clusters/{cluster_id}").get_json()["paths"]
    added = client.post(f"/api/clusters/{cluster_id}/members", json={"path": path, "action": "add"})
    assert added.status_code == 201
    assert path in client.get(f"/api/clusters/{cluster_id}").get_json()["paths"]

    saved = client.post("/api/decisions", json={"cluster_id": cluster_id, "identity": "creator_a", "family": "review"})
    assert saved.status_code == 201
    assert client.delete(f"/api/decisions/{cluster_id}").status_code == 200
