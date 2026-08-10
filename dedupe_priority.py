#!/usr/bin/env python3
"""Find exact duplicate files while preserving priority-root copies.

Priority roots are read-only. Target duplicates are report-only by default;
``--apply`` moves them into a quarantine tree rather than deleting them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def iter_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def build_report(priority_roots: list[Path], target_roots: list[Path], cache_path: Path | None = None) -> dict[str, Any]:
    cache: dict[str, dict[str, Any]] = {}
    if cache_path and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")).get("files", {})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cache = {}

    def cached_digest(path: Path) -> str | None:
        try:
            stat = path.stat()
            old = cache.get(str(path))
            if old and old.get("size") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns:
                return str(old["sha256"])
            value = digest(path)
            cache[str(path)] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": value}
            return value
        except (OSError, KeyError, TypeError, ValueError):
            return None

    owners: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    scanned = 0
    for root in priority_roots:
        for path in iter_files(root):
            scanned += 1
            try:
                key = cached_digest(path)
            except OSError:
                continue
            if not key:
                continue
            owners.setdefault(key, str(path))
    for root in target_roots:
        for path in iter_files(root):
            scanned += 1
            try:
                key = cached_digest(path)
            except OSError:
                continue
            if not key:
                continue
            owner = owners.get(key)
            if owner:
                duplicates.append({"path": str(path), "duplicate_of": owner, "sha256": key, "root": str(root)})
            else:
                owners[key] = str(path)
    report = {
        "schema_version": 1,
        "priority_roots": [str(root) for root in priority_roots],
        "target_roots": [str(root) for root in target_roots],
        "scanned_files": scanned,
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "hash_cache_entries": len(cache),
    }
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"schema_version": 1, "files": cache}, sort_keys=True) + "\n", encoding="utf-8")
    return report


def quarantine(report: dict[str, Any], quarantine_root: Path) -> int:
    moved = 0
    for item in report["duplicates"]:
        source = Path(item["path"])
        root = Path(item["root"])
        try:
            relative = source.relative_to(root)
        except ValueError:
            relative = Path(source.name)
        destination = quarantine_root / root.name / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}__{item['sha256'][:12]}{destination.suffix}")
        shutil.move(str(source), str(destination))
        item["quarantined_to"] = str(destination)
        moved += 1
    report["quarantined_count"] = moved
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--priority-root", action="append", type=Path)
    parser.add_argument("--target-root", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".cache/picorg/priority-dedupe.json"))
    parser.add_argument("--quarantine-root", type=Path, default=Path(".cache/picorg/priority-dedupe-quarantine"))
    parser.add_argument("--cache", type=Path, default=Path(".cache/picorg/priority-dedupe-hashes.json"))
    parser.add_argument("--apply", action="store_true", help="move target duplicates to quarantine; never modify priority roots")
    args = parser.parse_args()
    priority_roots = args.priority_root or [Path("/mnt/elements16a/Pron/redditdaily"), Path("/mnt/elements16a/Pron/metadaily")]
    target_roots = args.target_root or [Path("/mnt/elements16/@mixedpics"), Path("/mnt/desktop/Pictures")]
    report = build_report(priority_roots, target_roots, args.cache)
    if args.apply:
        quarantine(report, args.quarantine_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("scanned_files", "duplicate_count", "quarantined_count") if key in report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
