#!/usr/bin/env python3
"""Score a labeled pair file with the optional InsightFace backend."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from face_match_benchmark import evaluate_pairs
from insightface_backend import InsightFaceBackend, MODEL_ID


def run(pairs_path: Path, max_fmr: float = 0.001) -> dict:
    pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
    paths = sorted({path for pair in pairs for path in (pair["path_a"], pair["path_b"])})
    backend = InsightFaceBackend(providers=["CPUExecutionProvider"])
    embeddings = {}
    statuses: dict[str, int] = {}
    started = time.monotonic()
    for raw_path in paths:
        try:
            vector, metadata = backend.embed(Path(raw_path))
            status = str(metadata.get("status", "unknown"))
            statuses[status] = statuses.get(status, 0) + 1
            if vector:
                embeddings[raw_path] = vector
        except Exception as exc:  # keep corrupt inputs in the benchmark report
            status = type(exc).__name__
            statuses[status] = statuses.get(status, 0) + 1
    report = evaluate_pairs(pairs, embeddings, max_fmr)
    report.update({
        "model_id": MODEL_ID,
        "input_images": len(paths),
        "status_counts": statuses,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-fmr", type=float, default=0.001)
    args = parser.parse_args()
    report = run(args.pairs, args.max_fmr)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("model_id", "input_images", "pair_count", "skipped_pairs", "selected")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
