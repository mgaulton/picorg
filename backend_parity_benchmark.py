#!/usr/bin/env python3
"""Compare two face-embedding backends on the same held-out labelled pairs.

The inputs are privacy-preserving local embedding caches. No image bytes leave
the machine and no database or review state is changed. Use an image-disjoint
held-out pair file produced by ``split_face_pairs.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from face_match_benchmark import evaluate_pairs, load_embeddings, load_pairs


def _common_pairs(pairs: list[dict[str, Any]], left: dict[str, list[float]], right: dict[str, list[float]]) -> list[dict[str, Any]]:
    return [
        pair for pair in pairs
        if str(pair.get("path_a")) in left
        and str(pair.get("path_b")) in left
        and str(pair.get("path_a")) in right
        and str(pair.get("path_b")) in right
    ]


def build_report(pairs_path: Path, legacy_path: Path, picorg_path: Path, max_fmr: float) -> dict[str, Any]:
    pairs = load_pairs(pairs_path)
    legacy = load_embeddings(legacy_path)
    picorg = load_embeddings(picorg_path)
    common = _common_pairs(pairs, legacy, picorg)
    if not common:
        raise ValueError("no labelled pairs have embeddings in both backends")
    return {
        "pairs": str(pairs_path),
        "legacy_embeddings": str(legacy_path),
        "picorg_embeddings": str(picorg_path),
        "pair_count_common": len(common),
        "legacy_embeddings_count": len(legacy),
        "picorg_embeddings_count": len(picorg),
        "legacy": evaluate_pairs(common, legacy, max_fmr),
        "picorg": evaluate_pairs(common, picorg, max_fmr),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--legacy-embeddings", type=Path, required=True)
    parser.add_argument("--picorg-embeddings", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-fmr", type=float, default=0.001)
    args = parser.parse_args()
    if not 0 <= args.max_fmr <= 1:
        parser.error("--max-fmr must be between 0 and 1")
    report = build_report(args.pairs, args.legacy_embeddings, args.picorg_embeddings, args.max_fmr)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
