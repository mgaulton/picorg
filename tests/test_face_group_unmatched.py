import face_group_unmatched


def test_unreadable_media_stat_is_treated_as_unavailable(monkeypatch, tmp_path):
    def raise_io_error(_path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(face_group_unmatched.Path, "is_file", raise_io_error)
    assert face_group_unmatched._is_readable_file(str(tmp_path / "degraded.jpg")) is False
