import json

import insightface_pair_benchmark as benchmark


def test_run_reports_statuses_and_scores(monkeypatch, tmp_path):
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps([
        {"path_a": "a", "path_b": "b", "label": "genuine"},
        {"path_a": "a", "path_b": "c", "label": "impostor"},
    ]))

    class FakeBackend:
        def __init__(self, providers):
            pass

        def embed(self, path):
            return {"a": [0.0], "b": [0.1], "c": [1.0]}[str(path)], {"status": "embedded"}

    monkeypatch.setattr(benchmark, "InsightFaceBackend", FakeBackend)
    report = benchmark.run(pairs)
    assert report["pair_count"] == 2
    assert report["status_counts"] == {"embedded": 3}
