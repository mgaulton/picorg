#!/usr/bin/env python3
"""Plan/apply benchmark-gated face identity moves.

The matcher remains report-only by default.  ``--apply`` is deliberately
separate and fails closed unless the held-out benchmark, canonical identity
lookup, source hash, and single-confident-face gates all pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import picorg_sorter as sorter


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"error: invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"error: expected JSON object: {path}")
    return value


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def benchmark_errors(
    benchmark: dict[str, Any],
    *,
    max_fmr_upper: float = 0.001,
    max_fnmr_upper: float = 0.05,
    min_genuine: int = 100,
    min_impostor: int = 1000,
) -> list[str]:
    errors: list[str] = []
    genuine = int(benchmark.get("genuine_pairs", 0) or 0)
    impostor = int(benchmark.get("impostor_pairs", 0) or 0)
    if genuine < min_genuine:
        errors.append(f"genuine_pairs {genuine} < {min_genuine}")
    if impostor < min_impostor:
        errors.append(f"impostor_pairs {impostor} < {min_impostor}")
    selected = benchmark.get("selected")
    if not isinstance(selected, dict):
        return errors + ["benchmark selected operating point is missing"]
    for name, limit in (("fmr_ci95", max_fmr_upper), ("fnmr_ci95", max_fnmr_upper)):
        interval = selected.get(name)
        upper = interval[1] if isinstance(interval, list) and len(interval) >= 2 else None
        if not isinstance(upper, (int, float)):
            errors.append(f"benchmark {name} upper bound is missing")
        elif float(upper) > limit:
            errors.append(f"benchmark {name} upper {float(upper):.6f} > {limit:.6f}")
    return errors


def identity_index() -> dict[str, sorter.Identity]:
    catalog, _aliases, _canonical, _tokens, _canonical_by_key = sorter.load_identity_catalog()
    index: dict[str, sorter.Identity] = {}
    for identity in catalog:
        for value in (identity.canonical, *identity.aliases):
            key = sorter.normalize_key(value)
            if key and not sorter.is_generic_identity_token(identity.canonical):
                index.setdefault(key, identity)
    return index


def _under(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return True
    return False


def _candidate_gate(
    row: dict[str, Any],
    *,
    identity: sorter.Identity | None,
    source_roots: list[Path],
    protected_roots: list[Path],
    source_fingerprints: dict[str, Any],
    threshold: float,
    margin: float,
) -> tuple[bool, str, dict[str, Any]]:
    path_text = str(row.get("path") or "")
    path = Path(path_text)
    if row.get("status") != "matched" or not row.get("matched_identity"):
        return False, "status_not_matched", {"path": path_text}
    if int(row.get("confident_face_count", 0) or 0) != 1:
        return False, "not_exactly_one_confident_face", {"path": path_text}
    if identity is None:
        return False, "identity_not_in_canonical_registry", {"path": path_text}
    if not _under(path, source_roots):
        return False, "outside_movable_source_roots", {"path": path_text}
    if _under(path, protected_roots):
        return False, "protected_source_root", {"path": path_text}
    try:
        if not path.is_file():
            return False, "source_missing", {"path": path_text}
        expected = str(source_fingerprints.get(path_text) or "")
        if not expected:
            return False, "source_fingerprint_missing", {"path": path_text}
        actual = sha256_file(path)
    except OSError:
        return False, "source_unreadable", {"path": path_text}
    if actual != expected:
        return False, "source_fingerprint_changed", {"path": path_text}
    candidates = row.get("candidates") if isinstance(row.get("candidates"), list) else []
    if not candidates:
        return False, "candidates_missing", {"path": path_text}
    best = candidates[0] if isinstance(candidates[0], dict) else {}
    distance = float(best.get("distance", threshold + 1.0))
    if distance > threshold:
        return False, "distance_gate", {"path": path_text, "distance": distance}
    if len(candidates) > 1 and isinstance(candidates[1], dict):
        second = float(candidates[1].get("distance", distance))
        if second - distance < margin:
            return False, "margin_gate", {"path": path_text, "distance": distance, "margin": second - distance}
    return True, "eligible", {
        "path": path_text,
        "identity": identity.canonical,
        "family": identity.family,
        "distance": distance,
        "source_sha256": actual,
        "multi_face_policy": row.get("multi_face_policy"),
    }


def build_plan(
    matches: dict[str, Any],
    *,
    source_roots: list[Path],
    protected_roots: list[Path],
    threshold: float | None = None,
    margin: float | None = None,
) -> dict[str, Any]:
    index = identity_index()
    source_fingerprints = matches.get("source_fingerprints")
    if not isinstance(source_fingerprints, dict):
        source_fingerprints = {}
    effective_threshold = float(matches.get("threshold", 0.45) if threshold is None else threshold)
    effective_margin = float(matches.get("margin", 0.08) if margin is None else margin)
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for raw in matches.get("results", []):
        if not isinstance(raw, dict):
            continue
        identity = index.get(sorter.normalize_key(str(raw.get("matched_identity") or "")))
        ok, reason, detail = _candidate_gate(
            raw,
            identity=identity,
            source_roots=source_roots,
            protected_roots=protected_roots,
            source_fingerprints=source_fingerprints,
            threshold=effective_threshold,
            margin=effective_margin,
        )
        (eligible if ok else blocked).append({**detail, "reason": reason})
    return {
        "schema_version": 1,
        "source_matches": matches.get("audit"),
        "threshold": effective_threshold,
        "margin": effective_margin,
        "eligible": eligible,
        "blocked": blocked,
        "eligible_count": len(eligible),
        "blocked_count": len(blocked),
    }


def apply_plan(plan: dict[str, Any], *, destination_root: Path, ledger: Path) -> dict[str, Any]:
    moved: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in plan.get("eligible", []):
        source = Path(str(item["path"]))
        identity = sorter.Identity(str(item["identity"]), str(item["family"]), ())
        try:
            if not source.is_file():
                raise FileNotFoundError("source missing before apply")
            if sha256_file(source) != str(item.get("source_sha256") or ""):
                raise RuntimeError("source fingerprint changed after planning")
            target_dir = sorter.destination_for(identity, destination_root)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = sorter.resolve_target_path(target_dir, source)
            shutil.move(str(source), str(target))
            moved.append({"source": str(source), "destination": str(target), "identity": identity.canonical})
        except (OSError, ValueError) as exc:
            errors.append({"source": str(source), "error": str(exc)})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        for item in moved:
            handle.write(json.dumps({"event": "face_auto_move", **item}, sort_keys=True) + "\n")
    return {"moved": moved, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".cache/picorg/face-auto-move-plan.json"))
    parser.add_argument("--source-root", action="append", type=Path, required=True)
    parser.add_argument("--protected-root", action="append", type=Path, default=[])
    parser.add_argument("--destination-root", type=Path, default=sorter.DEST_ROOT)
    parser.add_argument("--ledger", type=Path, default=Path("review_decision_ledger.jsonl"))
    parser.add_argument("--max-fmr-upper", type=float, default=0.001)
    parser.add_argument("--max-fnmr-upper", type=float, default=0.05)
    parser.add_argument("--min-genuine", type=int, default=100)
    parser.add_argument("--min-impostor", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    matches = _read(args.matches)
    benchmark = _read(args.benchmark)
    errors = benchmark_errors(
        benchmark,
        max_fmr_upper=args.max_fmr_upper,
        max_fnmr_upper=args.max_fnmr_upper,
        min_genuine=args.min_genuine,
        min_impostor=args.min_impostor,
    )
    plan = build_plan(
        matches,
        source_roots=args.source_root,
        protected_roots=[*args.protected_root, *sorter.PROTECTED_SOURCE_ROOTS],
    )
    plan["benchmark_errors"] = errors
    plan["apply_allowed"] = not errors
    if args.apply:
        if errors:
            for error in errors:
                print(f"SAFETY GATE FAILED: {error}")
            return 1
        plan["apply"] = apply_plan(plan, destination_root=args.destination_root, ledger=args.ledger)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{args.output.name}.", dir=args.output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"eligible": plan["eligible_count"], "blocked": plan["blocked_count"], "apply_allowed": not errors}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
