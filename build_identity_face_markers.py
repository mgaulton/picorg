#!/usr/bin/env python3
"""Attach cached face embeddings to manually reviewed identity markers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markers", type=Path, default=Path("/opt/picorg/identity_face_markers.json"))
    parser.add_argument("--embeddings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    marker_payload = json.loads(args.markers.read_text(encoding="utf-8"))
    embedding_payload = json.loads(args.embeddings.read_text(encoding="utf-8"))
    records = embedding_payload.get("records", {})
    output: list[dict[str, Any]] = []
    skipped = 0
    for marker in marker_payload.get("markers", []):
        path = str(marker.get("path") or "")
        record = records.get(path) if isinstance(records, dict) else None
        embedding = record.get("embedding") if isinstance(record, dict) else None
        if marker.get("status") != "confirmed" or not embedding:
            skipped += 1
            continue
        if marker.get("sha256") and record.get("fingerprint") != marker["sha256"]:
            skipped += 1
            continue
        output.append({**marker, "embedding": embedding, "model": embedding_payload.get("model_id"), "detector": embedding_payload.get("detector")})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schema_version": 1, "markers": output}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"markers": len(output), "skipped": skipped, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
