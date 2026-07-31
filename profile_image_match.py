#!/usr/bin/env python3
"""Report exact normalized-image matches against user-supplied references.

This tool never downloads URLs, contacts platforms, or moves files. Put
publicly obtained reference images under ``<family>__<canonical>/`` (the
layout produced by ``export_face_references.py``), then compare an intake
root against that local reference set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def iter_images(root: Path) -> Iterable[Path]:
    if root.is_file() and root.suffix.lower() in IMAGE_EXTENSIONS:
        yield root
        return
    if root.is_dir():
        yield from (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_hash(path: Path) -> str:
    command = [
        "magick", str(path), "-auto-orient", "-strip", "-resize", "256x256^",
        "-gravity", "center", "-extent", "256x256", "png:-",
    ]
    completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return hashlib.sha256(completed.stdout).hexdigest()


def identity_for(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path.parent.name
    if relative.parts:
        return relative.parts[0]
    return path.parent.name


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare local images with offline profile references")
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if shutil.which("magick") is None:
        parser.error("ImageMagick 'magick' is required for normalized image fingerprints")

    references: Dict[str, List[Dict[str, str]]] = {}
    failures: List[Dict[str, str]] = []
    for path in iter_images(args.references):
        try:
            fingerprint = normalized_hash(path)
            raw = sha256(path)
        except (OSError, subprocess.CalledProcessError) as exc:
            failures.append({"path": str(path), "error": str(exc)})
            continue
        references.setdefault(fingerprint, []).append(
            {"path": str(path), "identity": identity_for(path, args.references), "sha256": raw}
        )

    matches: List[Dict[str, object]] = []
    scanned = 0
    for root in args.root:
        for path in iter_images(root):
            scanned += 1
            try:
                fingerprint = normalized_hash(path)
            except (OSError, subprocess.CalledProcessError) as exc:
                failures.append({"path": str(path), "error": str(exc)})
                continue
            hits = references.get(fingerprint, [])
            if hits:
                matches.append(
                    {
                        "path": str(path),
                        "normalized_hash": fingerprint,
                        "confidence": 1.0,
                        "rule": "normalized-image-hash",
                        "references": hits,
                    }
                )

    payload = {
        "schema_version": 1,
        "references": sum(len(items) for items in references.values()),
        "scanned": scanned,
        "matches": len(matches),
        "failures": failures,
        "results": matches,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("references", "scanned", "matches", "failures")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
