import json

from build_face_pairs import build_pairs


def test_build_pairs_uses_confirmed_embeddings_only(tmp_path):
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"decisions": [
        {"status": "confirmed", "identity": "alice", "sample_paths": ["a", "b", "missing"]},
        {"status": "pending", "identity": "bob", "sample_paths": ["c", "d"]},
        {"status": "confirmed", "identity": "bob", "sample_paths": ["c", "d"]},
    ]}))
    embeddings = tmp_path / "embeddings.json"
    embeddings.write_text(json.dumps({"records": {
        "a": {"embedding": [0.0]}, "b": {"embedding": [0.1]},
        "c": {"embedding": [1.0]}, "d": {"embedding": [1.1]},
    }}))
    pairs = build_pairs(decisions, embeddings)
    assert sum(pair["label"] == "genuine" for pair in pairs) == 2
    assert sum(pair["label"] == "impostor" for pair in pairs) == 4


def test_build_pairs_can_use_individual_image_confirmations(tmp_path):
    decisions = tmp_path / "clusters.json"
    decisions.write_text(json.dumps({"decisions": [{"status": "confirmed", "identity": "ignored", "sample_paths": ["a", "b"]}]}))
    image_decisions = tmp_path / "image-decisions.json"
    image_decisions.write_text(json.dumps({"decisions": [
        {"status": "confirmed", "identity": "Alice", "path": "a"},
        {"status": "confirmed", "identity": "alice", "path": "b"},
        {"status": "confirmed", "identity": "Bob", "path": "c"},
        {"status": "confirmed", "identity": "Bob", "path": "d"},
    ]}))
    embeddings = tmp_path / "embeddings.json"
    embeddings.write_text(json.dumps({"records": {
        "a": {"embedding": [0.0]}, "b": {"embedding": [0.1]}, "c": {"embedding": [1.0]}, "d": {"embedding": [1.1]},
    }}))

    pairs = build_pairs(decisions, embeddings, image_decisions_path=image_decisions)

    assert sum(pair["label"] == "genuine" for pair in pairs) == 2
    assert sum(pair["label"] == "impostor" for pair in pairs) == 4
    assert {pair.get("identity") for pair in pairs if pair["label"] == "genuine"} == {"Alice", "Bob"}


def test_build_pairs_uses_latest_image_decision_per_path(tmp_path):
    decisions = tmp_path / "clusters.json"
    decisions.write_text(json.dumps({"decisions": []}))
    image_decisions = tmp_path / "image-decisions.json"
    image_decisions.write_text(json.dumps({"decisions": [
        {"status": "confirmed", "identity": "Alice", "path": "a", "saved_at": "2026-01-01T00:00:00Z"},
        {"status": "confirmed", "identity": "Alice", "path": "b", "saved_at": "2026-01-01T00:00:01Z"},
        {"status": "needs-evidence", "identity": "Alice", "path": "a", "saved_at": "2026-01-01T00:00:02Z"},
    ]}))
    embeddings = tmp_path / "embeddings.json"
    embeddings.write_text(json.dumps({"records": {"a": {"embedding": [0.0]}, "b": {"embedding": [0.1]}}}))

    assert build_pairs(decisions, embeddings, image_decisions_path=image_decisions) == []
