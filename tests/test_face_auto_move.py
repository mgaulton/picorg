from __future__ import annotations

import hashlib
import json

import face_auto_move


def test_benchmark_gate_rejects_high_false_nonmatch_rate():
    errors = face_auto_move.benchmark_errors(
        {
            "genuine_pairs": 200,
            "impostor_pairs": 2000,
            "selected": {"fmr_ci95": [0.0, 0.0005], "fnmr_ci95": [0.1, 0.2]},
        }
    )
    assert any("fnmr_ci95" in error for error in errors)


def test_plan_allows_one_known_face_with_unknown_companions(tmp_path, monkeypatch):
    source = tmp_path / "one-known.jpg"
    source.write_bytes(b"image")
    digest = hashlib.sha256(b"image").hexdigest()
    identity = face_auto_move.sorter.Identity("alice", "manual", ())
    monkeypatch.setattr(face_auto_move, "identity_index", lambda: {"alice": identity})
    plan = face_auto_move.build_plan(
        {
            "audit": "audit.json",
            "threshold": 0.45,
            "margin": 0.08,
            "source_fingerprints": {str(source): digest},
            "results": [{
                "path": str(source),
                "status": "matched",
                "matched_identity": "alice",
                "confident_face_count": 1,
                "multi_face_policy": "one_known_other_unresolved",
                "candidates": [{"person": "alice", "distance": 0.30}, {"person": "other", "distance": 0.45}],
            }],
        },
        source_roots=[tmp_path],
        protected_roots=[],
    )
    assert plan["eligible_count"] == 1
    assert plan["eligible"][0]["multi_face_policy"] == "one_known_other_unresolved"


def test_plan_blocks_multiple_confident_faces(tmp_path, monkeypatch):
    source = tmp_path / "conflict.jpg"
    source.write_bytes(b"image")
    identity = face_auto_move.sorter.Identity("alice", "manual", ())
    monkeypatch.setattr(face_auto_move, "identity_index", lambda: {"alice": identity})
    plan = face_auto_move.build_plan(
        {
            "source_fingerprints": {str(source): hashlib.sha256(b"image").hexdigest()},
            "results": [{
                "path": str(source), "status": "matched", "matched_identity": "alice",
                "confident_face_count": 2, "candidates": [{"person": "alice", "distance": 0.2}],
            }],
        },
        source_roots=[tmp_path],
        protected_roots=[],
    )
    assert plan["eligible_count"] == 0
    assert plan["blocked"][0]["reason"] == "not_exactly_one_confident_face"
