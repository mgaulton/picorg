from __future__ import annotations

import json

from online_evidence import MockEvidenceProvider, build_records, evidence_key


def test_mock_provider_is_hash_only_and_idempotent() -> None:
    digest = "a" * 64
    records = build_records(
        [{"path": "/tmp/private.jpg", "sha256": digest}, {"path": "/tmp/duplicate.jpg", "sha256": digest}],
        MockEvidenceProvider(),
        10,
    )
    assert len(records) == 1
    record = records[0]
    assert record["query_sha256"] == digest
    assert record["status"] == "not_queried"
    assert record["identity_assignment"] == "forbidden"
    assert record["evidence_id"] == evidence_key("mock", digest)
    assert record["result_url"] is None


def test_cli_writes_schema(tmp_path, monkeypatch) -> None:
    queue = tmp_path / "queue.json"
    output = tmp_path / "evidence.json"
    queue.write_text(json.dumps({"items": [{"path": "x.jpg", "sha256": "b" * 64}]}), encoding="utf-8")
    from online_evidence import main

    monkeypatch.setattr("sys.argv", ["online_evidence.py", str(queue), "--output", str(output)])
    assert main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["identity_assignment"] == "forbidden"
    assert len(payload["records"]) == 1
