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


def validate_name_moves(report: dict, min_precision: float) -> list[str]:
    """Gate only the precision of explicit high-confidence name moves.

    Recall measures how much of the gallery was labeled, not whether the
    proposed moves are correct. A low-coverage audit can therefore safely
    move its precision-qualified subset while the broader production gate
    continues to require recall as well.
    """
    errors: list[str] = []
    precision = report.get("ground_truth_precision")
    if not isinstance(precision, (int, float)):
        errors.append("ground_truth_precision is missing")
    elif precision < min_precision:
        errors.append(f"ground_truth_precision {precision:.4f} < {min_precision:.4f}")
    high_confidence = report.get("high_confidence")
    if not isinstance(high_confidence, int) or high_confidence < 1:
        errors.append(f"high_confidence {high_confidence!r} is empty")
    return errors


def validate_benchmark(
    report: dict,
    *,
    max_fmr: float,
    max_fmr_upper: float,
    max_fnmr_upper: float,
    min_genuine: int,
    min_impostor: int,
) -> list[str]:
    """Validate a held-out calibration report before permitting an apply run."""
    errors: list[str] = []
    selected = report.get("selected")
    if not isinstance(selected, dict):
        return ["benchmark selected operating point is missing"]
    genuine = report.get("genuine_pairs")
    impostor = report.get("impostor_pairs")
    if not isinstance(genuine, int) or genuine < min_genuine:
        errors.append(f"benchmark genuine_pairs {genuine!r} < {min_genuine}")
    if not isinstance(impostor, int) or impostor < min_impostor:
        errors.append(f"benchmark impostor_pairs {impostor!r} < {min_impostor}")
    fmr = selected.get("fmr")
    if not isinstance(fmr, (int, float)):
        errors.append("benchmark selected fmr is missing")
    elif fmr > max_fmr:
        errors.append(f"benchmark selected fmr {fmr:.6f} > {max_fmr:.6f}")
    fmr_ci = selected.get("fmr_ci95")
    fmr_upper = fmr_ci[1] if isinstance(fmr_ci, list) and len(fmr_ci) >= 2 else None
    if not isinstance(fmr_upper, (int, float)):
        errors.append("benchmark selected fmr_ci95 upper bound is missing")
    elif fmr_upper > max_fmr_upper:
        errors.append(f"benchmark selected fmr_ci95 upper {fmr_upper:.6f} > {max_fmr_upper:.6f}")
    fnmr_ci = selected.get("fnmr_ci95")
    fnmr_upper = fnmr_ci[1] if isinstance(fnmr_ci, list) and len(fnmr_ci) >= 2 else None
    if not isinstance(fnmr_upper, (int, float)):
        errors.append("benchmark selected fnmr_ci95 upper bound is missing")
    elif fnmr_upper > max_fnmr_upper:
        errors.append(
            f"benchmark selected fnmr_ci95 upper {fnmr_upper:.6f} > {max_fnmr_upper:.6f}"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--min-precision", type=float, default=0.99)
    parser.add_argument("--min-recall", type=float, default=0.99)
    parser.add_argument("--name-move-only", action="store_true", help="gate only precision and non-empty >=0.95 name moves")
    parser.add_argument("--benchmark-report", type=Path)
    parser.add_argument("--max-fmr", type=float, default=0.001)
    parser.add_argument("--max-fmr-upper", type=float, default=0.001)
    parser.add_argument("--max-fnmr-upper", type=float, default=0.05)
    parser.add_argument("--min-genuine", type=int, default=100)
    parser.add_argument("--min-impostor", type=int, default=1000)
    args = parser.parse_args()
    payload = json.loads(args.audit.read_text(encoding="utf-8"))
    report = payload.get("report", payload)
    errors = validate_name_moves(report, args.min_precision) if args.name_move_only else validate(report, args.min_precision, args.min_recall)
    if args.benchmark_report:
        benchmark = json.loads(args.benchmark_report.read_text(encoding="utf-8"))
        errors.extend(
            validate_benchmark(
                benchmark,
                max_fmr=args.max_fmr,
                max_fmr_upper=args.max_fmr_upper,
                max_fnmr_upper=args.max_fnmr_upper,
                min_genuine=args.min_genuine,
                min_impostor=args.min_impostor,
            )
        )
    if errors:
        for error in errors:
            print(f"SAFETY GATE FAILED: {error}")
        return 1
    print("SAFETY GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
