#!/usr/bin/env python3
"""Benchmark UniFace embeddings against locally labeled face pairs.

This is deliberately an opt-in benchmark. It never uploads images and does not
change the organizer's production embedding cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from face_match_benchmark import wilson_interval


def _normalise(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    return vector / max(float(np.linalg.norm(vector)), 1e-12)


def _single_face(faces):
    """Return a face only when detection is unambiguous."""
    return faces[0] if len(faces) == 1 else None


def _extractor(backend: str):
    if backend == "arcface":
        from uniface import FaceAnalyzer

        analyzer = FaceAnalyzer()

        def extract(image: np.ndarray) -> np.ndarray | None:
            faces = analyzer.analyze(image)
            # Pair benchmarks must use the same safety policy as production:
            # never choose an arbitrary face from a group photo.
            face = _single_face(faces)
            if face is None:
                return None
            return _normalise(face.embedding)

        return extract

    from uniface.detection import SCRFD
    from uniface.recognition import AdaFace

    detector = SCRFD()
    recognizer = AdaFace()

    def extract(image: np.ndarray) -> np.ndarray | None:
        faces = detector.detect(image)
        face = _single_face(faces)
        if face is None:
            return None
        return _normalise(recognizer.get_normalized_embedding(image, face.landmarks))

    return extract


def _build_name_index(roots: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        index[path.name].append(path)
                except OSError:
                    continue
        except OSError:
            continue
    return dict(index)


def run(
    pairs_path: Path,
    backend: str,
    thresholds: list[float],
    license_status: str = "unverified",
    search_roots: list[Path] | None = None,
) -> dict:
    pairs_raw = pairs_path.read_bytes()
    pairs = json.loads(pairs_raw)
    paths = sorted({path for pair in pairs for path in (pair["path_a"], pair["path_b"])})
    extract = _extractor(backend)
    embeddings: dict[str, np.ndarray] = {}
    status_counts: dict[str, int] = {}
    name_index = _build_name_index(search_roots or [])
    relinked = 0
    ambiguous = 0
    started = time.monotonic()
    for raw_path in paths:
        try:
            source = Path(raw_path)
            if not source.is_file():
                candidates = name_index.get(source.name, [])
                if len(candidates) == 1:
                    source = candidates[0]
                    relinked += 1
                elif len(candidates) > 1:
                    status_counts["ambiguous"] = status_counts.get("ambiguous", 0) + 1
                    ambiguous += 1
                    continue
                else:
                    status_counts["missing"] = status_counts.get("missing", 0) + 1
                    continue
            image = cv2.imread(str(source))
            if image is None:
                status_counts["image_unreadable"] = status_counts.get("image_unreadable", 0) + 1
                continue
            vector = extract(image)
            if vector is None:
                status_counts["no_face"] = status_counts.get("no_face", 0) + 1
            else:
                embeddings[raw_path] = vector
        except Exception as exc:  # keep a corrupt input from aborting the report
            key = type(exc).__name__
            status_counts[key] = status_counts.get(key, 0) + 1

    scores = [
        (float(np.dot(embeddings[pair["path_a"]], embeddings[pair["path_b"]])), pair["label"])
        for pair in pairs
        if pair["path_a"] in embeddings and pair["path_b"] in embeddings
    ]
    genuine = [score for score, label in scores if label == "genuine"]
    impostor = [score for score, label in scores if label == "impostor"]
    operating_points = []
    for threshold in thresholds:
        true_positive_rate = sum(score >= threshold for score in genuine) / len(genuine) if genuine else 0.0
        false_match_rate = sum(score >= threshold for score in impostor) / len(impostor) if impostor else 0.0
        operating_points.append({
            "threshold": threshold,
            "tpr": round(true_positive_rate, 6),
            "fnmr": round(1.0 - true_positive_rate, 6),
            "fmr": round(false_match_rate, 6),
            "fnmr_ci95": wilson_interval(
                sum(score < threshold for score in genuine), len(genuine)
            ),
            "fmr_ci95": wilson_interval(
                sum(score >= threshold for score in impostor), len(impostor)
            ),
        })
    ranges = {}
    for label, values in (("genuine", genuine), ("impostor", impostor)):
        if values:
            ranges[label] = {"min": round(min(values), 6), "max": round(max(values), 6)}
    return {
        "backend": backend,
        "model_id": f"uniface-{backend}",
        "model_license_status": license_status,
        "pairs_sha256": hashlib.sha256(pairs_raw).hexdigest(),
        "pairs": len(pairs),
        "input_images": len(paths),
        "embedded_images": len(embeddings),
        "scored_pairs": len(scores),
        "coverage": round(len(scores) / len(pairs), 6) if pairs else 0.0,
        "relinked_images": relinked,
        "ambiguous_images": ambiguous,
        "genuine_pairs": len(genuine),
        "impostor_pairs": len(impostor),
        "status_counts": status_counts,
        "score_ranges": ranges,
        "operating_points": operating_points,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "warning": "Small local pair sets are not production accuracy evidence; calibrate on held-out data.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--backend", choices=("arcface", "adaface"), default="arcface")
    parser.add_argument("--threshold", type=float, action="append", dest="thresholds")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--search-root", type=Path, action="append", default=[], help="optional root(s) for unique-basename relinking of moved files")
    parser.add_argument("--model-license-status", default="unverified", choices=("unverified", "research-only", "approved"))
    args = parser.parse_args()
    thresholds = args.thresholds or [0.30, 0.40, 0.45, 0.50, 0.55, 0.60]
    if any(not -1.0 <= threshold <= 1.0 for threshold in thresholds):
        parser.error("--threshold must be between -1 and 1")
    report = run(args.pairs, args.backend, thresholds, args.model_license_status, args.search_root)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not report["genuine_pairs"] or not report["impostor_pairs"]:
        print("benchmark invalid: requires at least one scored genuine and impostor pair", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
