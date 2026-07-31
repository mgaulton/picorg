#!/usr/bin/env python3
"""Export unmatched local images for deliberate reverse-image review.

The queue contains paths and hashes only. It never uploads images, opens a
browser, contacts a search provider, or changes the intake tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List

from picorg_sorter import gallery_base_title, title_from_path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def queue_items(audit: Dict[str, object], limit: int) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    seen_hashes = set()
    for result in audit.get("results", []):
        if not isinstance(result, dict) or result.get("canonical"):
            continue
        source = Path(str(result.get("path") or ""))
        try:
            is_file = source.is_file()
        except OSError:
            is_file = False
        if source.suffix.lower() not in IMAGE_EXTENSIONS or not is_file:
            continue
        try:
            digest = file_sha256(source)
        except OSError:
            continue
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        title = str(result.get("title") or title_from_path(source))
        items.append(
            {
                "path": str(source),
                "sha256": digest,
                "title": title,
                "gallery_key": gallery_base_title(title),
                "source_root": result.get("source_root"),
                "source_family": result.get("source_family"),
                "status": "pending_review",
                "candidate_identity": None,
                "profile_url": None,
                "evidence_urls": [],
                "notes": "Do not upload without explicit permission; candidate requires independent corroboration.",
            }
        )
        if len(items) >= limit:
            break
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue unmatched images for manual reverse-image review")
    parser.add_argument("audit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    payload = json.loads(args.audit.read_text(encoding="utf-8"))
    audit = payload.get("report", payload) if isinstance(payload, dict) else {}
    results = payload.get("results", []) if isinstance(payload, dict) else []
    queue = queue_items({"results": results}, max(0, args.limit))
    output = {
        "schema_version": 1,
        "source_audit": str(args.audit),
        "privacy": "paths and hashes only; no image upload or provider request is performed",
        "scanned": audit.get("scanned"),
        "queued": len(queue),
        "items": queue,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"queued": len(queue), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
