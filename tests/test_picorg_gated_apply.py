import json

import pytest

from picorg_sorter import MatchResult, file_sha256, load_gated_audit, write_audit


def _high_confidence_result(path):
    return MatchResult(
        path=str(path),
        source_root=str(path.parent),
        family="manual",
        canonical="creator_a",
        confidence=0.99,
        rule="alias",
    )


def test_fingerprinted_audit_round_trips_and_rejects_changed_source(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"before")
    result = _high_confidence_result(source)
    monkeypatch.setenv("PICORG_AUDIT_FINGERPRINTS", "1")

    audit = write_audit([result], {"scanned": 1}, "run-1", tmp_path / "audits")
    results, report, run_id = load_gated_audit(audit)
    assert len(results) == 1
    assert results[0].path == result.path
    assert results[0].canonical == result.canonical
    assert report["fingerprinted_sources"] == 1
    assert run_id == "run-1"

    source.write_bytes(b"after")
    with pytest.raises(RuntimeError, match="source files changed"):
        load_gated_audit(audit)


def test_gated_audit_requires_exact_high_confidence_coverage(tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"bytes")
    result = _high_confidence_result(source)
    payload = {
        "run_id": "run-2",
        "report": {},
        "results": [result.__dict__],
        "source_fingerprints": {str(source): file_sha256(source), str(tmp_path / "extra.jpg"): "x"},
    }
    audit = tmp_path / "bad.json"
    audit.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="cover exactly"):
        load_gated_audit(audit)
