#!/usr/bin/env python3
"""Run face matching only for new/changed audit paths and merge prior results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _gallery_signature(database: Path, manifest: Path) -> str:
    """Return a cheap generation ID for the known-identity gallery.

    The matcher uses this to preserve confirmed matches while re-evaluating
    only unresolved media after identities or exemplars change.  The manifest
    is content-addressed; the database stat covers legacy galleries without a
    manifest and atomic database replacement.
    """
    digest = hashlib.sha256()
    digest.update(str(database.resolve(strict=False)).encode("utf-8"))
    try:
        stat = database.stat()
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    except OSError:
        digest.update(b"missing")
    try:
        digest.update(manifest.read_bytes())
    except OSError:
        digest.update(b"manifest-missing")
    return digest.hexdigest()


def _gallery_identities(manifest: Path) -> set[str]:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    gallery = payload.get("gallery") if isinstance(payload, dict) else None
    return {str(identity) for identity in gallery} if isinstance(gallery, dict) else set()


def should_reuse_result(
    item: dict[str, object],
    prior_results: dict[str, dict[str, object]],
    current_fingerprints: dict[str, str],
    previous_fingerprints: dict[str, str],
    *,
    gallery_changed: bool,
    gallery_identities: set[str],
) -> bool:
    """Reuse a result only when its media and gallery generations are safe."""
    path = str(item.get("path") or "")
    prior = prior_results.get(path)
    if not path or prior is None or not current_fingerprints.get(path) or current_fingerprints.get(path) != previous_fingerprints.get(path):
        return False
    if not gallery_changed:
        return True
    return prior.get("status") == "matched" and (
        not gallery_identities or str(prior.get("matched_identity") or "") in gallery_identities
    )


def _read(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"error: expected JSON object: {path}")
    return payload


def _fingerprints(payload: dict[str, object]) -> dict[str, str]:
    value = payload.get("source_fingerprints")
    return value if isinstance(value, dict) else {}


def _aggregate(results: list[dict[str, object]], audit_results: list[dict[str, object]]) -> dict[str, object]:
    titles = {str(item.get("path")): str(item.get("title") or Path(str(item.get("path"))).stem) for item in audit_results if isinstance(item, dict)}
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    cluster_matches: dict[str, list[str]] = defaultdict(list)
    for result in results:
        if result.get("status") != "matched" or not result.get("matched_identity"):
            continue
        person = str(result["matched_identity"])
        groups[person].append(result)
        cluster_matches[titles.get(str(result.get("path")), Path(str(result.get("path"))).stem)].append(person)
    return {
        "matched": sum(1 for item in results if item.get("status") == "matched"),
        "ambiguous": sum(1 for item in results if item.get("status") == "ambiguous"),
        "no_face": sum(1 for item in results if item.get("status") == "no_face"),
        "groups": {person: items for person, items in sorted(groups.items())},
        "cluster_consensus": {
            cluster: {
                "images": len(people),
                "identities": {person: people.count(person) for person in sorted(set(people))},
                "consensus": len(set(people)) == 1 and len(people) >= 3,
            }
            for cluster, people in sorted(cluster_matches.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--gallery-manifest", type=Path, required=True)
    parser.add_argument("--embedding-store", type=Path)
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--evidence-db", type=Path, default=Path(os.environ.get("PICORG_EVIDENCE_DB", ".cache/picorg/identity_evidence.sqlite3")))
    parser.add_argument("--checkpoint", type=Path, help="durable progress checkpoint (defaults beside --output)")
    parser.add_argument("--batch-size", type=int, default=1000, help="number of pending images per resumable matcher batch")
    parser.add_argument("--trust-embedding-cache", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.45)
    parser.add_argument("--margin", type=float, default=0.08)
    parser.add_argument("--limit-per-cluster", type=int, default=20)
    parser.add_argument("--quality-weighted", action="store_true")
    parser.add_argument("--adaptive-jitters", action="store_true")
    args = parser.parse_args()
    audit = _read(args.audit)
    current_fp = _fingerprints(audit)
    previous = _read(args.previous) if args.previous.is_file() else {}
    previous_fp = _fingerprints(previous)
    if not previous_fp and isinstance(previous.get("audit"), str) and Path(str(previous["audit"])).is_file():
        previous_fp = _fingerprints(_read(Path(str(previous["audit"]))))
    prior_results = {
        str(item.get("path")): item
        for item in (previous.get("results", []) if isinstance(previous.get("results"), list) else [])
        if isinstance(item, dict) and item.get("path")
    }
    gallery_signature = _gallery_signature(args.db, args.gallery_manifest)
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    checkpoint_path = args.checkpoint or args.output.with_name(f"{args.output.stem}.checkpoint.json")
    checkpoint = _read(checkpoint_path) if checkpoint_path.is_file() else {}
    if (
        checkpoint.get("audit") == str(args.audit)
        and checkpoint.get("gallery_signature") == gallery_signature
        and isinstance(checkpoint.get("results"), list)
    ):
        # A previous process may have finished several batches before being
        # interrupted. Treat those results like a prior output, subject to the
        # same content-fingerprint checks as any other reuse.
        for item in checkpoint["results"]:
            if isinstance(item, dict) and item.get("path"):
                prior_results[str(item["path"])] = item
        checkpoint_fingerprints = checkpoint.get("source_fingerprints")
        if isinstance(checkpoint_fingerprints, dict):
            previous_fp.update({str(key): str(value) for key, value in checkpoint_fingerprints.items()})
    previous_gallery_signature = str(previous.get("gallery_signature") or checkpoint.get("gallery_signature") or "")
    # Older matcher outputs did not record the gallery generation.  Treating
    # those as unchanged would silently retain stale ambiguous decisions on
    # the first incremental run after this upgrade, so force unresolved items
    # through the current gallery once while retaining valid known matches.
    gallery_changed = bool(prior_results) and (
        not previous_gallery_signature or previous_gallery_signature != gallery_signature
    )
    gallery_identities = _gallery_identities(args.gallery_manifest)
    current_items = [
        item for item in audit.get("results", [])
        if isinstance(item, dict)
        and item.get("rule") == "unmatched"
        and Path(str(item.get("path", ""))).suffix.lower() in IMAGE_SUFFIXES
    ]
    reusable = {
        str(item["path"]): prior_results[str(item["path"])]
        for item in current_items
        if should_reuse_result(
            item,
            prior_results,
            current_fp,
            previous_fp,
            gallery_changed=gallery_changed,
            gallery_identities=gallery_identities,
        )
    }
    pending_items = [item for item in current_items if str(item["path"]) not in reusable]
    payload = dict(audit)

    def write_json_atomic(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)

    def write_checkpoint(processed_results: list[dict[str, object]]) -> None:
        write_json_atomic(checkpoint_path, {
            "schema_version": 1,
            "audit": str(args.audit),
            "gallery_signature": gallery_signature,
            "source_fingerprints": {
                str(item.get("path")): current_fp[str(item.get("path"))]
                for item in processed_results
                if str(item.get("path")) in current_fp
            },
            "results": processed_results,
            "processed": len(processed_results),
        })

    def write_partial(results: list[dict[str, object]], complete: bool = False) -> None:
        partial = dict(audit)
        partial.update(_aggregate(results, current_items))
        partial["audit"] = str(args.audit)
        partial["results"] = results
        partial["processed"] = len(results)
        partial["reused"] = len(reusable)
        partial["pending"] = len(pending_items)
        partial["incremental"] = True
        partial["complete"] = complete
        partial["gallery_signature"] = gallery_signature
        partial["source_fingerprints"] = {
            str(item.get("path")): current_fp[str(item.get("path"))]
            for item in results
            if str(item.get("path")) in current_fp
        }
        write_json_atomic(args.output, partial)

    fresh_results: list[dict[str, object]] = []
    command = [
        sys.executable,
        str(Path(__file__).with_name("face_group_unmatched.py")),
        "--db", str(args.db),
        "--gallery-manifest", str(args.gallery_manifest),
        "--threshold", str(args.threshold), "--margin", str(args.margin),
        "--limit-per-cluster", str(args.limit_per_cluster),
    ]
    if args.quality_weighted:
        command.append("--quality-weighted")
    if args.adaptive_jitters:
        command.append("--adaptive-jitters")
    if args.embedding_store:
        command += ["--embedding-store", str(args.embedding_store)]
    if args.embedding_cache:
        command += ["--embedding-cache", str(args.embedding_cache)]
    if args.trust_embedding_cache:
        command.append("--trust-embedding-cache")
    if pending_items:
        for offset in range(0, len(pending_items), args.batch_size):
            batch = pending_items[offset:offset + args.batch_size]
            batch_payload = dict(audit)
            batch_payload["results"] = batch
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="picorg-incremental-audit-", delete=False) as handle:
                json.dump(batch_payload, handle, sort_keys=True)
                temporary = Path(handle.name)
            batch_output = args.output.with_name(f".{args.output.name}.{os.getpid()}.{offset}.json")
            batch_command = command + ["--audit", str(temporary), "--output", str(batch_output)]
            try:
                print(f"incremental face match: batch {offset + 1}-{offset + len(batch)}/{len(pending_items)}", flush=True)
                completed = subprocess.run(batch_command, check=False)
                if completed.returncode:
                    write_checkpoint(fresh_results)
                    write_partial(list(reusable.values()) + fresh_results)
                    return completed.returncode
                fresh = _read(batch_output)
                fresh_results.extend(item for item in fresh.get("results", []) if isinstance(item, dict))
                write_checkpoint(fresh_results)
                merged_partial = list(reusable.values()) + fresh_results
                merged_partial.sort(key=lambda item: str(item.get("path")))
                write_partial(merged_partial)
            finally:
                temporary.unlink(missing_ok=True)
                batch_output.unlink(missing_ok=True)
    write_checkpoint(fresh_results)
    fresh = {"audit": str(args.audit), "results": fresh_results}
    if checkpoint_path.exists():
        checkpoint_path.unlink(missing_ok=True)
    fresh_results = [item for item in fresh.get("results", []) if isinstance(item, dict)]
    merged = list(reusable.values()) + fresh_results
    merged.sort(key=lambda item: str(item.get("path")))
    aggregate = _aggregate(merged, current_items)
    fresh.update(aggregate)
    fresh["audit"] = str(args.audit)
    fresh["results"] = merged
    fresh["processed"] = len(merged)
    fresh["reused"] = len(reusable)
    fresh["pending"] = len(pending_items)
    fresh["incremental"] = True
    fresh["gallery_signature"] = gallery_signature
    fresh["gallery_changed"] = gallery_changed
    fresh["reused_confirmed"] = sum(1 for item in reusable.values() if item.get("status") == "matched")
    fresh["reprocessed_unresolved"] = len(pending_items) if gallery_changed else 0
    fresh["source_fingerprints"] = {
        str(item.get("path")): current_fp[str(item.get("path"))]
        for item in merged
        if str(item.get("path")) in current_fp
    }
    fresh["complete"] = True
    write_json_atomic(args.output, fresh)
    if fresh_results:
        try:
            from identity_evidence_store import record_match_results
            run_id = f"incremental:{args.output.name}:{Path(args.audit).stat().st_mtime_ns}"
            evidence_results = [
                {**item, "sha256": current_fp.get(str(item.get("path")))}
                for item in fresh_results
            ]
            stored = record_match_results(args.evidence_db, evidence_results, run_id=run_id)
            print(f"evidence store: match_results={stored}", flush=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(f"error: unable to persist match evidence: {exc}") from exc
    print(json.dumps({"reused": len(reusable), "pending": len(pending_items), "processed": len(merged)}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
