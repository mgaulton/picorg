import json

from identity_evidence_store import (
    confirmed_keys,
    list_assignment_queue,
    open_store,
    queue_assignment,
    record_face_observation,
    record_match,
    record_match_results,
    record_pipeline_run,
    request_promotion,
    sync_store,
    update_assignment_status,
    upsert_cluster,
    upsert_media,
)


def test_sync_store_is_idempotent_and_preserves_confirmation(tmp_path):
    md = tmp_path / "md.json"
    markers = tmp_path / "markers.json"
    assignments = tmp_path / "assignments.json"
    md.write_text(json.dumps({"identities": [
        {"id": "alice", "primary_folder": "Alice", "display_names": ["Alice A"], "status": "confirmed"},
        {"id": "topic", "status": "pending"},
    ]}), encoding="utf-8")
    markers.write_text(json.dumps({"markers": [
        {"key": "image:1", "identity": "bob", "path": "/x.jpg", "status": "confirmed", "sha256": "abc"},
    ]}), encoding="utf-8")
    assignments.write_text(json.dumps({"decisions": [
        {"identity": "alice", "path": "/x.jpg"},
    ]}), encoding="utf-8")
    db = tmp_path / "state.sqlite3"
    first = sync_store(db, md_registry=md, markers=markers, assignments=assignments)
    second = sync_store(db, md_registry=md, markers=markers, assignments=assignments)
    assert first == second
    aliases, canonical = confirmed_keys(db)
    assert {"alice", "alicea", "bob"} <= aliases
    assert {"alice", "bob"} <= canonical


def test_content_addressed_evidence_and_assignment_queue_are_durable(tmp_path):
    db = tmp_path / "state.sqlite3"
    digest = "a" * 64
    upsert_media(db, sha256=digest, current_path="/incoming/photo.jpg", size=12, mtime_ns=3)
    observation = record_face_observation(
        db,
        sha256=digest,
        current_path="/incoming/photo.jpg",
        face_index=0,
        model_id="test-face",
        model_version="1",
        quality=0.91,
        status="embedded",
        embedding=b"vector",
    )
    match_id = record_match(
        db,
        observation_id=observation,
        identity="Alice Example",
        score=0.97,
        margin=0.12,
        threshold=0.9,
        model_id="test-face",
        model_version="1",
        decision="candidate",
        run_id="run-1",
    )
    assert observation > 0
    assert match_id > 0
    first = queue_assignment(
        db,
        path="/incoming/photo.jpg",
        expected_sha256=digest,
        identity="Alice Example",
        confidence=0.97,
    )
    assert queue_assignment(
        db,
        path="/incoming/photo.jpg",
        expected_sha256=digest,
        identity="Alice Example",
    ) == first
    assert list_assignment_queue(db, ["pending"])[0]["identity"] == "aliceexample"
    update_assignment_status(db, first, "conflict", "hash changed")
    assert list_assignment_queue(db, ["conflict"])[0]["error"] == "hash changed"


def test_cluster_run_and_promotion_records(tmp_path):
    db = tmp_path / "state.sqlite3"
    upsert_cluster(
        db,
        cluster_id="fbunknown-0001",
        display_name="Unknown 001",
        status="review",
        model_id="test-face",
        model_version="1",
        threshold=0.9,
    )
    record_pipeline_run(db, run_id="run-1", job="cycle", status="running", stage="matching", total=10, processed=3)
    promotion = request_promotion(
        db,
        identity="new-person",
        display_name="New Person",
        aliases=["newperson"],
        evidence_count=5,
    )
    assert promotion > 0
    import sqlite3

    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT status FROM clusters WHERE cluster_id='fbunknown-0001'").fetchone()[0] == "review"
        assert connection.execute("SELECT processed FROM pipeline_runs WHERE run_id='run-1'").fetchone()[0] == 3
        assert connection.execute("SELECT status FROM promotions WHERE promotion_id=?", (promotion,)).fetchone()[0] == "eligible"


def test_batch_match_results_are_keyed_by_hash_and_run(tmp_path):
    db = tmp_path / "evidence.sqlite3"
    digest = "b" * 64
    written = record_match_results(db, [{
        "path": "/incoming/photo.jpg",
        "sha256": digest,
        "status": "matched",
        "matched_face_index": 0,
        "quality": [0.8],
        "candidates": [{"person": "Alice Example", "distance": 0.2}, {"person": "Other", "distance": 0.5}],
    }], run_id="run-1")
    assert written == 1
    with open_store(db) as connection:
        assert connection.execute("select count(*) from media where sha256=?", (digest,)).fetchone()[0] == 1
        assert connection.execute("select count(*) from face_observations where sha256=?", (digest,)).fetchone()[0] == 1
        assert connection.execute("select identity from matches").fetchone()[0] == "aliceexample"
