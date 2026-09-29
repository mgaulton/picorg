#!/usr/bin/env python3
"""Bounded health checks for PicOrg media roots.

This only stats and opens each root; it never walks media or changes files.
Callers can therefore process healthy intake roots during a partial mount
outage while refusing destructive priority-dedupe actions when protected roots
are unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def is_read_only_mount(path: Path) -> bool:
    """Return the mount's read-only flag using the kernel mount table."""
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    target = path.resolve()
    best_length = -1
    read_only = False
    for line in mounts:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            mountpoint = Path(fields[1].replace("\\040", " ")).resolve()
        except OSError:
            continue
        if target == mountpoint or mountpoint in target.parents:
            if len(str(mountpoint)) > best_length:
                best_length = len(str(mountpoint))
                read_only = "ro" in fields[3].split(",")
    return read_only


def check_root(root: Path, *, require_writable: bool = False) -> dict[str, str | bool]:
    try:
        if not root.exists():
            return {"path": str(root), "healthy": False, "reason": "missing"}
        if not root.is_dir():
            return {"path": str(root), "healthy": False, "reason": "not_directory"}
        with os.scandir(root) as entries:
            next(entries, None)
        if require_writable and is_read_only_mount(root):
            return {"path": str(root), "healthy": False, "reason": "read_only_mount"}
        return {"path": str(root), "healthy": True, "reason": "ok"}
    except OSError as exc:
        return {"path": str(root), "healthy": False, "reason": f"{type(exc).__name__}: {exc}"}


def health_report(roots: Iterable[Path], *, require_writable: bool = False) -> dict[str, object]:
    records = [check_root(root, require_writable=require_writable) for root in roots]
    healthy = [str(item["path"]) for item in records if item["healthy"]]
    unavailable = [item for item in records if not item["healthy"]]
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "healthy": len(unavailable) == 0,
        "partial": bool(healthy) and bool(unavailable),
        "records": records,
        "healthy_paths": healthy,
        "unavailable_paths": [str(item["path"]) for item in unavailable],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--healthy-paths", action="store_true")
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument("--require-writable", action="store_true", help="treat read-only mounts as unavailable")
    args = parser.parse_args()
    report = health_report(args.root, require_writable=args.require_writable)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    if args.healthy_paths:
        print("\n".join(str(path) for path in report["healthy_paths"]))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_all and not report["healthy"]:
        return 75
    if not report["healthy_paths"]:
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
