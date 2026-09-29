#!/usr/bin/env python3
"""Measure face-cluster purity against confirmed review labels.

This is deliberately report-only.  It never assigns, moves, or rewrites media
or review decisions.  Paths in older audits often point at the pre-sort tree;
``--search-root`` permits a conservative unique-basename relink for metrics.
Ambiguous and unreadable paths are counted and excluded instead of being
treated as correct.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


CONFIRMED = {"confirmed", "approved"}


def _identity_key(value: str) -> str:
    """Compare labels case-insensitively without inventing alias matches."""
    return " ".join(value.casefold().split())


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _decision_items(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("decisions", [])
    if not isinstance(payload, list):
        return []
    return (item for item in payload if isinstance(item, Mapping))


def _confirmed_labels(paths: Sequence[Path]) -> Tuple[Dict[str, str], Dict[str, List[str]], Counter]:
    labels: Dict[str, str] = {}
    conflicts: Dict[str, List[str]] = defaultdict(list)
    sources: Counter = Counter()
    for path in paths:
        try:
            payload = _load_json(path)
        except (OSError, json.JSONDecodeError):
            sources[f"unreadable:{path.name}"] += 1
            continue
        for item in _decision_items(payload):
            if str(item.get("status") or "").lower() not in CONFIRMED:
                continue
            identity = _identity_key(str(item.get("identity") or "").strip())
            if not identity:
                continue
            raw_paths: List[Any] = []
            if item.get("path"):
                raw_paths.append(item.get("path"))
            raw_paths.extend(item.get("sample_paths") or [])
            source = "image" if item.get("scope") == "image" or item.get("path") else "cluster_sample"
            for raw in raw_paths:
                key = os.path.normpath(str(raw))
                if not key or key == ".":
                    continue
                previous = labels.get(key)
                if previous and previous != identity:
                    values = conflicts[key]
                    if previous not in values:
                        values.append(previous)
                    if identity not in values:
                        values.append(identity)
                    labels.pop(key, None)
                    sources["conflicting"] += 1
                elif key not in conflicts:
                    labels[key] = identity
                    sources[source] += 1
    return labels, dict(conflicts), sources


def _safe_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _build_basename_index(roots: Sequence[Path]) -> Tuple[Dict[str, Path], set[str], int]:
    candidates: Dict[str, List[Path]] = defaultdict(list)
    read_errors = 0
    for root in roots:
        try:
            if not root.is_dir():
                continue
            iterator = root.rglob("*")
            for path in iterator:
                if _safe_file(path):
                    candidates[path.name].append(path)
        except OSError:
            read_errors += 1
    unique = {name: paths[0] for name, paths in candidates.items() if len(paths) == 1}
    ambiguous = {name for name, paths in candidates.items() if len(paths) > 1}
    return unique, ambiguous, read_errors


def _resolver(audit_paths: Sequence[str], search_roots: Sequence[Path]) -> Tuple[Callable[[str], Optional[str]], Dict[str, int]]:
    audit_exact = {os.path.normpath(path): path for path in audit_paths}
    unique, ambiguous, read_errors = _build_basename_index(search_roots) if search_roots else ({}, set(), 0)
    stats = {"direct": 0, "relinked": 0, "missing": 0, "ambiguous": 0, "read_errors": read_errors}

    def resolve(raw: str) -> Optional[str]:
        key = os.path.normpath(raw)
        # When no relink roots are supplied, retain the audit namespace so
        # archived audits can still be compared.  With roots, prefer an
        # existing physical path and otherwise relink stale pre-sort paths.
        if key in audit_exact and (not search_roots or _safe_file(Path(key))):
            stats["direct"] += 1
            return audit_exact[key]
        basename = Path(key).name
        if basename in ambiguous:
            stats["ambiguous"] += 1
            return None
        target = unique.get(basename)
        if target is None:
            stats["missing"] += 1
            return None
        stats["relinked"] += 1
        return str(target)

    return resolve, stats


def compute_metrics(audit: Mapping[str, Any], decision_paths: Sequence[Path], search_roots: Sequence[Path] = ()) -> Dict[str, Any]:
    results = [item for item in (audit.get("results") or []) if isinstance(item, Mapping)]
    face_results = [item for item in results if str(item.get("face_cluster_id") or "").strip()]
    audit_paths = [str(item.get("path") or "") for item in face_results if item.get("path")]
    resolve, resolve_stats = _resolver(audit_paths, search_roots)
    labels, conflicts, label_sources = _confirmed_labels(decision_paths)

    # Resolve both sides into the same physical path namespace.  A label that
    # cannot be joined is evidence coverage loss, not a negative match.
    resolved_labels: Dict[str, str] = {}
    for raw, identity in labels.items():
        resolved = resolve(raw)
        if resolved:
            resolved_labels[resolved] = identity
    groups: Dict[str, List[str]] = defaultdict(list)
    for item in face_results:
        resolved = resolve(str(item["path"]))
        if resolved:
            groups[str(item["face_cluster_id"])].append(resolved)

    cluster_rows: List[Dict[str, Any]] = []
    labeled_paths = 0
    weighted_correct = 0
    for cluster_id, paths in groups.items():
        counts = Counter(resolved_labels[path] for path in paths if path in resolved_labels)
        if not counts:
            continue
        total = sum(counts.values())
        best = counts.most_common(1)[0]
        purity = best[1] / total
        labeled_paths += total
        weighted_correct += best[1]
        cluster_rows.append({
            "face_cluster_id": cluster_id,
            "labeled_count": total,
            "identity_count": len(counts),
            "purity": round(purity, 6),
            "contamination": round(1.0 - purity, 6),
            "identity_counts": dict(sorted(counts.items())),
            "sample_paths": paths[:12],
        })
    cluster_rows.sort(key=lambda row: (row["purity"], -row["labeled_count"], row["face_cluster_id"]))
    weighted_purity = weighted_correct / labeled_paths if labeled_paths else None
    macro_purity = (sum(row["purity"] for row in cluster_rows) / len(cluster_rows)) if cluster_rows else None
    strict = audit.get("strict_all_members")
    min_similarity = audit.get("min_similarity")
    return {
        "schema_version": 1,
        "source": "face_cluster_purity_report",
        "audit": {"model_id": audit.get("model_id"), "threshold": audit.get("threshold"), "min_similarity": min_similarity, "strict_all_members": strict},
        "comparable_to_default_strict_mode": strict is True,
        "clusters": len({str(item["face_cluster_id"]) for item in face_results}),
        "clusters_with_labels": len(cluster_rows),
        "clusters_mixed": sum(1 for row in cluster_rows if row["identity_count"] > 1),
        "audit_face_paths": len(face_results),
        "labeled_paths": labeled_paths,
        "weighted_purity": round(weighted_purity, 6) if weighted_purity is not None else None,
        "weighted_contamination": round(1.0 - weighted_purity, 6) if weighted_purity is not None else None,
        "macro_purity": round(macro_purity, 6) if macro_purity is not None else None,
        "resolver": resolve_stats,
        "label_sources": dict(label_sources),
        "conflicting_labels": len(conflicts),
        "worst_clusters": cluster_rows[:20],
        "valid": bool(cluster_rows),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, action="append", default=[])
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    decisions = args.decisions or [Path("review_decisions.json"), Path("review_image_decisions.json")]
    try:
        audit = _load_json(args.audit)
        if not isinstance(audit, Mapping) or not isinstance(audit.get("results"), list):
            raise ValueError("audit must contain a results list")
        report = compute_metrics(audit, decisions, args.search_root)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
