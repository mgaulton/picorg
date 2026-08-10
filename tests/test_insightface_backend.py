import pytest

import insightface_backend as backend


def test_model_id_is_explicit():
    assert backend.MODEL_ID == "insightface-buffalo_l-scrfd-arcface"


def test_missing_optional_dependency_is_actionable(monkeypatch):
    real_import = __import__

    def blocked(name, *args, **kwargs):
        if name == "insightface":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    with pytest.raises(RuntimeError, match="requirements-insightface.txt"):
        backend.InsightFaceBackend()
