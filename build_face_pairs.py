#!/usr/bin/env python3
"""Build deterministic labeled face pairs from confirmed review decisions."""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any


def build_pairs(
    decisions_path: Path,
    embeddings_path: Path,
    min_per_identity: int = 2,
    image_decisions_path: Path | None = None,
) -> list[dict[str, Any]]:
    decisions_payload = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions = decisions_payload.get("decisions", [])
    if image_decisions_path:
        image_payload = json.loads(image_decisions_path.read_text(encoding="utf-8"))
        # The ledger is append-only. Resolve the newest decision per path
        # before building labels so a later correction/rejection supersedes
        # an older confirmation instead of leaking stale pairs into training.
        latest_by_path: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, decision in enumerate(image_payload.get("decisions", [])):
            path = str(decision.get("path") or "")
            if not path:
                continue
            current = latest_by_path.get(path)
            timestamp = str(decision.get("saved_at") or "")
            if current is None or (timestamp, index) >= (str(current[1].get("saved_at") or ""), current[0]):
                latest_by_path[path] = (index, decision)
        decisions = [row[1] for row in latest_by_path.values()]
    records = json.loads(embeddings_path.read_text(encoding="utf-8")).get("records", {})
    grouped: dict[str, set[str]] = {}
    display_names: dict[str, str] = {}
    for decision in decisions:
        if decision.get("status") != "confirmed":
            continue
        identity = str(decision.get("identity", "")).strip()
        if not identity:
            continue
        # Image-level ledgers use `path`; cluster ledgers use `sample_paths`.
        candidate_paths = [decision.get("path")] if image_decisions_path else decision.get("sample_paths", [])
        key = identity.casefold()
        paths = grouped.setdefault(key, set())
        display_names.setdefault(key, identity)
        paths.update(
            str(path)
            for path in candidate_paths
            if path and isinstance(records.get(str(path)), dict) and isinstance(records[str(path)].get("embedding"), list)
        )
    groups = [
        (display_names[key], sorted(paths))
        for key, paths in grouped.items()
        if len(paths) >= min_per_identity
    ]
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
    parser.add_argument("--image-decisions", type=Path, help="prefer individually confirmed image decisions over mixed cluster samples")
    args = parser.parse_args()
    if args.min_per_identity < 2:
        parser.error("--min-per-identity must be at least 2")
    pairs = build_pairs(args.decisions, args.embeddings, args.min_per_identity, args.image_decisions)
    args.output.write_text(json.dumps(pairs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"pairs": len(pairs), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
