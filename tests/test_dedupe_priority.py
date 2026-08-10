import json

from dedupe_priority import build_report, quarantine


def test_priority_copy_wins_and_target_is_quarantined(tmp_path):
    priority = tmp_path / "priority"
    target = tmp_path / "target"
    priority.mkdir()
    target.mkdir()
    (priority / "keep.jpg").write_bytes(b"same")
    (target / "remove.jpg").write_bytes(b"same")
    cache = tmp_path / "hashes.json"
    report = build_report([priority], [target], cache)
    assert report["duplicate_count"] == 1
    quarantine(report, tmp_path / "quarantine")
    assert (priority / "keep.jpg").is_file()
    assert not (target / "remove.jpg").exists()
    assert json.loads(cache.read_text())["files"]
