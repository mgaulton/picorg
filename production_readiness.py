#!/usr/bin/env python3
"""Evaluate whether a PicOrg run is safe for automatic moves."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def check(
    audit: dict,
    preflight: dict | None,
    *,
    min_precision: float,
    min_recall: float,
    ui_token: str,
    backend: str = "dlib",
    model_license_confirmed: bool = False,
    benchmark: dict | None = None,
    min_genuine_pairs: int = 100,
    min_impostor_pairs: int = 100,
    max_fmr_ci95: float = 0.01,
    max_fnmr_ci95: float = 0.01,
) -> list[str]:
    errors: list[str] = []
    for field, minimum in (("ground_truth_precision", min_precision), ("ground_truth_recall", min_recall)):
        value = audit.get(field)
        if not isinstance(value, (int, float)):
            errors.append(f"{field} is missing")
        elif value < minimum:
            errors.append(f"{field} {value:.4f} < {minimum:.4f}")
    if preflight:
        counts = preflight.get("counts", {})
        for field in ("missing", "unsupported", "empty", "corrupt", "oversized"):
            if counts.get(field, 0):
                errors.append(f"preflight {field}={counts[field]}")
    if not ui_token:
        errors.append("PICORG_UI_TOKEN is not configured for LAN exposure")
    if backend == "insightface" and not model_license_confirmed:
        errors.append("InsightFace model license has not been confirmed for this deployment")
    if benchmark is None:
        errors.append("held-out benchmark report is required")
    else:
        genuine = int(benchmark.get("genuine_pairs", 0) or 0)
        impostor = int(benchmark.get("impostor_pairs", 0) or 0)
        if genuine < min_genuine_pairs:
            errors.append(f"genuine_pairs {genuine} < {min_genuine_pairs}")
        if impostor < min_impostor_pairs:
            errors.append(f"impostor_pairs {impostor} < {min_impostor_pairs}")
        selected = benchmark.get("selected", {})
        fmr_ci = selected.get("fmr_ci95", [0.0, 1.0])
        fnmr_ci = selected.get("fnmr_ci95", [0.0, 1.0])
        if not isinstance(fmr_ci, list) or len(fmr_ci) != 2 or fmr_ci[1] > max_fmr_ci95:
            errors.append(f"fmr_ci95 upper bound exceeds {max_fmr_ci95:.4f}")
        if not isinstance(fnmr_ci, list) or len(fnmr_ci) != 2 or fnmr_ci[1] > max_fnmr_ci95:
            errors.append(f"fnmr_ci95 upper bound exceeds {max_fnmr_ci95:.4f}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--min-precision", type=float, default=0.99)
    parser.add_argument("--min-recall", type=float, default=0.99)
    parser.add_argument("--ui-token", default="")
    parser.add_argument("--backend", default="dlib", choices=("dlib", "insightface"))
    parser.add_argument("--model-license-confirmed", action="store_true")
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--min-genuine-pairs", type=int, default=100)
    parser.add_argument("--min-impostor-pairs", type=int, default=100)
    parser.add_argument("--max-fmr-ci95", type=float, default=0.01)
    parser.add_argument("--max-fnmr-ci95", type=float, default=0.01)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    preflight = json.loads(args.preflight.read_text(encoding="utf-8")) if args.preflight else None
    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    errors = check(
        audit.get("report", audit),
        preflight,
        min_precision=args.min_precision,
        min_recall=args.min_recall,
        ui_token=args.ui_token,
        backend=args.backend,
        model_license_confirmed=args.model_license_confirmed,
        benchmark=benchmark,
        min_genuine_pairs=args.min_genuine_pairs,
        min_impostor_pairs=args.min_impostor_pairs,
        max_fmr_ci95=args.max_fmr_ci95,
        max_fnmr_ci95=args.max_fnmr_ci95,
    )
    result = {"production_ready": not errors, "errors": errors}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
