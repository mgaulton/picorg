import hashlib
import json

from audit_face_marker_health import audit_markers


def test_audit_reports_hash_and_path_health(tmp_path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    digest = hashlib.sha256(b"image").hexdigest()
    markers = tmp_path / "markers.json"
    markers.write_text(json.dumps({"markers": [
        {"status": "confirmed", "identity": "creator", "path": str(image), "sha256": digest},
        {"status": "confirmed", "identity": "creator", "path": str(tmp_path / "gone.jpg"), "sha256": None},
        {"status": "rejected", "identity": "other", "path": str(image), "sha256": digest},
    ]}), encoding="utf-8")

    report = audit_markers(markers)
    assert report["confirmed"] == 2
    assert report["confirmed_with_sha256"] == 1
    assert report["valid_existing_sha256"] == 1
    assert report["missing_or_unhashed"] == 1
