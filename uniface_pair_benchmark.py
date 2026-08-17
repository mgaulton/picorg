#!/usr/bin/env python3
"""Benchmark UniFace embeddings against locally labeled face pairs.

This is deliberately an opt-in benchmark. It never uploads images and does not
change the organizer's production embedding cache.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np


def _normalise(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    return vector / max(float(np.linalg.norm(vector)), 1e-12)


def _extractor(backend: str):
    if backend == "arcface":
        from uniface import FaceAnalyzer

        analyzer = FaceAnalyzer()

        def extract(image: np.ndarray) -> np.ndarray | None:
            faces = analyzer.analyze(image)
            if not faces:
                return None
            face = max(faces, key=lambda item: float(item.confidence))
            return _normalise(face.embedding)

        return extract

    from uniface.detection import SCRFD
    from uniface.recognition import AdaFace

    detector = SCRFD()
    recognizer = AdaFace()

    def extract(image: np.ndarray) -> np.ndarray | None:
        faces = detector.detect(image, max_num=1)
        if not faces:
            return None
        return _normalise(recognizer.get_normalized_embedding(image, faces[0].landmarks))

    return extract


def run(pairs_path: Path, backend: str, thresholds: list[float]) -> dict:
    pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
    paths = sorted({path for pair in pairs for path in (pair["path_a"], pair["path_b"])})
    extract = _extractor(backend)
    embeddings: dict[str, np.ndarray] = {}
    status_counts: dict[str, int] = {}
    started = time.monotonic()
    for raw_path in paths:
        try:
            image = cv2.imread(raw_path)
            if image is None:
                raise ValueError("image_unreadable")
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
        })
    ranges = {}
    for label, values in (("genuine", genuine), ("impostor", impostor)):
        if values:
            ranges[label] = {"min": round(min(values), 6), "max": round(max(values), 6)}
    return {
        "backend": backend,
        "pairs": len(pairs),
        "input_images": len(paths),
        "embedded_images": len(embeddings),
        "scored_pairs": len(scores),
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
    args = parser.parse_args()
    thresholds = args.thresholds or [0.30, 0.40, 0.45, 0.50, 0.55, 0.60]
    if any(not -1.0 <= threshold <= 1.0 for threshold in thresholds):
        parser.error("--threshold must be between -1 and 1")
    report = run(args.pairs, args.backend, thresholds)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
