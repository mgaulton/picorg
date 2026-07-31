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

from review_ui import build_clusters, load_audit


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
    paths: Dict[str, Dict[str, str]] = {}
    disjoint = DisjointSet()
    for prefix, clusters in (("name", name_clusters), ("face", face_clusters)):
        for cluster in clusters:
            node = f"{prefix}:{cluster['cluster_id']}"
            disjoint.add(node)
            for path in cluster["paths"]:
                paths.setdefault(path, {})[prefix] = node
                disjoint.union(node, f"path:{path}")

    components: Dict[str, Dict[str, Any]] = {}
    for path, links in paths.items():
        root = disjoint.find(next(iter(links.values())))
        component = components.setdefault(root, {"paths": [], "name_nodes": set(), "face_nodes": set()})
        component["paths"].append(path)
        component["name_nodes"].update(node for node in links.values() if node.startswith("name:"))
        component["face_nodes"].update(node for node in links.values() if node.startswith("face:"))

    output: List[Dict[str, Any]] = []
    names_by_node = {f"name:{cluster['cluster_id']}": cluster for cluster in name_clusters}
    faces_by_node = {f"face:{cluster['cluster_id']}": cluster for cluster in face_clusters}
    for index, component in enumerate(sorted(components.values(), key=lambda item: (-len(item["paths"]), sorted(item["paths"])[0]))):
        component_paths = sorted(component["paths"])
        digest = hashlib.sha256("|".join(component_paths).encode("utf-8")).hexdigest()[:16]
        name_titles = sorted(names_by_node[node]["title"] for node in component["name_nodes"])
        face_ids = sorted(faces_by_node[node]["cluster_id"] for node in component["face_nodes"])
        method = "name+face" if name_titles and face_ids else "name-only" if name_titles else "face-only"
        title = f"reconciled-{digest} ({method})"
        output.append({
            "cluster_id": f"reconciled-{digest}",
            "title": title,
            "paths": component_paths,
            "count": len(component_paths),
            "name_titles": name_titles,
            "face_clusters": face_ids,
            "method": method,
            "sample_paths": component_paths[:12],
        })
    return output


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
            results.append({"path": path, "title": cluster["title"], "canonical": None, "source_root": str(Path(path).parent), "reconciliation": {"method": cluster["method"], "name_titles": cluster["name_titles"], "face_clusters": cluster["face_clusters"]}})
    _atomic_write(args.output, {"schema_version": 1, "source": "name_face_reconciliation", "report": {"clusters": len(clusters), "results": len(results)}, "results": results})
    print(json.dumps({"clusters": len(clusters), "results": len(results), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
