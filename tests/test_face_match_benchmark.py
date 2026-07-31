import face_match_benchmark as benchmark


def test_selects_lowest_fnmr_under_false_match_budget():
    embeddings = {
        "a": [0.0, 0.0],
        "b": [0.1, 0.0],
        "c": [1.0, 1.0],
        "d": [1.2, 1.0],
    }
    report = benchmark.evaluate_pairs(
        [
            {"path_a": "a", "path_b": "b", "label": "genuine"},
            {"path_a": "a", "path_b": "c", "label": "impostor"},
            {"path_a": "c", "path_b": "d", "label": "genuine"},
            {"path_a": "b", "path_b": "d", "label": "impostor"},
        ],
        embeddings,
        max_fmr=0.0,
    )
    assert report["selected"]["fmr"] == 0
    assert report["selected"]["fnmr"] == 0


def test_skips_pairs_without_cached_embeddings():
    report = benchmark.evaluate_pairs(
        [
            {"path_a": "a", "path_b": "missing", "label": True},
            {"path_a": "a", "path_b": "b", "label": True},
            {"path_a": "a", "path_b": "c", "label": False},
        ],
        {"a": [0.0], "b": [0.1], "c": [1.0]},
        max_fmr=0.1,
    )
    assert report["skipped_pairs"] == 1
    assert report["pair_count"] == 2
