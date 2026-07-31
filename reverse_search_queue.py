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
LOW_SIGNAL_TITLE_TOKENS = {"img", "fb", "rdt", "good", "morning", "mommy", "goddess", "daddy", "sexy", "hot"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def queue_items(audit: Dict[str, object], limit: int, per_gallery: int) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    seen_hashes = set()
    candidates: List[Dict[str, object]] = []
    gallery_counts = {
        str(item.get("base_title")): int(item.get("count", 0))
        for item in audit.get("gallery_sets", [])
        if isinstance(item, dict) and item.get("identity") == "unmatched"
    }
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
        title = str(result.get("title") or title_from_path(source))
        words = [word for word in title.lower().split() if word.isalpha()]
        title_key = gallery_base_title(title)
        title_key_lower = title_key.lower()
        source_label = None
        if title_key_lower == "rdt":
            source_label = "reddit_download"
        elif title_key_lower == "fb img" or title_key_lower.startswith("fb img "):
            source_label = "facebook_download"
        priority = gallery_counts.get(title_key, 1) * 10
        priority += min(len(words), 5) * 3
        priority += min(len(title), 80) // 20
        priority -= sum(token in LOW_SIGNAL_TITLE_TOKENS for token in words) * 8
        meaningful_words = [word for word in words if word not in LOW_SIGNAL_TITLE_TOKENS and len(word) >= 3]
        if not meaningful_words or all(word.isdigit() for word in meaningful_words):
            priority -= 5000
        candidates.append(
            {
                "path": str(source),
                "title": title,
                "gallery_key": gallery_base_title(title),
                "source_label": source_label,
                "priority": priority,
                "source_root": result.get("source_root"),
                "source_family": result.get("source_family"),
                "status": "pending_review",
                "candidate_identity": None,
                "profile_url": None,
                "evidence_urls": [],
                "notes": "Do not upload without explicit permission; candidate requires independent corroboration.",
            }
        )
    candidates.sort(key=lambda item: (-int(item["priority"]), str(item["path"])))
    hash_budget = max(limit * 5, limit)
    selected_galleries = set()
    gallery_counts_selected: Dict[str, int] = {}
    for candidate in candidates[:hash_budget]:
        gallery_key = str(candidate["gallery_key"])
        if gallery_counts_selected.get(gallery_key, 0) >= per_gallery:
            continue
        source = Path(str(candidate["path"]))
        try:
            digest = file_sha256(source)
        except OSError:
            continue
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        candidate["sha256"] = digest
        items.append(candidate)
        selected_galleries.add(gallery_key)
        gallery_counts_selected[gallery_key] = gallery_counts_selected.get(gallery_key, 0) + 1
        if len(items) >= limit:
            break
    if len(items) < limit:
        for candidate in candidates[hash_budget:]:
            gallery_key = str(candidate["gallery_key"])
            if gallery_counts_selected.get(gallery_key, 0) >= per_gallery:
                continue
            try:
                digest = file_sha256(Path(str(candidate["path"])))
            except OSError:
                continue
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            candidate["sha256"] = digest
            items.append(candidate)
            gallery_counts_selected[gallery_key] = gallery_counts_selected.get(gallery_key, 0) + 1
            if len(items) >= limit:
                break
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue unmatched images for manual reverse-image review")
    parser.add_argument("audit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--per-gallery", type=int, default=3)
    args = parser.parse_args()
    payload = json.loads(args.audit.read_text(encoding="utf-8"))
    audit = payload.get("report", payload) if isinstance(payload, dict) else {}
    results = payload.get("results", []) if isinstance(payload, dict) else []
    queue = queue_items(
        {"results": results, "gallery_sets": audit.get("gallery_sets", [])},
        max(0, args.limit),
        max(1, args.per_gallery),
    )
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
