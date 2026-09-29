#!/usr/bin/env python3
"""Select a quality-diverse reference gallery without changing the face DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

import picorg_sorter as sorter


def _confirmed_paths(markers_path: Path | None, verify_hash: bool = False) -> set[str]:
    """Return existing confirmed targets with optional expensive hash checks."""
    if not markers_path or not markers_path.is_file():
        return set()
    try:
        payload = json.loads(markers_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    confirmed: set[str] = set()
    for marker in payload.get("markers", []) if isinstance(payload, dict) else []:
        if not isinstance(marker, dict) or marker.get("status") != "confirmed":
            continue
        raw_path = str(marker.get("path") or "")
        path = Path(raw_path)
        if verify_hash:
            # Strict resolution performs a filesystem stat for every marker;
            # keep it limited to the explicit integrity-check mode because
            # RD/MD mounts may be slow or temporarily unavailable.
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
        else:
            # The database stores absolute paths.  Preserve the marker path
            # without touching the source filesystem during normal gallery
            # selection; the decoder/preflight remains authoritative.
            resolved = Path(raw_path)
        expected = str(marker.get("sha256") or "").lower()
        if expected and verify_hash:
            digest = hashlib.sha256()
            try:
                with resolved.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
            if digest.hexdigest() != expected:
                continue
        elif not expected:
            # Unhashed legacy markers are useful for display but not strong
            # enough to promote an exemplar automatically.
            continue
        confirmed.add(str(resolved))
    return confirmed


def select_gallery(
    rows: list[tuple[str, float, np.ndarray]],
    limit: int,
    priority_paths: set[str] | None = None,
) -> list[tuple[str, float]]:
    if not rows or limit <= 0:
        return []
    priority_paths = priority_paths or set()
    def normalized(value: str) -> str:
        # Avoid Path.resolve() here: it performs filesystem I/O and can block
        # on an unavailable RD/MD/FUSE mount.  Gallery paths are stored as
        # absolute paths, so lexical normalization is sufficient.
        return os.path.normpath(value)

    priority_keys = {normalized(path) for path in priority_paths}
    ordered = sorted(
        rows,
        key=lambda row: (-int(normalized(row[0]) in priority_keys), -row[1], row[0]),
    )
    # Maintain the nearest-selected distance vector incrementally. This keeps
    # the quality/diversity policy deterministic without one Python norm call
    # per candidate (which becomes very slow on a large identity gallery).
    matrix = np.asarray([row[2] for row in ordered], dtype=np.float32)
    priority = np.asarray(
        [1.0 if normalized(row[0]) in priority_keys else 0.0 for row in ordered],
        dtype=np.float32,
    )
    quality = np.asarray([float(row[1]) for row in ordered], dtype=np.float32)
    selected_indices = [0]
    available = np.ones(len(ordered), dtype=bool)
    available[0] = False
    min_distance = np.linalg.norm(matrix - matrix[0], axis=1)
    while available.any() and len(selected_indices) < limit:
        score = min_distance + 0.10 * quality + priority
        score[~available] = -np.inf
        best_score = float(np.max(score))
        candidates = np.flatnonzero(available & (score == best_score))
        best_index = int(candidates[0])
        selected_indices.append(best_index)
        available[best_index] = False
        min_distance = np.minimum(min_distance, np.linalg.norm(matrix - matrix[best_index], axis=1))
    selected = [ordered[index] for index in selected_indices]
    return [(path, round(quality, 6)) for path, quality, _ in selected]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/opt/photo_reorg/data/high_accuracy_faces.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-per-person", type=int, default=12)
    parser.add_argument("--min-quality", type=float, default=0.0)
    parser.add_argument("--markers", type=Path, help="optional confirmed marker file; only markers with SHA-256 are eligible")
    parser.add_argument("--verify-markers", action="store_true", help="rehash confirmed marker files (slow; off by default)")
    args = parser.parse_args()
    if not 1 <= args.max_per_person:
        parser.error("--max-per-person must be positive")
    with sqlite3.connect(args.db) as conn:
        rows = conn.execute("SELECT person_name, image_path, face_quality, encoding FROM face_encodings").fetchall()
    grouped: dict[str, list[tuple[str, float, np.ndarray]]] = {}
    for person, path, quality, blob in rows:
        quality_value = float(quality or 0.0)
        vector = np.frombuffer(blob, dtype=np.float64)
        if vector.shape != (128,) or quality_value < args.min_quality:
            continue
        grouped.setdefault(str(person), []).append((str(path), quality_value, vector))
    confirmed = _confirmed_paths(args.markers, args.verify_markers)
    confirmed_keys = {os.path.normpath(item) for item in confirmed}
    selected = {person: select_gallery(items, args.max_per_person, confirmed) for person, items in grouped.items()}
    selected = {person: paths for person, paths in selected.items() if paths and not sorter.is_generic_identity_token(person.split("__", 1)[-1])}
    payload = {
        "schema_version": 1,
        "database": str(args.db),
        "max_per_person": args.max_per_person,
        "min_quality": args.min_quality,
        "confirmed_markers": str(args.markers) if args.markers else None,
        "confirmed_selected": sum(
            1 for paths in selected.values() for path, _ in paths if os.path.normpath(path) in confirmed_keys
        ),
        "identities": len(selected),
        "references": sum(len(paths) for paths in selected.values()),
        "gallery": dict(sorted(selected.items())),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("identities", "references")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
