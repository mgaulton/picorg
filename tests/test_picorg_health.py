from picorg_health import check_root, health_report


def test_health_report_distinguishes_partial_roots(tmp_path):
    healthy = tmp_path / "healthy"
    healthy.mkdir()
    report = health_report([healthy, tmp_path / "missing"])
    assert report["healthy"] is False
    assert report["partial"] is True
    assert report["healthy_paths"] == [str(healthy)]
    assert report["unavailable_paths"] == [str(tmp_path / "missing")]


def test_health_root_is_read_only(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    result = check_root(root)
    assert result == {"path": str(root), "healthy": True, "reason": "ok"}
    assert not list(tmp_path.glob("*.json"))


def test_read_only_mount_can_be_required_for_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("picorg_health.is_read_only_mount", lambda _path: True)
    report = health_report([tmp_path], require_writable=True)
    assert report["healthy"] is False
    assert report["records"][0]["reason"] == "read_only_mount"
