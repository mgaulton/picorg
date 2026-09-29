#!/usr/bin/env python3
"""Validate a coalesced face-reference tree without following failures globally."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def validate(root: Path) -> dict[str, object]:
    identities = 0
    entries = 0
    readable = 0
    broken = 0
    errors: list[dict[str, str]] = []
    try:
        identity_dirs = list(root.iterdir())
    except OSError as exc:
        return {"root": str(root), "identities": 0, "entries": 0, "readable": 0,
                "broken": 0, "errors": [{"path": str(root), "error": str(exc)}]}
    for identity_dir in identity_dirs:
        if not identity_dir.is_dir() or identity_dir.name.startswith("."):
            continue
        identities += 1
        try:
            children = list(identity_dir.iterdir())
        except OSError as exc:
            errors.append({"path": str(identity_dir), "error": str(exc)})
            continue
        for child in children:
            if child.name.startswith(".") or not child.is_symlink():
                continue
            entries += 1
            try:
                if child.exists() and child.is_file() and os.access(child, os.R_OK):
                    readable += 1
                else:
                    broken += 1
            except OSError as exc:
                broken += 1
                errors.append({"path": str(child), "error": str(exc)})
    return {"root": str(root), "identities": identities, "entries": entries,
            "readable": readable, "broken": broken, "errors": errors[:100]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-identities", type=int, default=1)
    parser.add_argument("--min-readable", type=int, default=1)
    args = parser.parse_args()
    report = validate(args.root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("identities", "entries", "readable", "broken")}, sort_keys=True))
    if not args.root.is_dir():
        raise SystemExit(f"reference root is missing: {args.root}")
    if int(report["identities"]) < args.min_identities:
        raise SystemExit("reference gallery has too few identity folders")
    if int(report["readable"]) < args.min_readable:
        raise SystemExit("reference gallery has too few readable references")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
