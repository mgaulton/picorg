#!/usr/bin/env python3
"""Reconcile report-only face-grouping batches without making assignments."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--audit", type=Path, default=Path("/tmp/picorg_periodic_apply.json"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/picorg_face_grouping_reconciled.json"))
    args = parser.parse_args()

    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    titles = {item["path"]: item.get("title") or Path(item["path"]).stem for item in audit.get("results", [])}
    batch_files = sorted(args.batch_dir.glob("picorg_face_grouping_batch-*.json"))
    by_path = {}
    batches = []
    for batch_file in batch_files:
        try:
            payload = json.loads(batch_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        batches.append({"file": str(batch_file), "offset": payload.get("offset"), "processed": payload.get("processed", 0)})
        for result in payload.get("results", []):
            by_path[result["path"]] = result

    results = list(by_path.values())
    groups = defaultdict(list)
    clusters = defaultdict(list)
    for result in results:
        if result.get("status") != "matched":
            continue
        candidates = result.get("candidates") or []
        if not candidates:
            continue
        person = candidates[0]["person"]
        groups[person].append(result["path"])
        clusters[titles.get(result["path"], Path(result["path"]).stem)].append(person)

    consensus = {}
    for cluster, people in clusters.items():
        counts = Counter(people)
        leader, count = counts.most_common(1)[0]
        consensus[cluster] = {
            "images": len(people),
            "identities": dict(counts),
            "consensus": count >= 3 and count / len(people) >= 0.75 and len(counts) == 1,
        }

    payload = {
        "audit": str(args.audit),
        "batches": batches,
        "batch_count": len(batches),
        "unique_paths": len(results),
        "statuses": dict(Counter(result.get("status") for result in results)),
        "groups": {person: sorted(paths) for person, paths in sorted(groups.items())},
        "cluster_consensus": consensus,
        "consensus_clusters": sum(1 for item in consensus.values() if item["consensus"]),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("batch_count", "unique_paths", "statuses", "consensus_clusters")}, sort_keys=True))
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
