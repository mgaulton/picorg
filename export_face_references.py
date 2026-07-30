#!/usr/bin/env python3
"""Stage high-confidence picorg matches as photo_reorg face references.

The source media is never moved or copied. References are symlinks in a
temporary tree consumed by the face-database builder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path


DEFAULT_BLOCKED = {
    "ass", "boobs", "boots", "cum", "cosplay", "goth", "lingerie",
    "nipples", "panties", "redheads", "selfie", "selfies", "sex", "sexy",
    "tittyfuck", "upskirt", "videos", "wet",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audit", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/picorg_face_references"))
    parser.add_argument("--per-identity", type=int, default=20)
    parser.add_argument("--min-confidence", type=float, default=0.98)
    parser.add_argument("--include-contains", action="store_true")
    parser.add_argument("--min-files", type=int, default=3)
    args = parser.parse_args()

    payload = json.loads(args.audit.read_text())
    blocked = set(DEFAULT_BLOCKED)
    registry = Path(__file__).with_name("project_registry.json")
    if registry.exists():
        blocked.update(json.loads(registry.read_text()).get("blocked_tokens", []))

    grouped = defaultdict(set)
    for result in payload["results"]:
        family = result.get("family")
        canonical = result.get("canonical")
        source = Path(result["path"])
        rule = result.get("rule", "")
        if (
            not family or not canonical
            or source.suffix.lower() not in IMAGE_EXTENSIONS
            or result.get("confidence", 0.0) < args.min_confidence
            or not (rule.startswith("exact:") or (args.include_contains and rule == "contains:canonical"))
            or canonical.lower() in blocked
        ):
            continue
        if source.is_file():
            grouped[(family, canonical)].add(source)
        else:
            # After picorg apply, matched files live under the canonical
            # destination rather than their original intake path.
            fallback_dir = Path("/mnt/elements16/@mixedpics_sorted") / family / canonical
            if fallback_dir.is_dir():
                grouped[(family, canonical)].update(
                    item for item in fallback_dir.rglob("*")
                    if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
                )

    created = 0
    identities = 0
    args.output.mkdir(parents=True, exist_ok=True)
    for (family, canonical), sources in sorted(grouped.items()):
        if len(sources) < args.min_files:
            continue
        target_dir = args.output / f"{safe_name(family)}__{safe_name(canonical)}"
        target_dir.mkdir(parents=True, exist_ok=True)
        identities += 1
        for index, source in enumerate(sorted(sources)[: args.per_identity]):
            target = target_dir / f"{index:04d}_{safe_name(source.name)}"
            if not target.exists():
                os.symlink(source, target)
                created += 1

    print(json.dumps({"identities": identities, "references": created, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
