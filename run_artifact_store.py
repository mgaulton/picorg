#!/usr/bin/env python3
"""Publish and resolve the current immutable PicOrg run snapshot.

SQLite is the durable operational store.  The JSON files produced by a run
remain immutable interchange/audit artifacts; this module gives callers one
atomic pointer to the newest complete, face-backed snapshot without making
the UI guess from directory mtimes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


POINTER_SCHEMA_VERSION = 1
DEFAULT_POINTER = Path(".cache/picorg/current-run.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    path = path.resolve(strict=False)
    if not path.is_file():
        if required:
            raise FileNotFoundError(path)
        return None
    return {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def publish_current(
    pointer: Path,
    *,
    audit: Path,
    reconciled: Path,
    face_audit: Path,
    identity_matches: Path | None = None,
    manifest: Path | None = None,
    evidence_db: Path | None = None,
) -> dict[str, Any]:
    """Atomically publish a complete face-backed run.

    A run cannot become current unless the primary audit, face audit, and
    face-only reconciled audit exist and validate.  Existing snapshots are
    never modified or removed.
    """
    audit_artifact = _artifact(audit, required=True)
    face_artifact = _artifact(face_audit, required=True)
    reconciled_artifact = _artifact(reconciled, required=True)
    try:
        reconciled_payload = json.loads(reconciled.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid reconciled audit: {reconciled}: {exc}") from exc
    if not isinstance(reconciled_payload, dict) or reconciled_payload.get("cluster_policy") != "face-only":
        raise ValueError("refusing to publish a non-face-only reconciled audit")
    optional = {
        "identity_matches": _artifact(identity_matches) if identity_matches else None,
        "manifest": _artifact(manifest) if manifest else None,
        # SQLite is the mutable operational authority.  Record its location
        # for observability but do not hash the live WAL-backed database into
        # an immutable run pointer.
        "evidence_db": ({"path": str(evidence_db.resolve(strict=False)), "mutable": True} if evidence_db else None),
    }
    payload: dict[str, Any] = {
        "schema_version": POINTER_SCHEMA_VERSION,
        "published_at": utc_now(),
        "run_id": audit.stem,
        "audit": audit_artifact,
        "face_audit": face_artifact,
        "reconciled_audit": reconciled_artifact,
        **optional,
    }
    _atomic_write(pointer, payload)
    return payload


def _verified_artifact(value: Any) -> Path | None:
    if not isinstance(value, Mapping):
        return None
    raw_path = value.get("path")
    expected = value.get("sha256")
    if not raw_path or not expected:
        return None
    path = Path(str(raw_path))
    if not path.is_file():
        return None
    try:
        if sha256_file(path) != str(expected):
            return None
    except OSError:
        return None
    return path


def resolve_current(pointer: Path = DEFAULT_POINTER) -> Path | None:
    """Return the verified primary audit from an atomic current pointer."""
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != POINTER_SCHEMA_VERSION:
        return None
    audit = _verified_artifact(payload.get("audit"))
    face = _verified_artifact(payload.get("face_audit"))
    reconciled = _verified_artifact(payload.get("reconciled_audit"))
    if not audit or not face or not reconciled:
        return None
    try:
        data = json.loads(reconciled.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("cluster_policy") != "face-only":
        return None
    return audit


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--pointer", type=Path, default=DEFAULT_POINTER)
    publish.add_argument("--audit", type=Path, required=True)
    publish.add_argument("--face-audit", type=Path, required=True)
    publish.add_argument("--reconciled", type=Path, required=True)
    publish.add_argument("--identity-matches", type=Path)
    publish.add_argument("--manifest", type=Path)
    publish.add_argument("--evidence-db", type=Path)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--pointer", type=Path, default=DEFAULT_POINTER)
    resolve.add_argument("--field", choices=("audit", "face-audit", "reconciled"), default="audit")
    args = parser.parse_args()
    if args.command == "resolve":
        if args.field == "audit":
            result = resolve_current(args.pointer)
        else:
            try:
                payload = json.loads(args.pointer.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            key = {"face-audit": "face_audit", "reconciled": "reconciled_audit"}[args.field]
            result = _verified_artifact(payload.get(key)) if isinstance(payload, dict) else None
        if result is None:
            return 1
        print(result)
        return 0
    try:
        payload = publish_current(
            args.pointer,
            audit=args.audit,
            face_audit=args.face_audit,
            reconciled=args.reconciled,
            identity_matches=args.identity_matches,
            manifest=args.manifest,
            evidence_db=args.evidence_db,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps({"pointer": str(args.pointer), "run_id": payload["run_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
