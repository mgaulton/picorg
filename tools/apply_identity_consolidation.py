#!/usr/bin/env python3
"""Resumeably move files according to a preflighted identity consolidation manifest."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def load_moves(manifest: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    root = Path(payload["source_root"]).resolve()
    moves = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    for row in payload["items"]:
        if row.get("status") == "unresolved_identity" or row["source"] == row["target"]:
            continue
        source = Path(row["source"])
        target = Path(row["target"])
        if not source.resolve(strict=False).is_relative_to(root) or not target.parent.resolve(strict=False).is_relative_to(root):
            raise ValueError(f"path escapes consolidation root: {source} -> {target}")
        # The source tree can contain distinct case-sensitive NTFS entries.
        # Do not fold source names; only the new shared targets must be unique
        # under Windows' usual case-insensitive lookup.
        source_key = os.path.normcase(os.path.normpath(str(source)))
        target_key = os.path.normcase(os.path.normpath(str(target))).casefold()
        if source_key in seen_sources or target_key in seen_targets:
            raise ValueError(f"case-insensitive path collision: {source} -> {target}")
        seen_sources.add(source_key)
        seen_targets.add(target_key)
        moves.append(row)
    return payload, moves


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="perform moves; default only checks readiness")
    args = parser.parse_args()
    payload, moves = load_moves(args.manifest)
    if not args.apply:
        print(json.dumps({"ready": True, "moves": len(moves), "mode": "preflight_only"}))
        return 0

    started = time.monotonic()
    complete = 0
    args.journal.parent.mkdir(parents=True, exist_ok=True)
    with args.journal.open("a", encoding="utf-8", buffering=1) as journal:
        for index, row in enumerate(moves, 1):
            source, target = Path(row["source"]), Path(row["target"])
            source_exists, target_exists = source.exists(), target.exists()
            if source_exists and target_exists:
                raise RuntimeError(f"both source and target exist: {source} -> {target}")
            if source_exists:
                if source.is_symlink() or not source.is_file() or source.stat().st_size != row["size_bytes"]:
                    raise RuntimeError(f"source changed since preflight: {source}")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
            elif target_exists:
                if target.is_symlink() or not target.is_file() or target.stat().st_size != row["size_bytes"]:
                    raise RuntimeError(f"previous target does not match manifest: {target}")
            else:
                raise RuntimeError(f"both source and target are absent: {source} -> {target}")
            complete += 1
            if index % 100 == 0 or index == len(moves):
                record = {"index": index, "total": len(moves), "source": str(source), "target": str(target),
                          "bytes": sum(x["size_bytes"] for x in moves[:index]), "elapsed_seconds": round(time.monotonic() - started, 1)}
                journal.write(json.dumps(record, sort_keys=True) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                print(json.dumps({"progress": index, "total": len(moves), "elapsed_seconds": record["elapsed_seconds"]}), flush=True)
    print(json.dumps({"complete": complete, "unresolved_left_in_place": sum(x.get("status") == "unresolved_identity" for x in payload["items"]),
                      "journal": str(args.journal)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
