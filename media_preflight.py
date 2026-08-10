#!/usr/bin/env python3
"""Classify unmatched audit paths before face extraction.

The report is non-destructive and separates input coverage problems from model
accuracy. It does not open, move, hash, or upload media.
"""

from __future__ import annotations

import argparse
import json
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
DEFAULT_MAX_PIXELS = 89_478_485


def classify_path(raw_path: str, verify_image: bool = False, max_pixels: int = DEFAULT_MAX_PIXELS) -> str:
    path = Path(raw_path)
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "not_file"
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


def preflight(audit_path: Path, verify_image: bool = False, max_pixels: int = DEFAULT_MAX_PIXELS) -> dict[str, Any]:
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    records = []
    counts: Counter[str] = Counter()
    for item in results:
        if not isinstance(item, dict) or item.get("canonical") or not item.get("path"):
            continue
        path = str(item["path"])
        status = classify_path(path, verify_image, max_pixels)
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
    args = parser.parse_args()
    report = preflight(args.audit, args.verify_images, args.max_pixels)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
