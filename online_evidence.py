#!/usr/bin/env python3
"""Create provider-neutral online-evidence records without uploading media.

The default provider is a deterministic mock.  This deliberately keeps the
online step report-only: no identity, marker, decision, or file move can be
created by this command.  Future providers can implement ``EvidenceProvider``
and must be explicitly enabled by the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1


def evidence_key(provider: str, query_sha256: str) -> str:
    """Return a stable idempotence key for one provider/query pair."""

    return hashlib.sha256(f"{provider}:{query_sha256}".encode("utf-8")).hexdigest()


def _query_hash(item: dict[str, Any]) -> str | None:
    for key in ("face_sha256", "query_sha256", "sha256"):
        value = item.get(key)
        if isinstance(value, str) and len(value) == 64:
            return value.lower()
    return None


def _face_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(item.get("face_index"), int):
        metadata["face_index"] = item["face_index"]
    if isinstance(item.get("face_box"), list) and len(item["face_box"]) == 4:
        try:
            metadata["face_box"] = [int(value) for value in item["face_box"]]
        except (TypeError, ValueError):
            pass
    return metadata


class EvidenceProvider:
    name = "base"

    def query(self, item: dict[str, Any], query_sha256: str) -> dict[str, Any]:
        raise NotImplementedError


class MockEvidenceProvider(EvidenceProvider):
    """A no-network provider used to validate the contract and pipeline wiring."""

    name = "mock"

    def query(self, item: dict[str, Any], query_sha256: str) -> dict[str, Any]:
        del item
        return {
            "status": "not_queried",
            "result_url": None,
            "result_type": "mock",
            "provider_score": None,
            "http_status": None,
            "reason": "mock provider never uploads or contacts a service",
            "query_sha256": query_sha256,
        }


def build_records(items: Iterable[object], provider: EvidenceProvider, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if len(records) >= limit:
            break
        if not isinstance(raw, dict):
            continue
        query_sha256 = _query_hash(raw)
        if not query_sha256:
            continue
        key = evidence_key(provider.name, query_sha256)
        if key in seen:
            continue
        seen.add(key)
        result = provider.query(raw, query_sha256)
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": key,
            "provider": provider.name,
            "query_sha256": query_sha256,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "path": raw.get("path"),
            "source_audit": raw.get("source_audit"),
            "provenance": "online-evidence-report-only",
            "identity_assignment": "forbidden",
            **_face_metadata(raw),
            **result,
        }
        records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Create opt-in online face evidence records (mock by default)")
    parser.add_argument("queue", type=Path, help="reverse_search_queue.py JSON output")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("mock",), default="mock")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--enable-online", action="store_true", help="reserved for explicitly approved network providers")
    args = parser.parse_args()
    if args.enable_online and args.provider == "mock":
        parser.error("--enable-online is unnecessary for the mock provider; no network provider is installed")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    payload = json.loads(args.queue.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        parser.error("queue must be a JSON object with an items list")
    provider = MockEvidenceProvider()
    records = build_records(payload["items"], provider, args.limit)
    output = {
        "schema_version": SCHEMA_VERSION,
        "source_queue": str(args.queue),
        "provider": provider.name,
        "privacy": "hashes and metadata only; no image upload or provider request was performed",
        "identity_assignment": "forbidden",
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"provider": provider.name, "records": len(records), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
