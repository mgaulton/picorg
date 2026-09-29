#!/usr/bin/env python3
"""Create deterministic image-disjoint train and held-out face-pair sets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def split_pairs(pairs: list[dict], fraction: float = 0.2, seed: str = "picorg") -> tuple[list[dict], list[dict]]:
    identities: dict[str, set[str]] = {}
    for pair in pairs:
        identity = pair.get("identity")
        if identity:
            identities.setdefault(str(identity), set()).update((str(pair.get("path_a", "")), str(pair.get("path_b", ""))))
        for key in ("identity_a", "identity_b"):
            if pair.get(key):
                path_key = "path_a" if key == "identity_a" else "path_b"
                identities.setdefault(str(pair[key]), set()).add(str(pair.get(path_key, "")))
    heldout: set[str] = set()
    for identity, paths in identities.items():
        ranked = sorted(paths, key=lambda path: hashlib.sha256(f"{seed}:{identity}:{path}".encode()).hexdigest())
        count = max(1, round(len(ranked) * fraction)) if len(ranked) >= 2 else 0
        heldout.update(ranked[:count])
    # Partition in one pass. Comparing each dict against the evaluation list
    # makes this O(number_of_pairs * number_of_heldout_pairs), which becomes
    # prohibitively slow for the full labelled cache.
    training = []
    evaluation = []
    for pair in pairs:
        is_evaluation = str(pair.get("path_a")) in heldout and str(pair.get("path_b")) in heldout
        (evaluation if is_evaluation else training).append(pair)
    return training, evaluation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--train-output", type=Path, required=True)
    parser.add_argument("--heldout-output", type=Path, required=True)
    parser.add_argument("--fraction", type=float, default=0.2)
    parser.add_argument("--seed", default="picorg")
    args = parser.parse_args()
    if not 0 < args.fraction < 1:
        parser.error("--fraction must be between 0 and 1")
    pairs = json.loads(args.pairs.read_text(encoding="utf-8"))
    train, heldout = split_pairs(pairs, args.fraction, args.seed)
    for path, data in ((args.train_output, train), (args.heldout_output, heldout)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"train_pairs": len(train), "heldout_pairs": len(heldout)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
