#!/usr/bin/env python3
"""Report whether confirmed face markers are durable enough for reuse.

This is intentionally report-only. Markers created before the review UI began
capturing hashes may have moved paths; they must not be silently relinked by a
basename guess because that can contaminate identity evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_markers(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    markers = payload.get("markers", []) if isinstance(payload, dict) else []
    confirmed = [item for item in markers if isinstance(item, dict) and item.get("status") == "confirmed"]
    with_hash = [item for item in confirmed if item.get("sha256")]
    existing = [item for item in confirmed if Path(str(item.get("path") or "")).is_file()]
    valid_hashes = 0
    invalid_hashes = 0
    for marker in with_hash:
        marker_path = Path(str(marker.get("path") or ""))
        if not marker_path.is_file():
            continue
        try:
            if _sha256(marker_path) == str(marker["sha256"]):
                valid_hashes += 1
            else:
                invalid_hashes += 1
        except OSError:
            invalid_hashes += 1
    identities = Counter(str(item.get("identity") or "") for item in confirmed)
    return {
        "markers": len(markers),
        "confirmed": len(confirmed),
        "confirmed_with_sha256": len(with_hash),
        "confirmed_paths_existing": len(existing),
        "valid_existing_sha256": valid_hashes,
        "invalid_existing_sha256": invalid_hashes,
        "missing_or_unhashed": len(confirmed) - len(with_hash),
        "identities": len([name for name in identities if name]),
        "top_identities": identities.most_common(20),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markers", type=Path, default=Path("/opt/picorg/identity_face_markers.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_markers(args.markers)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
