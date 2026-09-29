import json

import numpy as np

import uniface_pair_benchmark as benchmark


def test_single_face_policy_rejects_group_photos():
    face = object()
    assert benchmark._single_face([face]) is face
    assert benchmark._single_face([]) is None
    assert benchmark._single_face([face, object()]) is None


def test_uniface_benchmark_classifies_missing_inputs_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "_extractor", lambda backend: lambda image: None)
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps([
        {"path_a": str(tmp_path / "missing-a.jpg"), "path_b": str(tmp_path / "missing-b.jpg"), "label": "genuine", "identity": "a"},
        {"path_a": str(tmp_path / "missing-a.jpg"), "path_b": str(tmp_path / "missing-c.jpg"), "label": "impostor", "identity_a": "a", "identity_b": "b"},
    ]))
    report = benchmark.run(pairs, "adaface", [0.5])
    assert report["status_counts"] == {"missing": 3}
    assert report["scored_pairs"] == 0
    assert report["coverage"] == 0.0


def test_uniface_benchmark_relinks_unique_basename(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "_extractor", lambda backend: lambda image: None)
    source = tmp_path / "organized" / "identity" / "photo.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not-an-image")
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps([
        {"path_a": "/old/photo.jpg", "path_b": "/old/missing.jpg", "label": "genuine", "identity": "a"},
    ]))
    report = benchmark.run(pairs, "adaface", [0.5], search_roots=[tmp_path / "organized"])
    assert report["relinked_images"] == 1
    assert report["status_counts"] == {"image_unreadable": 1, "missing": 1}


def test_uniface_operating_points_include_wilson_intervals(tmp_path, monkeypatch):
    same_a = tmp_path / "same-a.jpg"
    same_b = tmp_path / "same-b.jpg"
    other = tmp_path / "other.jpg"
    for path in (same_a, same_b, other):
        path.write_bytes(b"fixture")
    monkeypatch.setattr(benchmark.cv2, "imread", lambda path: path)
    monkeypatch.setattr(
        benchmark,
        "_extractor",
        lambda backend: lambda image: np.array([1.0, 0.0])
        if "same" in image
        else np.array([-1.0, 0.0]),
    )
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps([
        {"path_a": str(same_a), "path_b": str(same_b), "label": "genuine"},
        {"path_a": str(same_a), "path_b": str(other), "label": "impostor"},
    ]))
    report = benchmark.run(pairs, "adaface", [0.5])
    point = report["operating_points"][0]
    assert point["fmr"] == 0.0
    assert point["fnmr"] == 0.0
    assert len(point["fmr_ci95"]) == 2
    assert len(point["fnmr_ci95"]) == 2
