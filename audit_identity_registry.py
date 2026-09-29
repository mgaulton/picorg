#!/usr/bin/env python3
"""Audit PicOrg markers against the shared Metadaily identity registry."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import picorg_sorter as sorter

DEFAULT_MARKERS = Path("/opt/picorg/identity_face_markers.json")
DEFAULT_MD = Path(os.environ.get("PICORG_IDENTITY_REGISTRY", "/opt/shared/identity_aliases.json"))
DEFAULT_OUT = Path("/opt/picorg/.cache/picorg/audits") / "identity-registry-audit.json"


def _marker_identities(markers: List[dict]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for row in markers:
        identity = str(row.get("identity") or "").strip()
        if identity and str(row.get("status") or "") == "confirmed":
            counts[identity] += 1
    return dict(counts)


def build_audit(
    *,
    markers_path: Path,
    md_path: Path,
) -> Dict[str, Any]:
    markers_payload = json.loads(markers_path.read_text(encoding="utf-8"))
    markers = list(markers_payload.get("markers") or [])
    md_payload = json.loads(md_path.read_text(encoding="utf-8")) if md_path.exists() else {}
    md_identities = list(md_payload.get("identities") or []) if isinstance(md_payload, dict) else []

    catalog, alias_index, canonical_index, _, preferred = sorter.load_identity_catalog()
    confirmed_markers = _marker_identities(markers)

    linked: List[dict] = []
    ambiguous: List[dict] = []
    unlinked: List[dict] = []
    for identity, count in sorted(confirmed_markers.items()):
        key = sorter.normalize_key(identity)
        hits = alias_index.get(key) or set()
        cans = sorted({item.canonical for item in hits})
        families = sorted({item.family for item in hits})
        row = {
            "identity": identity,
            "marker_count": count,
            "catalog_canonicals": cans,
            "catalog_families": families,
            "preferred_target": preferred.get(key),
        }
        if len(cans) == 1:
            linked.append(row)
        elif len(cans) > 1:
            ambiguous.append(row)
        else:
            unlinked.append(row)

    md_confirmed = [item for item in md_identities if isinstance(item, dict) and item.get("status") == "confirmed"]
    marker_keys = {sorter.normalize_key(name) for name in confirmed_markers}
    md_without_markers = []
    for item in md_confirmed:
        keys = {
            sorter.normalize_key(str(item.get("primary_folder") or "")),
            sorter.normalize_key(str(item.get("id") or "")),
        }
        keys |= {sorter.normalize_key(str(value)) for value in (item.get("display_names") or []) if value}
        if not (keys & marker_keys):
            md_without_markers.append(
                {
                    "id": item.get("id"),
                    "primary_folder": item.get("primary_folder"),
                }
            )

    # Alias keys that still map to multiple distinct normalized canonicals.
    collisions = []
    for alias_key, idents in alias_index.items():
        canon_keys = {sorter.normalize_key(item.canonical) for item in idents}
        if len(canon_keys) <= 1:
            continue
        collisions.append(
            {
                "alias_key": alias_key,
                "canonicals": sorted({item.canonical for item in idents}),
                "families": sorted({item.family for item in idents}),
                "preferred_target": preferred.get(alias_key),
            }
        )
    collisions.sort(key=lambda row: (-len(row["canonicals"]), row["alias_key"]))

    family_counts = Counter(item.family for item in catalog)
    redditdaily = sorted(
        ({"canonical": item.canonical, "aliases": list(item.aliases)} for item in catalog if item.family == "redditdaily"),
        key=lambda row: str(row["canonical"]).casefold(),
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "markers": str(markers_path),
            "metadaily_registry": str(md_path),
            "md_updated_at": md_payload.get("updated_at") if isinstance(md_payload, dict) else None,
        },
        "catalog": {
            "total": len(catalog),
            "families": dict(family_counts.most_common()),
            "redditdaily_identities": redditdaily,
            "preferred_alias_targets": len(preferred),
        },
        "markers": {
            "total_rows": len(markers),
            "confirmed_identities": len(confirmed_markers),
            "linked": linked,
            "ambiguous": ambiguous,
            "unlinked": unlinked,
        },
        "metadaily": {
            "confirmed": len(md_confirmed),
            "rejected": sum(1 for item in md_identities if isinstance(item, dict) and item.get("status") == "rejected"),
            "confirmed_without_marker_identity": len(md_without_markers),
            "sample_without_markers": md_without_markers[:25],
        },
        "alias_collisions_multi_canonical": {
            "count": len(collisions),
            "top": collisions[:40],
        },
        "actions": [
            "Keep apply disabled until ambiguous marker identities are reviewed.",
            "Rename manual/new_creator after human identification; do not publish to Metadaily yet.",
            "Preferred alias targets in project_registry.json resolve known noisy clusters.",
        ],
    }


def refresh_metadaily_aliases(
    *,
    md_path: Path,
    marker_identities: Dict[str, int],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Add missing marker spellings onto existing confirmed Metadaily rows only."""
    payload = json.loads(md_path.read_text(encoding="utf-8"))
    identities = list(payload.get("identities") or [])
    changed: List[dict] = []
    _, alias_index, _, _, preferred = sorter.load_identity_catalog()

    for marker_identity, count in marker_identities.items():
        key = sorter.normalize_key(marker_identity)
        target_key = preferred.get(key) or key
        hits = [item for item in (alias_index.get(target_key) or set()) if item.family == "metadaily"]
        if not hits:
            continue
        canonical = sorted(hits, key=lambda item: (-len(item.aliases), item.canonical))[0].canonical
        canon_key = sorter.normalize_key(canonical)
        for item in identities:
            if not isinstance(item, dict) or item.get("status") != "confirmed":
                continue
            md_keys = {
                sorter.normalize_key(str(item.get("id") or "")),
                sorter.normalize_key(str(item.get("primary_folder") or "")),
            }
            if canon_key not in md_keys:
                continue
            search = list(item.get("search_terms") or [])
            display = list(item.get("display_names") or [])
            existing_keys = {
                sorter.normalize_key(str(value))
                for value in (*search, *display, item.get("id"), item.get("primary_folder"))
                if value
            }
            if key in existing_keys:
                break
            search.append(marker_identity)
            item["search_terms"] = search
            changed.append(
                {
                    "id": item.get("id"),
                    "primary_folder": item.get("primary_folder"),
                    "added_alias": marker_identity,
                    "marker_count": count,
                }
            )
            break

    if changed and not dry_run:
        payload["identities"] = identities
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        md_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return {"changed": changed, "dry_run": dry_run, "count": len(changed)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markers", type=Path, default=DEFAULT_MARKERS)
    parser.add_argument("--metadaily-registry", type=Path, default=DEFAULT_MD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--refresh-metadaily-aliases",
        action="store_true",
        help="Add missing marker spellings onto existing confirmed Metadaily rows only.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    audit = build_audit(markers_path=args.markers, md_path=args.metadaily_registry)
    refresh = None
    if args.refresh_metadaily_aliases:
        markers_payload = json.loads(args.markers.read_text(encoding="utf-8"))
        refresh = refresh_metadaily_aliases(
            md_path=args.metadaily_registry,
            marker_identities=_marker_identities(list(markers_payload.get("markers") or [])),
            dry_run=args.dry_run,
        )
        audit["metadaily_alias_refresh"] = refresh

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    print(
        "markers linked={linked} ambiguous={ambiguous} unlinked={unlinked} collisions={collisions}".format(
            linked=len(audit["markers"]["linked"]),
            ambiguous=len(audit["markers"]["ambiguous"]),
            unlinked=len(audit["markers"]["unlinked"]),
            collisions=audit["alias_collisions_multi_canonical"]["count"],
        )
    )
    if refresh is not None:
        print(f"metadaily alias refresh changed={refresh['count']} dry_run={refresh['dry_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
