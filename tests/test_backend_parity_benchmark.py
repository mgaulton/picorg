import json

from backend_parity_benchmark import build_report


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_parity_uses_only_common_pairs(tmp_path):
    pairs = tmp_path / "pairs.json"
    legacy = tmp_path / "legacy.json"
    picorg = tmp_path / "picorg.json"
    _write(pairs, [
        {"path_a": "a.jpg", "path_b": "b.jpg", "label": "genuine"},
        {"path_a": "a.jpg", "path_b": "c.jpg", "label": "impostor"},
    ])
    _write(legacy, {"records": {"a.jpg": {"embedding": [0, 0]}, "b.jpg": {"embedding": [0, 0]}, "c.jpg": {"embedding": [1, 1]}}})
    _write(picorg, {"records": {"a.jpg": {"embedding": [0, 0]}, "b.jpg": {"embedding": [0, 0]}, "c.jpg": {"embedding": [1, 1]}, "unused.jpg": {"embedding": [3, 3]}}})

    report = build_report(pairs, legacy, picorg, 0.001)
    assert report["pair_count_common"] == 2
    assert report["legacy"]["genuine_pairs"] == 1
    assert report["picorg"]["impostor_pairs"] == 1
