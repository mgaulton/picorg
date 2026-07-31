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
