#!/usr/bin/env python3
"""Reconcile title clusters and face clusters into one review-only audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List

import picorg_sorter as sorter
from review_ui import build_clusters, load_audit


def _registry_identity_aliases() -> Dict[str, str]:
    """Return normalized curated registry names mapped to one canonical."""
    path = Path(__file__).with_name("project_registry.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    aliases: Dict[str, str] = {}
    for entry in payload.get("entries", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict) or str(entry.get("family") or "") != "manual":
            continue
        canonical = str(entry.get("canonical") or "").strip()
        if not canonical:
            continue
        for name in (canonical, *(entry.get("aliases") or [])):
            key = sorter.normalize_key(str(name))
            if key:
                aliases[key] = canonical
    return aliases


class DisjointSet:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def add(self, value: str) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: str) -> str:
        self.add(value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def reconcile_clusters(name_audit: Dict[str, Any], face_audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    name_clusters = build_clusters(name_audit)
    face_clusters = build_clusters(face_audit)
    registry_aliases = _registry_identity_aliases()
    names_for_path: Dict[str, List[Dict[str, Any]]] = {}
    for cluster in name_clusters:
        for path in cluster["paths"]:
            names_for_path.setdefault(path, []).append(cluster)

    output: List[Dict[str, Any]] = []
    # Emit one review cluster per face cluster. Name evidence is collected as
    # context for those paths and is never allowed to merge face clusters.
    for face_cluster in face_clusters:
        component_paths = sorted({str(path) for path in face_cluster["paths"] if path})
        if not component_paths:
            continue
        face_id = str(face_cluster["cluster_id"])
        # Keep the historical path-derived id stable so existing review
        # decisions continue to apply after the membership split.
        digest = hashlib.sha256("|".join(component_paths).encode("utf-8")).hexdigest()[:16]
        related_names = [name for path in component_paths for name in names_for_path.get(path, [])]
        name_titles = sorted({str(name["title"]) for name in related_names})
        name_identities = sorted({
            registry_aliases[sorter.normalize_key(str(identity))]
            for name in related_names
            for identity in name.get("expected_identities", [])
            if identity and sorter.normalize_key(str(identity)) in registry_aliases
        })
        face_ids = [face_id]
        face_labels = sorted({str(label) for label in face_cluster.get("face_cluster_labels", []) if label})
        method = "name+face" if name_titles else "face-only"
        # Only expose an fbunknown label for a small, single-face group. Large
        # groups remain reviewable but are not identity evidence.
        face_label_eligible = len(component_paths) < 100 and len(face_labels) == 1
        if len(name_identities) == 1:
            title = name_identities[0]
        elif face_label_eligible:
            title = face_labels[0]
            if name_titles:
                title += f" · name: {name_titles[0]}"
        else:
            title = f"facegroup-{digest[:8]}"
        output.append({
            "cluster_id": f"reconciled-{digest}",
            "title": title,
            "paths": component_paths,
            "count": len(component_paths),
            "name_titles": name_titles,
            "name_identities": name_identities,
            "face_clusters": face_ids,
            "face_labels": face_labels,
            "face_label_eligible": face_label_eligible,
            "method": method,
            "sample_paths": component_paths[:12],
        })

    # Do not emit filename/title-only groups. A review cluster is meaningful
    # only when at least one usable face embedding placed its members in the
    # same face cluster. Images with no face result remain in the audit for
    # later processing, but cannot be presented as a purported face group.
    return sorted(output, key=lambda item: (-item["count"], item["paths"][0] if item["paths"] else ""))


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name-audit", type=Path, required=True)
    parser.add_argument("--face-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    clusters = reconcile_clusters(load_audit(args.name_audit), load_audit(args.face_audit))
    results = []
    for cluster in clusters:
        for path in cluster["paths"]:
            results.append({
                "path": path,
                "title": cluster["title"],
                "canonical": None,
                "source_root": str(Path(path).parent),
                # Keep the review method/face labels on each row so the UI can
                # filter face-first groups without reinterpreting titles.
                "review_method": cluster["method"],
                "face_cluster_labels": cluster["face_labels"] if cluster["face_label_eligible"] else [],
                "face_cluster_ids": cluster["face_clusters"],
                "cluster_label": cluster["face_labels"][0] if cluster["face_label_eligible"] else None,
                "face_cluster_id": cluster["face_clusters"][0] if cluster["face_clusters"] else None,
                "expected_identity": cluster["name_identities"][0] if len(cluster["name_identities"]) == 1 else None,
                "reconciliation": {"method": cluster["method"], "name_titles": cluster["name_titles"], "face_clusters": cluster["face_clusters"]},
            })
    _atomic_write(
        args.output,
        {
            "schema_version": 2,
            "source": "name_face_reconciliation",
            "cluster_policy": "face-only",
            "report": {"clusters": len(clusters), "results": len(results)},
            "results": results,
        },
    )
    print(json.dumps({"clusters": len(clusters), "results": len(results), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
