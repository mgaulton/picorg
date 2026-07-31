#!/usr/bin/env python3
"""Calibrate face-distance thresholds from local labelled pairs.

This is an offline evaluation tool. It never searches online, assigns an
identity, or changes the review ledger. Use the resulting operating point for
identity confirmation; keep the broader clustering threshold separate.

Pairs may be JSON or JSONL records with ``path_a``, ``path_b``, and ``label``.
Labels are ``genuine``/``same``/true or ``impostor``/``different``/false.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def vector_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    return sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)) ** 0.5


def _label(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"genuine", "same", "positive", "true", "1"}:
            return True
        if normalized in {"impostor", "different", "negative", "false", "0"}:
            return False
    raise ValueError(f"unsupported pair label: {value!r}")


def load_embeddings(path: Path) -> dict[str, list[float]]:
    payload = json.loads(path.read_text())
    records = payload.get("records", payload) if isinstance(payload, Mapping) else payload
    if not isinstance(records, Mapping):
        raise ValueError("embedding cache must contain a records mapping")
    result = {}
    for image_path, record in records.items():
        embedding = record.get("embedding") if isinstance(record, Mapping) else record
        if isinstance(embedding, list) and embedding:
            result[str(image_path)] = [float(value) for value in embedding]
    return result


def load_pairs(path: Path) -> list[dict[str, Any]]:
    text = path.read_text().strip()
    if not text:
        return []
    payload = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(payload, list):
        raise ValueError("pairs must be a JSON array or JSONL")
    return payload


def evaluate_pairs(pairs: Iterable[Mapping[str, Any]], embeddings: Mapping[str, Sequence[float]], max_fmr: float) -> dict[str, Any]:
    scored = []
    skipped = 0
    for pair in pairs:
        try:
            left = embeddings[str(pair["path_a"])]
            right = embeddings[str(pair["path_b"])]
            scored.append((_label(pair["label"]), vector_distance(left, right)))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    genuine = sum(label for label, _ in scored)
    impostor = len(scored) - genuine
    if not genuine or not impostor:
        raise ValueError("calibration requires at least one genuine and one impostor pair")

    candidates = sorted({distance for _, distance in scored})
    candidates.insert(0, 0.0)
    operating = []
    for threshold in candidates:
        false_matches = sum(not label and distance <= threshold for label, distance in scored)
        false_nonmatches = sum(label and distance > threshold for label, distance in scored)
        operating.append({
            "threshold": threshold,
            "fmr": false_matches / impostor,
            "fnmr": false_nonmatches / genuine,
            "false_matches": false_matches,
            "false_nonmatches": false_nonmatches,
        })
    eligible = [point for point in operating if point["fmr"] <= max_fmr]
    selected = min(eligible, key=lambda point: (point["fnmr"], point["threshold"]))
    return {
        "pair_count": len(scored),
        "skipped_pairs": skipped,
        "genuine_pairs": genuine,
        "impostor_pairs": impostor,
        "max_fmr": max_fmr,
        "selected": selected,
        "operating_points": operating,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--embeddings", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-fmr", type=float, default=0.001)
    args = parser.parse_args()
    if not 0 <= args.max_fmr <= 1:
        parser.error("--max-fmr must be between 0 and 1")
    report = evaluate_pairs(load_pairs(args.pairs), load_embeddings(args.embeddings), args.max_fmr)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
