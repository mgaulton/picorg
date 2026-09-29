from __future__ import annotations

import json

from incremental_face_match import _gallery_identities, _gallery_signature, should_reuse_result


def test_gallery_signature_changes_when_manifest_changes(tmp_path):
    database = tmp_path / "faces.db"
    manifest = tmp_path / "gallery.json"
    database.write_bytes(b"db")
    manifest.write_text(json.dumps({"gallery": {"alice": []}}), encoding="utf-8")
    first = _gallery_signature(database, manifest)
    manifest.write_text(json.dumps({"gallery": {"alice": [], "bob": []}}), encoding="utf-8")
    assert _gallery_signature(database, manifest) != first
    assert _gallery_identities(manifest) == {"alice", "bob"}


def test_gallery_change_reprocesses_unresolved_but_keeps_valid_matches():
    prior = {
        "/new.jpg": {"path": "/new.jpg", "status": "ambiguous"},
        "/known.jpg": {"path": "/known.jpg", "status": "matched", "matched_identity": "alice"},
        "/removed.jpg": {"path": "/removed.jpg", "status": "matched", "matched_identity": "old"},
    }
    current = {path: "sha-" + path for path in prior}
    assert not should_reuse_result(
        {"path": "/new.jpg"}, prior, current, current,
        gallery_changed=True, gallery_identities={"alice"},
    )
    assert should_reuse_result(
        {"path": "/known.jpg"}, prior, current, current,
        gallery_changed=True, gallery_identities={"alice"},
    )
    assert not should_reuse_result(
        {"path": "/removed.jpg"}, prior, current, current,
        gallery_changed=True, gallery_identities={"alice"},
    )


def test_legacy_output_without_gallery_signature_is_considered_stale():
    """Missing generation metadata must not preserve unresolved old results."""
    prior = {"/ambiguous.jpg": {"path": "/ambiguous.jpg", "status": "ambiguous"}}
    current = {"/ambiguous.jpg": "sha"}
    previous_signature = ""
    gallery_signature = "new-generation"
    gallery_changed = bool(prior) and (
        not previous_signature or previous_signature != gallery_signature
    )
    assert gallery_changed
    assert not should_reuse_result(
        {"path": "/ambiguous.jpg"}, prior, current, current,
        gallery_changed=gallery_changed, gallery_identities={"alice"},
    )
