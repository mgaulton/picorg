#!/usr/bin/env python3
"""Classify unmatched audit paths before face extraction.

The report is non-destructive and separates input coverage problems from model
accuracy. It does not open, move, hash, or upload media.
"""

from __future__ import annotations

import argparse
import json
import re
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
DEFAULT_MAX_PIXELS = 89_478_485
THUMBNAIL_PATTERN = re.compile(r"(?:^|[_ .-])(?:thumb(?:nail)?|preview|teaser|lowres|small|\d{2,4}px|\d{2,4}x\d{2,4})(?:$|[_ .-])", re.I)


def classify_path(raw_path: str, verify_image: bool = False, max_pixels: int = DEFAULT_MAX_PIXELS, skip_paths: set[str] | None = None) -> str:
    if skip_paths and raw_path in skip_paths:
        return "skipped"
    path = Path(raw_path)
    if THUMBNAIL_PATTERN.search(path.stem):
        return "thumbnail"
    try:
        if not path.exists():
            return "missing"
        if not path.is_file():
            return "not_file"
    except OSError:
        # Degraded/FUSE mounts can raise EIO from exists/is_file. Keep the
        # audit complete and let operators quarantine the exact path.
        return "unreadable"
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return "unsupported_extension"
    try:
        if path.stat().st_size == 0:
            return "empty"
    except OSError:
        return "unreadable"
    if verify_image:
        try:
            from PIL import Image
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    width, height = image.size
                    if width * height > max_pixels:
                        return "oversized"
                    image.verify()
        except Image.DecompressionBombWarning:
            return "oversized"
        except Exception:
            return "corrupt"
    return "candidate"


def load_skip_paths(path: Path | None) -> set[str]:
    if not path:
        return set()
    try:
        if not path.is_file():
            return set()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    values = payload.get("paths", []) if isinstance(payload, dict) else payload
    return {str(value) for value in values if isinstance(value, str)}


def preflight(audit_path: Path, verify_image: bool = False, max_pixels: int = DEFAULT_MAX_PIXELS, skip_paths: Path | None = None) -> dict[str, Any]:
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    records = []
    counts: Counter[str] = Counter()
    skipped = load_skip_paths(skip_paths)
    for item in results:
        if not isinstance(item, dict) or item.get("canonical") or not item.get("path"):
            continue
        path = str(item["path"])
        status = classify_path(path, verify_image, max_pixels, skipped)
        counts[status] += 1
        records.append({"path": path, "status": status})
    return {
        "schema_version": 1,
        "source_audit": str(audit_path),
        "total_unmatched": len(records),
        "counts": dict(sorted(counts.items())),
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-images", action="store_true")
    parser.add_argument("--max-pixels", type=int, default=DEFAULT_MAX_PIXELS)
    parser.add_argument("--skip-paths", type=Path, help="JSON file containing paths to exclude before filesystem access")
    args = parser.parse_args()
    report = preflight(args.audit, args.verify_images, args.max_pixels, args.skip_paths)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
