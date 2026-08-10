#!/usr/bin/env python3
"""Build deterministic labeled face pairs from confirmed review decisions."""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any


def build_pairs(decisions_path: Path, embeddings_path: Path, min_per_identity: int = 2) -> list[dict[str, Any]]:
    decisions = json.loads(decisions_path.read_text(encoding="utf-8")).get("decisions", [])
    records = json.loads(embeddings_path.read_text(encoding="utf-8")).get("records", {})
    groups: list[tuple[str, list[str]]] = []
    for decision in decisions:
        if decision.get("status") != "confirmed":
            continue
        paths = [
            str(path)
            for path in decision.get("sample_paths", [])
            if isinstance(records.get(path), dict) and isinstance(records[path].get("embedding"), list)
        ]
        if len(paths) >= min_per_identity:
            groups.append((str(decision.get("identity", "")), sorted(set(paths))))
    groups.sort(key=lambda item: item[0])
    pairs: list[dict[str, Any]] = []
    for identity, paths in groups:
        pairs.extend({"path_a": a, "path_b": b, "label": "genuine", "identity": identity} for a, b in itertools.combinations(paths, 2))
    for (left_identity, left_paths), (right_identity, right_paths) in itertools.combinations(groups, 2):
        pairs.extend(
            {"path_a": left, "path_b": right, "label": "impostor", "identity_a": left_identity, "identity_b": right_identity}
            for left in left_paths
            for right in right_paths
        )
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--embeddings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-per-identity", type=int, default=2)
    args = parser.parse_args()
    if args.min_per_identity < 2:
        parser.error("--min-per-identity must be at least 2")
    pairs = build_pairs(args.decisions, args.embeddings, args.min_per_identity)
    args.output.write_text(json.dumps(pairs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"pairs": len(pairs), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
