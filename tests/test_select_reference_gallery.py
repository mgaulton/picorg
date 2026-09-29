import json

import numpy as np

from select_reference_gallery import _confirmed_paths, select_gallery


def test_confirmed_markers_require_hash_and_match(tmp_path):
    image = tmp_path / "confirmed.jpg"
    image.write_bytes(b"confirmed")
    import hashlib

    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    markers = tmp_path / "markers.json"
    markers.write_text(
        json.dumps(
            {
                "markers": [
                    {"path": str(image), "status": "confirmed", "sha256": digest},
                    {"path": str(tmp_path / "legacy.jpg"), "status": "confirmed", "sha256": None},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert _confirmed_paths(markers) == {str(image.resolve())}


def test_selection_prioritizes_confirmed_and_preserves_diversity(tmp_path):
    confirmed = tmp_path / "confirmed.jpg"
    other = tmp_path / "other.jpg"
    rows = [
        (str(other), 1.0, np.array([0.0, 0.0])),
        (str(confirmed), 0.1, np.array([1.0, 1.0])),
    ]
    selected = select_gallery(rows, 1, {str(confirmed.resolve())})
    assert selected == [(str(confirmed), 0.1)]
