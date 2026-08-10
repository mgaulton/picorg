import json

import media_preflight


def test_classify_path_distinguishes_missing_and_supported(tmp_path):
    assert media_preflight.classify_path(str(tmp_path / "missing.jpg")) == "missing"
    image = tmp_path / "valid.jpg"
    image.write_bytes(b"not decoded here")
    assert media_preflight.classify_path(str(image)) == "candidate"
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")
    assert media_preflight.classify_path(str(video)) == "unsupported_extension"


def test_verify_images_rejects_invalid_data(tmp_path):
    image = tmp_path / "invalid.jpg"
    image.write_bytes(b"not-an-image")
    assert media_preflight.classify_path(str(image), verify_image=True) == "corrupt"


def test_preflight_counts_only_unmatched_results(tmp_path):
    image = tmp_path / "valid.png"
    image.write_bytes(b"data")
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"results": [
        {"path": str(image)},
        {"path": str(tmp_path / "missing.jpg")},
        {"path": str(tmp_path / "known.jpg"), "canonical": "alice"},
    ]}))
    report = media_preflight.preflight(audit)
    assert report["total_unmatched"] == 2
    assert report["counts"] == {"candidate": 1, "missing": 1}


def test_verify_images_classifies_oversized_without_decoding_pixels(tmp_path):
    from PIL import Image

    image = tmp_path / "large.png"
    Image.new("RGB", (20, 20)).save(image)
    assert media_preflight.classify_path(str(image), verify_image=True, max_pixels=100) == "oversized"
