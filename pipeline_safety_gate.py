#!/usr/bin/env python3
"""Fail closed when an apply audit does not meet accuracy gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate(report: dict, min_precision: float, min_recall: float) -> list[str]:
    errors = []
    precision = report.get("ground_truth_precision")
    recall = report.get("ground_truth_recall")
    if not isinstance(precision, (int, float)):
        errors.append("ground_truth_precision is missing")
    elif precision < min_precision:
        errors.append(f"ground_truth_precision {precision:.4f} < {min_precision:.4f}")
    if not isinstance(recall, (int, float)):
        errors.append("ground_truth_recall is missing")
    elif recall < min_recall:
        errors.append(f"ground_truth_recall {recall:.4f} < {min_recall:.4f}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--min-precision", type=float, default=0.99)
    parser.add_argument("--min-recall", type=float, default=0.99)
    args = parser.parse_args()
    payload = json.loads(args.audit.read_text(encoding="utf-8"))
    errors = validate(payload.get("report", payload), args.min_precision, args.min_recall)
    if errors:
        for error in errors:
            print(f"SAFETY GATE FAILED: {error}")
        return 1
    print("SAFETY GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
