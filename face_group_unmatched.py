#!/usr/bin/env python3
"""Report-only face grouping for generic/unmatched picorg clusters.

This intentionally does not move files or update the picorg registry. It uses
the photo_reorg high-accuracy database to produce candidates that can later be
reviewed and converted into verified aliases or apply decisions.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import face_recognition
import numpy as np


DEFAULT_DB = Path("/opt/photo_reorg/data/high_accuracy_faces.db")
DEFAULT_AUDIT = Path("/tmp/picorg_periodic_apply.json")
DEFAULT_OUTPUT = Path("/tmp/picorg_face_grouping.json")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def load_references(db_path: Path) -> dict[str, np.ndarray]:
    grouped: dict[str, list[np.ndarray]] = defaultdict(list)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT person_name, encoding FROM face_encodings")
        for person_name, blob in rows:
            vector = np.frombuffer(blob, dtype=np.float64)
            if vector.shape == (128,):
                grouped[str(person_name)].append(vector)
    return {person: np.vstack(vectors) for person, vectors in grouped.items()}


def rank_candidates(encoding: np.ndarray, references: dict[str, np.ndarray]) -> list[dict[str, object]]:
    ranked = []
    for person, vectors in references.items():
        distance = float(np.min(np.linalg.norm(vectors - encoding, axis=1)))
        ranked.append({"person": person, "distance": round(distance, 5)})
    return sorted(ranked, key=lambda item: item["distance"])


def face_quality(location: tuple[int, int, int, int], shape: tuple[int, ...]) -> dict[str, float]:
    top, right, bottom, left = location
    height = max(0, bottom - top)
    width = max(0, right - left)
    image_area = max(1, int(shape[0]) * int(shape[1]))
    return {
        "width": float(width),
        "height": float(height),
        "min_dimension": float(min(width, height)),
        "area_ratio": round((width * height) / image_area, 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--limit-per-cluster", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=0.48)
    parser.add_argument("--margin", type=float, default=0.04)
    parser.add_argument("--min-face-pixels", type=int, default=80)
    parser.add_argument("--min-face-area-ratio", type=float, default=0.005)
    parser.add_argument("--num-jitters", type=int, default=1)
    parser.add_argument("--allow-multi-face", action="store_true")
    args = parser.parse_args()

    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    unmatched = [
        item for item in audit.get("results", [])
        if item.get("rule") == "unmatched"
        and Path(item["path"]).suffix.lower() in IMAGE_SUFFIXES
        and Path(item["path"]).is_file()
    ]

    # Sample each generic title cluster first so one repeated gallery cannot
    # consume the entire run. Set --limit-per-cluster 0 for all files.
    clusters: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in unmatched:
        clusters[str(item.get("title") or Path(item["path"]).stem)].append(item)
    selected = []
    for items in clusters.values():
        selected.extend(items if args.limit_per_cluster <= 0 else items[:args.limit_per_cluster])
    selected.sort(key=lambda item: str(item["path"]))
    total_selected = len(selected)
    if args.offset < 0 or args.offset > total_selected:
        parser.error(f"--offset must be between 0 and {total_selected}")
    selected = selected[args.offset:]
    selected = selected if args.max_files <= 0 else selected[:args.max_files]

    references = load_references(args.db)
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    cluster_matches: dict[str, list[str]] = defaultdict(list)
    results = []
    for index, item in enumerate(selected, 1):
        path = Path(item["path"])
        try:
            image = face_recognition.load_image_file(str(path))
            locations = face_recognition.face_locations(image, model="small")
        except Exception as exc:  # corrupt/unsupported files remain reviewable
            results.append({"path": str(path), "status": "error", "error": str(exc)})
            continue
        if not locations:
            results.append({"path": str(path), "status": "no_face"})
            continue
        if len(locations) > 1 and not args.allow_multi_face:
            results.append({
                "path": str(path),
                "status": "multi_face_deferred",
                "face_count": len(locations),
            })
            continue
        qualities = [face_quality(location, image.shape) for location in locations]
        usable = [
            index for index, quality in enumerate(qualities)
            if quality["min_dimension"] >= args.min_face_pixels
            and quality["area_ratio"] >= args.min_face_area_ratio
        ]
        if not usable:
            results.append({
                "path": str(path),
                "status": "low_quality",
                "face_count": len(locations),
                "quality": qualities,
            })
            continue
        encodings = face_recognition.face_encodings(
            image,
            known_face_locations=[locations[index] for index in usable],
            num_jitters=max(1, args.num_jitters),
            model="small",
        )
        if not encodings:
            results.append({"path": str(path), "status": "encoding_failed"})
            continue
        face_candidates = [rank_candidates(encoding, references) for encoding in encodings]
        # One-face mode is the default. In multi-face mode, preserve all face
        # candidates but require consensus before a person-level grouping.
        candidates = face_candidates[0]
        best = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        confident = bool(
            best
            and float(best["distance"]) <= args.threshold
            and (second is None or float(second["distance"]) - float(best["distance"]) >= args.margin)
        )
        result = {
            "path": str(path),
            "status": "matched" if confident else "ambiguous",
            "face_count": len(locations),
            "quality": qualities,
            "candidates": candidates[:5],
        }
        results.append(result)
        if confident:
            groups[str(best["person"])].append(result)
            cluster_matches[str(item.get("title") or path.stem)].append(str(best["person"]))
        if index % 100 == 0:
            print(f"processed={index}/{len(selected)}", flush=True)

    payload = {
        "audit": str(args.audit),
        "database": str(args.db),
        "threshold": args.threshold,
        "margin": args.margin,
        "references": len(references),
        "unmatched_available": len(unmatched),
        "processed": len(selected),
        "offset": args.offset,
        "next_offset": args.offset + len(selected),
        "total_selected": total_selected,
        "matched": sum(1 for item in results if item["status"] == "matched"),
        "ambiguous": sum(1 for item in results if item["status"] == "ambiguous"),
        "no_face": sum(1 for item in results if item["status"] == "no_face"),
        "groups": {person: items for person, items in sorted(groups.items())},
        "cluster_consensus": {
            cluster: {
                "images": len(people),
                "identities": {person: people.count(person) for person in sorted(set(people))},
                "consensus": len(set(people)) == 1 and len(people) >= 3,
            }
            for cluster, people in sorted(cluster_matches.items())
        },
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if args.checkpoint:
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        args.checkpoint.write_text(
            json.dumps({
                "audit": str(args.audit),
                "next_offset": payload["next_offset"],
                "total_selected": total_selected,
                "output": str(args.output),
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps({key: payload[key] for key in ("references", "unmatched_available", "processed", "matched", "ambiguous", "no_face")}, sort_keys=True))
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
