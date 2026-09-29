from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_review import OPINION_KEYS, _validate_endpoint, run_review, select_items, validate_opinion


def _opinion() -> dict:
    return {
        "face_count": 1,
        "occlusion": "none",
        "pose": "frontal",
        "blur": "low",
        "crop_quality": "good",
        "possible_duplicate": False,
        "abstain": False,
        "reason_codes": [],
    }


def test_selection_is_bounded_and_uncertain_first() -> None:
    rows = [
        {"path": "clear.jpg", "min_similarity": 0.99, "face_quality": 0.9},
        {"path": "uncertain.jpg", "min_similarity": 0.72, "face_count": 2, "count": 100},
        {"path": "other.jpg", "min_similarity": 0.95},
    ]
    selected = select_items(rows, 2)
    assert selected[0]["path"] == "uncertain.jpg"
    assert selected[1]["path"] in {"clear.jpg", "other.jpg"}


def test_agent_opinion_rejects_identity_fields() -> None:
    value = _opinion()
    value["identity"] = "someone"
    with pytest.raises(ValueError, match="forbidden"):
        validate_opinion(value)


def test_agent_opinion_requires_exact_schema() -> None:
    value = _opinion()
    value.pop("abstain")
    with pytest.raises(ValueError, match="exactly"):
        validate_opinion(value)
    assert set(OPINION_KEYS) == {
        "face_count", "occlusion", "pose", "blur", "crop_quality",
        "possible_duplicate", "abstain", "reason_codes",
    }


def test_endpoint_is_loopback_only_by_default() -> None:
    assert _validate_endpoint("http://127.0.0.1:11434/api/chat", False).endswith("/api/chat")
    assert _validate_endpoint("http://localhost:11434/api/chat", False).startswith("http://localhost")
    with pytest.raises(ValueError, match="non-loopback"):
        _validate_endpoint("http://192.168.2.5:11434/api/chat", False)
    assert _validate_endpoint("http://192.168.2.5:11434/api/chat", True).startswith("http://192.168.2.5")


def test_mock_run_is_report_only(tmp_path: Path) -> None:
    image = tmp_path / "candidate.jpg"
    image.write_bytes(b"not-an-image")
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"results": [{"path": str(image), "min_similarity": 0.7}]}), encoding="utf-8")
    output = tmp_path / "agent.json"
    report = run_review(audit, output, limit=1)
    assert report["identity_assignment"] == "forbidden"
    assert report["source_audit_sha256"] == hashlib.sha256(audit.read_bytes()).hexdigest()
    assert report["source_audit_schema_version"] is None
    assert report["selected"] == 1
    record = report["records"][0]
    assert record["opinion"]["abstain"] is True
    assert "identity" not in record["opinion"]
    assert output.is_file()
