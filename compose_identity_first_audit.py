#!/usr/bin/env python3
"""Compose identity-first face review data without moving files.

Known face-identity candidates are promoted into review groups before generic
face clusters.  Paths with a confident identity match are removed from the
generic portion, preventing a known person from being hidden in an unknown
collection.  All source audits remain intact for reproducibility.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def _read(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError(f"invalid audit payload: {path}")
    return payload


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(handle.name).replace(path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _identity_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return key or "unknown"


def compose(
    primary: Dict[str, Any],
    face: Dict[str, Any],
    identity_matches: Dict[str, Any],
) -> Dict[str, Any]:
    primary_by_path = {
        str(row.get("path")): row
        for row in primary.get("results", [])
        if isinstance(row, dict) and row.get("path")
    }
    matched_rows = [
        row
        for row in identity_matches.get("results", [])
        if isinstance(row, dict)
        and row.get("status") == "matched"
        and row.get("path")
        and row.get("matched_identity")
    ]
    matched_paths = {str(row["path"]) for row in matched_rows}
    generic_rows = [
        row
        for row in face.get("results", [])
        if isinstance(row, dict) and str(row.get("path") or "") not in matched_paths
    ]

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in matched_rows:
        grouped[str(row["matched_identity"])].append(row)
    promoted: List[Dict[str, Any]] = []
    for identity, rows in sorted(grouped.items(), key=lambda item: item[0].casefold()):
        cluster_id = f"identity-match:{_identity_key(identity)}"
        for row in sorted(rows, key=lambda item: str(item["path"])):
            path = str(row["path"])
            source = primary_by_path.get(path, {})
            promoted.append(
                {
                    "path": path,
                    "title": str(source.get("title") or Path(path).stem),
                    "face_cluster_id": cluster_id,
                    "cluster_label": identity,
                    "expected_identity": identity,
                    "review_method": "face-identity",
                    "source_family": "face_match",
                    "identity_match": True,
                    "matched_identity": identity,
                    "candidates": row.get("candidates", []),
                    "face_candidates": row.get("face_candidates", []),
                    "face_count": row.get("face_count"),
                    "confident_face_count": row.get("confident_face_count"),
                    "multi_face_policy": row.get("multi_face_policy"),
                    "quality": row.get("quality"),
                }
            )

    payload = dict(face)
    payload["schema_version"] = 2
    payload["source"] = "identity_first_face_review"
    payload["cluster_policy"] = "identity-first-face-then-generic"
    payload["primary_audit"] = primary.get("audit")
    payload["generic_face_audit"] = face.get("audit")
    payload["identity_match_audit"] = identity_matches.get("audit")
    payload["report"] = {
        **(face.get("report") or {}),
        "identity_first_matches": len(promoted),
        "generic_face_results": len(generic_rows),
        "identity_first_identities": len(grouped),
    }
    payload["results"] = promoted + generic_rows
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--face", type=Path, required=True)
    parser.add_argument("--identity-matches", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = compose(_read(args.primary), _read(args.face), _read(args.identity_matches))
    _atomic_write(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "identity_first_matches": payload["report"]["identity_first_matches"],
                "generic_face_results": payload["report"]["generic_face_results"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
