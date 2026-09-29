import hashlib
import json

from relink_face_markers import relink


def test_relink_uses_exact_hash_and_adds_hash_for_existing(tmp_path):
    root = tmp_path / "sorted"
    root.mkdir()
    moved = root / "person" / "photo.jpg"
    moved.parent.mkdir()
    moved.write_bytes(b"marker-photo")
    digest = hashlib.sha256(b"marker-photo").hexdigest()
    payload = {"markers": [
        {"key": "old", "path": "/missing/photo.jpg", "sha256": digest, "status": "confirmed"},
        {"key": "existing", "path": str(moved), "status": "confirmed"},
    ]}
    result, stats = relink(payload, [root])
    assert result["markers"][0]["path"] == str(moved)
    assert result["markers"][0]["relinked_from"] == "/missing/photo.jpg"
    assert result["markers"][1]["sha256"] == digest
    assert stats["relinked"] == 1
    assert stats["hashed"] == 1
