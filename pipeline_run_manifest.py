#!/usr/bin/env python3
"""Write a compact, privacy-preserving manifest for one pipeline audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", default="dlib")
    args = parser.parse_args()
    raw = args.audit.read_bytes()
    audit = json.loads(raw)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audit_sha256": hashlib.sha256(raw).hexdigest(),
        "audit": str(args.audit),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "face_backend": args.backend,
        "scanned": audit.get("scanned"),
        "matched": audit.get("matched"),
        "high_confidence": audit.get("high_confidence"),
        "ground_truth_precision": audit.get("ground_truth_precision"),
        "ground_truth_recall": audit.get("ground_truth_recall"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(args.output), "audit_sha256": manifest["audit_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
