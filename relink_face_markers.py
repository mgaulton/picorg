#!/usr/bin/env python3
"""Relink confirmed face markers using exact SHA-256 matches only.

This is deliberately non-destructive: it writes a new marker file and never
guesses by basename, title, or folder name. Existing marker paths are hashed
when possible; missing paths with a stored SHA are matched against the
provided roots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        for directory, _, names in os.walk(root, onerror=lambda _error: None):
            for name in names:
                path = Path(directory) / name
                try:
                    if path.is_file() and not path.is_symlink():
                        yield path
                except OSError:
                    continue


def relink(payload: dict[str, Any], roots: list[Path], hash_cache: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    markers = payload.get("markers", []) if isinstance(payload, dict) else []
    targets = {
        str(item.get("sha256")): item
        for item in markers
        if isinstance(item, dict) and item.get("status") == "confirmed" and item.get("sha256")
    }
    by_hash: dict[str, Path] = {}
    stats = {"markers": len(markers), "hashed": 0, "relinked": 0, "unresolved": 0, "invalid": 0, "read_errors": 0}
    cached_files = (hash_cache or {}).get("files", {}) if isinstance(hash_cache, dict) else {}
    for path in _files(roots):
        try:
            stat = path.stat()
            cached = cached_files.get(str(path))
            if (isinstance(cached, dict) and cached.get("mtime_ns") == stat.st_mtime_ns
                    and cached.get("size") == stat.st_size and cached.get("sha256")):
                digest = str(cached["sha256"])
            else:
                digest = _sha256(path)
        except OSError:
            stats["read_errors"] += 1
            continue
        if digest in targets and digest not in by_hash:
            by_hash[digest] = path

    output_markers: list[dict[str, Any]] = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        updated = dict(marker)
        path = Path(str(marker.get("path") or ""))
        digest = str(marker.get("sha256") or "")
        if path.is_file() and not path.is_symlink():
            try:
                stat = path.stat()
                cached = cached_files.get(str(path))
                if (isinstance(cached, dict) and cached.get("mtime_ns") == stat.st_mtime_ns
                        and cached.get("size") == stat.st_size and cached.get("sha256")):
                    actual = str(cached["sha256"])
                else:
                    actual = _sha256(path)
            except OSError:
                actual = ""
            if actual and digest and actual != digest:
                stats["invalid"] += 1
            elif actual:
                updated["sha256"] = actual
                stats["hashed"] += 1
        elif digest and digest in by_hash:
            updated["path"] = str(by_hash[digest])
            updated["relinked_from"] = str(path)
            stats["relinked"] += 1
        elif marker.get("status") == "confirmed":
            stats["unresolved"] += 1
        output_markers.append(updated)
    result = {"schema_version": 1, "updated": payload.get("updated"), "markers": output_markers}
    return result, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markers", type=Path, default=Path("/opt/picorg/identity_face_markers.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--hash-cache", type=Path, help="optional priority-dedupe hash cache to avoid re-reading unchanged files")
    args = parser.parse_args()
    payload = json.loads(args.markers.read_text(encoding="utf-8"))
    cache = json.loads(args.hash_cache.read_text(encoding="utf-8")) if args.hash_cache and args.hash_cache.is_file() else None
    result, stats = relink(payload, args.root, cache)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**stats, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
