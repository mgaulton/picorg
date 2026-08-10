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
