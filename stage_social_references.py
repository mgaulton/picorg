#!/usr/bin/env python3
"""Stage symlinked face references from intact social identity trees."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}


def safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/photo_reorg_social_references"))
    parser.add_argument("--per-identity", type=int, default=10)
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()

    shutil.rmtree(args.output, ignore_errors=True)
    args.output.mkdir(parents=True)
    identities = references = 0
    for root in args.roots:
        if not root.is_dir():
            continue
        source_name = safe(root.name)
        for identity_dir in sorted(item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")):
            images = sorted(
                item for item in identity_dir.rglob("*")
                if item.is_file() and item.suffix.lower() in EXTENSIONS
            )[: args.per_identity]
            if not images:
                continue
            target_dir = args.output / f"{source_name}__{safe(identity_dir.name)}"
            target_dir.mkdir()
            identities += 1
            for index, image in enumerate(images):
                os.symlink(image, target_dir / f"{index:04d}_{safe(image.name)}")
                references += 1

    print(f"identities={identities} references={references} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
