#!/usr/bin/env python3
"""Render a bounded reverse-search queue as a labeled local contact sheet."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a contact sheet from a reverse-search queue")
    parser.add_argument("queue", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--columns", type=int, default=4)
    args = parser.parse_args()
    if shutil.which("magick") is None:
        parser.error("ImageMagick 'magick' is required")

    payload = json.loads(args.queue.read_text(encoding="utf-8"))
    items = payload.get("items", [])[: max(0, args.limit)]
    command = ["magick", "montage"]
    accepted = 0
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("path") or ""))
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        identified = subprocess.run(
            ["magick", "identify", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if identified.returncode != 0:
            skipped += 1
            continue
        label = str(item.get("source_label") or "named")
        title = str(item.get("title") or path.name).replace("%", "%%")[:80]
        command.extend([str(path), "-label", f"{label}: {title}"])
        accepted += 1
    if not accepted:
        parser.error("queue contains no readable image paths")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    command.extend(
        [
            "-auto-orient",
            "-thumbnail",
            "320x320",
            "-tile",
            f"{max(1, args.columns)}x",
            "-geometry",
            "320x360+8+8",
            "-background",
            "white",
            str(args.output),
        ]
    )
    subprocess.run(command, check=True)
    print(json.dumps({"accepted": accepted, "skipped": skipped, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
